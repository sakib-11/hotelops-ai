"""Movement classification — deterministic classification on top of the
15.5.1 measurement foundation (Task 15.5.2).

The 15.5.1 foundation classifies each consecutive observation pair into a
``MovementMeasurement`` (distance + event-time delta) and an immediate
single-threshold UNKNOWN / STATIONARY / MOVING result. This module is the
classifier layer on top of it: it consumes those measurements (NEVER
recomputing distance) and applies hysteresis + event-time qualification.
Architecture (Task 15.5.2):

    SpatialObservation
        ↓ 15.5.1 MovementEngine (pair math, ordering, dedup, reorder)
    MovementMeasurement (distance + event_time + provenance)
        ↓ classification_input_from_movement (the only sanctioned wiring)
    MOVEMENT CLASSIFICATION FSM (UNKNOWN / STATIONARY / MOVING)
        ↓ hysteresis (enter > exit) + event-time qualification
    MovementClassificationState + MovementClassificationTransition (facts)

Classification model (Task 15.5.2 §2): UNKNOWN is the pristine per-track
state. The first measured pair classifies directly — UNKNOWN -> STATIONARY
(a displacement below the stationary policy) or UNKNOWN -> MOVING (above
the movement policy); there is no prior state for a qualification window
to protect. MOVING means the displacement exceeds the configured
``movement_enter_threshold``; STATIONARY means it remains below the
configured ``movement_exit_threshold``. Every value comes from
``TemporalPolicy`` — nothing is hardcoded.

Hysteresis (Task 15.5.2 §4): the enter threshold dominates the exit
threshold (validated by ``TemporalPolicy``). A measurement below the exit
threshold is STATIONARY evidence, above the enter threshold is MOVING
evidence, and between the two is the hysteresis band — which RETAINS the
current classification and never flips it (the exact anti-flap guard for
small positional noise near a boundary).

Temporal qualification (Task 15.5.2 §5): when a measurement's evidence
points away from the current state, the target state is held pending
(``pending_state`` + ``qualification_started``, both event time). The
classification changes ONLY once the evidence stays in that direction for
``movement_qualification_seconds`` of EVENT time (never processing time,
never wall clock). A contradicting measurement or a hysteresis-band
measurement cancels the run — \"qualified\" means every measurement since
the run started sustained it. The transition's event_time is the event
time of the confirming measurement (the qualification-completed boundary),
never the pending start. ``0.0`` disables qualification: one qualifying
measurement changes the state immediately (the degenerate single-threshold
behavior).

Ordering / idempotency / occlusion (Task 15.5.2 §10/§11/§12/§13): the
classifier reuses the 15.1 policy verbatim — ``event_time`` is
authoritative, ordering uses ``(event_time, frame_id)``, a replay of the
last applied position is DEDUPLICATED (no second classification change),
and an older position within ``reorder_window_seconds`` is REORDERED
(accepted with no rewind) while anything older raises ``LateEventError``.
Because the classification stream is a 1:1 projection of the movement
stream, the classifier reproduces the movement engine's dedup/reorder
facts deterministically. A temporary gap in observations (occlusion) never
resets the classification: no measurement means no evidence, so the state
is preserved (the existing grace policy is reused — no second occlusion
mechanism).

State isolation (Task 15.5.2 §14): every ``TemporalStateKey`` is an
independent per-track state; the engine verifies the key's
tenant/venue/session/camera/configuration-version/track/context against
the measurement's provenance (``StateKeyMismatchError`` otherwise) and the
measurement's ``policy_revision`` against the engine policy. The same
track in two sessions never shares state.

Configuration provenance (Task 15.5.2 §15): the key carries the pinned
``configuration_version_id`` and the policy carries its ``revision``; the
classifier never queries \"the latest configuration\". Historical
observations continue using the configuration pinned to their session.

Checkpoint (Task 15.5.2 §17): ``MovementClassificationCheckpoint`` carries
the classification, state_since, the qualification run, last event time,
identity, configuration version (in the key) and watermark under the same
versioned discipline as the sibling families; restart recovery reproduces
uninterrupted processing.

PURE CORE (Task 15.5.2 §21): no PostgreSQL, Redis, S3, HTTP, FastAPI, or
LLM calls, no current-time reads, no unbounded state (the state holds
scalars only — each step needs exactly the current measurement).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any
from uuid import uuid5

from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    LateEventError,
    StateKeyMismatchError,
)
from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule
from backend.app.intelligence.temporal.movement import MovementResult
from contracts.common import EventId, FrameId
from contracts.spatial import SpatialObservation
from contracts.temporal import (
    MOVEMENT_STATES,
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    MovementClassificationCheckpoint,
    MovementClassificationState,
    MovementClassificationTransition,
    MovementMeasurement,
    TemporalPolicy,
    TemporalStateKey,
)

__all__ = [
    "MOVEMENT_CLASSIFICATION_FSM",
    "MovementClassificationEngine",
    "MovementClassificationInput",
    "MovementClassificationResult",
    "classification_input_from_movement",
]

# The classification FSM. The engine decides WHICH event to emit from the
# measurement's evidence + qualification state (mirroring how the presence
# engine decides when to emit enter_confirmed); the FSM only answers "is
# this a legal move?". The "observed_*" events from the far state are the
# legal STAYS during a pending qualification run (a stationary entity
# observing above-enter evidence stays stationary until qualify_moving).
MOVEMENT_CLASSIFICATION_FSM = DeterministicFsm(
    name="movement_classification",
    version=TEMPORAL_ENGINE_VERSION,
    states=MOVEMENT_STATES,
    initial_state="unknown",
    rules=(
        # The first measurement is the classification anchor: UNKNOWN
        # stays UNKNOWN only for a measurement-less step (no pair yet).
        FsmRule(from_state="unknown", event="first_observed", to_state="unknown"),
        # A measured pair classifies UNKNOWN directly (no qualification —
        # there is no prior state to protect). A band measurement (below
        # the enter threshold) is conservative STATIONARY.
        FsmRule(from_state="unknown", event="observed_stationary", to_state="stationary"),
        FsmRule(from_state="unknown", event="observed_moving", to_state="moving"),
        FsmRule(from_state="unknown", event="observed_band", to_state="stationary"),
        # From STATIONARY: evidence stays, the hysteresis band stays, and
        # above-enter evidence stays while its qualification runs.
        FsmRule(from_state="stationary", event="observed_stationary", to_state="stationary"),
        FsmRule(from_state="stationary", event="observed_moving", to_state="stationary"),
        FsmRule(from_state="stationary", event="observed_band", to_state="stationary"),
        FsmRule(from_state="stationary", event="qualify_moving", to_state="moving"),
        # From MOVING: the mirror-image stays, plus the stationary exit.
        FsmRule(from_state="moving", event="observed_moving", to_state="moving"),
        FsmRule(from_state="moving", event="observed_stationary", to_state="moving"),
        FsmRule(from_state="moving", event="observed_band", to_state="moving"),
        FsmRule(from_state="moving", event="qualify_stationary", to_state="stationary"),
    ),
)


@dataclass(frozen=True, slots=True)
class MovementClassificationInput:
    """Pure-engine input: the per-track key + one 15.5.1 movement step.

    ``measurement`` is the foundation's fact for this step; it is None
    only when the movement engine emitted no measurement (the track
    anchor — a measurement is a PAIR — or a step the movement engine
    deduplicated/reordered). ``event_time`` + ``frame_id`` are the
    observation's event-time position (the 15.1 ordering discipline,
    re-validated against the measurement). ``processing_time`` is
    metadata only — ordering ALWAYS uses the event time.
    """

    key: TemporalStateKey
    measurement: MovementMeasurement | None
    event_time: datetime
    frame_id: FrameId
    processing_time: datetime


@dataclass(frozen=True, slots=True)
class MovementClassificationResult:
    """Deterministic result of applying one movement step."""

    state: MovementClassificationState
    # One MovementClassificationTransition whenever the classification
    # actually changed; None for stays, deduplicated, reordered, and
    # measurement-less steps.
    transition: MovementClassificationTransition | None = None
    # The 15.5.1 measurement that drove this step (None for the anchor,
    # deduplicated, and reordered inputs).
    measurement: MovementMeasurement | None = None
    deduplicated: bool = False
    reordered: bool = False


class MovementClassificationEngine:
    """Pure per-track movement classifier over 15.5.1 measurements.

    A standalone deterministic engine (its state shape — classification +
    qualification run — differs from the ``TemporalState`` and the
    ``MovementState``, so it is not a subclass of either) that REUSES the
    foundation's discipline wholesale: the same ``TemporalPolicy``
    (reorder window, hysteresis thresholds, qualification duration,
    revision), the same single-watermark ordering, the same typed error
    taxonomy, the same versioned checkpoint, and the same content-derived
    fact identities. It NEVER computes a measurement itself.
    """

    def __init__(
        self,
        *,
        fsm: DeterministicFsm = MOVEMENT_CLASSIFICATION_FSM,
        policy: TemporalPolicy,
    ) -> None:
        self._fsm = fsm
        self._policy = policy

    @property
    def fsm(self) -> DeterministicFsm:
        return self._fsm

    @property
    def policy(self) -> TemporalPolicy:
        return self._policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initial_state(self, key: TemporalStateKey) -> MovementClassificationState:
        """The pristine per-track classification: UNKNOWN, no evidence."""
        if not isinstance(key, TemporalStateKey):
            raise InvalidTemporalInputError(
                f"key must be a TemporalStateKey, got {type(key).__name__}"
            )
        if key.fsm_kind != "movement_classification":
            raise InvalidTemporalInputError(
                "movement classification state key fsm_kind must be "
                f"'movement_classification', got {key.fsm_kind!r}"
            )
        return MovementClassificationState(fsm_version=self._fsm.version, key=key)

    def apply(
        self,
        state: MovementClassificationState,
        inp: MovementClassificationInput,
    ) -> MovementClassificationResult:
        """Apply one 15.5.1 movement step (pure, deterministic).

        Raises the typed ``TemporalError`` taxonomy on any failure; a
        failure is never encoded as a state or a transition.
        """
        self._validate(state, inp)
        event_time = inp.event_time
        frame_id = inp.frame_id
        position = (event_time, frame_id)
        watermark = (state.watermark_event_time, state.last_applied_frame_id)

        if watermark[0] is not None:
            if position == watermark:
                return MovementClassificationResult(state=state, deduplicated=True)
            if position < watermark:
                return self._apply_out_of_order(state, event_time=event_time, frame_id=frame_id)

        measurement = inp.measurement
        if measurement is None:
            # A step with no measured pair (the track anchor, or a step
            # the movement engine already absorbed): no classification
            # evidence — the watermark/last_seen advance only, the
            # classification (and any pending qualification run) is
            # preserved. This is the occlusion grace: a missing
            # measurement never resets the state.
            updated = state.model_copy(
                update={
                    "last_seen": event_time,
                    "watermark_event_time": event_time,
                    "last_applied_frame_id": frame_id,
                }
            )
            return MovementClassificationResult(state=updated)

        next_state, pending_state, qualification_started, transition = self._classify(
            state, measurement
        )
        updates: dict[str, Any] = {
            "current_state": next_state,
            "state_since": (event_time if next_state != state.current_state else state.state_since),
            "pending_state": pending_state,
            "qualification_started": qualification_started,
            "last_seen": event_time,
            "watermark_event_time": event_time,
            "last_applied_frame_id": frame_id,
        }
        updated = state.model_copy(update=updates)
        return MovementClassificationResult(
            state=updated,
            transition=transition,
            measurement=measurement,
        )

    def checkpoint(self, state: MovementClassificationState) -> MovementClassificationCheckpoint:
        """Serialize ``state`` into a versioned, resumable checkpoint."""
        return MovementClassificationCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision=self._policy.revision,
            state=state,
        )

    def restore(self, checkpoint: MovementClassificationCheckpoint) -> MovementClassificationState:
        """Restore a checkpoint, rejecting version/policy drift (typed)."""
        if not isinstance(checkpoint, MovementClassificationCheckpoint):
            raise InvalidTemporalInputError(
                "checkpoint must be a MovementClassificationCheckpoint, "
                f"got {type(checkpoint).__name__}"
            )
        if checkpoint.engine_version != TEMPORAL_ENGINE_VERSION:
            raise FsmVersionMismatchError(
                f"checkpoint engine version {checkpoint.engine_version!r} does not "
                f"match the engine version {TEMPORAL_ENGINE_VERSION!r}"
            )
        if checkpoint.policy_revision != self._policy.revision:
            raise CheckpointIntegrityError(
                f"checkpoint policy revision {checkpoint.policy_revision!r} does not "
                f"match the engine policy revision {self._policy.revision!r}"
            )
        if checkpoint.state.fsm_version != self._fsm.version:
            raise FsmVersionMismatchError(
                f"checkpoint state FSM version {checkpoint.state.fsm_version!r} does "
                f"not match FSM '{self._fsm.name}' version {self._fsm.version!r}"
            )
        if checkpoint.state.key.fsm_kind != "movement_classification":
            raise InvalidTemporalInputError(
                "checkpoint state key fsm_kind is not 'movement_classification' "
                "— cross-FSM restore is rejected"
            )
        return checkpoint.state

    # ------------------------------------------------------------------
    # Validation (provenance + measurement integrity)
    # ------------------------------------------------------------------

    def _validate(
        self, state: MovementClassificationState, inp: MovementClassificationInput
    ) -> None:
        if not isinstance(state, MovementClassificationState):
            raise InvalidTemporalInputError(
                f"state must be a MovementClassificationState, got {type(state).__name__}"
            )
        if not isinstance(inp, MovementClassificationInput):
            raise InvalidTemporalInputError(
                f"input must be a MovementClassificationInput, got {type(inp).__name__}"
            )
        if inp.key != state.key:
            raise InvalidTemporalInputError(
                "movement classification input key must match the state key "
                "(cross-track apply is rejected)"
            )
        if inp.key.fsm_kind != "movement_classification":
            raise InvalidTemporalInputError(
                "movement classification input key fsm_kind must be "
                f"'movement_classification', got {inp.key.fsm_kind!r}"
            )
        if state.fsm_version != self._fsm.version:
            raise FsmVersionMismatchError(
                f"state FSM version {state.fsm_version!r} does not match FSM "
                f"'{self._fsm.name}' version {self._fsm.version!r}"
            )
        if inp.processing_time.tzinfo is None:
            raise InvalidTemporalInputError(
                "processing_time must be timezone-aware UTC (metadata only, "
                "never used for ordering)"
            )
        if inp.event_time.tzinfo is None:
            raise InvalidTemporalInputError("input event_time must be timezone-aware UTC")
        measurement = inp.measurement
        if measurement is None:
            return
        if not isinstance(measurement, MovementMeasurement):
            raise InvalidTemporalInputError(
                f"measurement must be a MovementMeasurement or None, got "
                f"{type(measurement).__name__}"
            )
        if measurement.key.fsm_kind != "movement":
            raise InvalidTemporalInputError(
                "classification consumes 15.5.1 movement measurements, got measurement "
                f"fsm_kind {measurement.key.fsm_kind!r}"
            )
        if measurement.event_time != inp.event_time:
            raise InvalidTemporalInputError(
                "measurement.event_time must equal the input event_time "
                "(a mis-wired movement -> classification step)"
            )
        if measurement.event_time.tzinfo is None:
            raise InvalidTemporalInputError("measurement event_time must be timezone-aware UTC")
        if not isfinite(measurement.distance):
            raise InvalidTemporalInputError(
                "measurement distance must be finite (a NaN/Inf displacement is "
                "never classified — movement is never fabricated)"
            )
        if measurement.policy_revision != self._policy.revision:
            raise InvalidTemporalInputError(
                f"measurement policy_revision {measurement.policy_revision!r} does not "
                f"match the engine policy revision {self._policy.revision!r} "
                "(measurements must come from the SAME pinned policy)"
            )
        self._check_measurement_matches_key(inp.key, measurement)

    def _check_measurement_matches_key(
        self, key: TemporalStateKey, measurement: MovementMeasurement
    ) -> None:
        """Provenance integrity: the classification key and the measurement
        agree on every scope component (fsm_kind differs by design —
        movement vs movement_classification)."""
        mismatches: list[str] = []
        if key.tenant_id != measurement.key.tenant_id:
            mismatches.append("tenant_id")
        if key.venue_id != measurement.key.venue_id:
            mismatches.append("venue_id")
        if key.session_id != measurement.key.session_id:
            mismatches.append("session_id")
        if key.camera_id != measurement.key.camera_id:
            mismatches.append("camera_id")
        if key.configuration_version_id != measurement.key.configuration_version_id:
            mismatches.append("configuration_version_id")
        if key.track_id != measurement.key.track_id:
            mismatches.append("track_id")
        if key.semantic_context != measurement.key.semantic_context:
            mismatches.append("semantic_context")
        if mismatches:
            raise StateKeyMismatchError(
                f"movement classification key does not match the movement measurement "
                f"provenance ({', '.join(mismatches)}); cross-scope classification "
                "is rejected"
            )

    # ------------------------------------------------------------------
    # Ordering (the 15.1 policy, per-track)
    # ------------------------------------------------------------------

    def _apply_out_of_order(
        self,
        state: MovementClassificationState,
        *,
        event_time: datetime,
        frame_id: FrameId,
    ) -> MovementClassificationResult:
        """Deterministic out-of-order policy (15.1 §5/§6): window or reject."""
        assert state.watermark_event_time is not None
        delta = (state.watermark_event_time - event_time).total_seconds()
        if delta <= self._policy.reorder_window_seconds:
            # Within the allowed reordering window: accepted, NEVER rewinds
            # the classification, the qualification run, or the watermark;
            # refreshes last_seen only if newer (accept-with-no-rewind).
            last_seen = event_time
            if state.last_seen is not None and state.last_seen > event_time:
                last_seen = state.last_seen
            updated = state.model_copy(update={"last_seen": last_seen})
            return MovementClassificationResult(state=updated, reordered=True)
        raise LateEventError(
            f"measurement event_time {event_time.isoformat()} is "
            f"{delta:.3f}s older than the classification watermark "
            f"{state.watermark_event_time.isoformat()}, beyond the reordering "
            f"window of {self._policy.reorder_window_seconds}s — rejected "
            "deterministically (never silently discarded or force-ordered)"
        )

    # ------------------------------------------------------------------
    # Classification semantics (hysteresis + qualification, pure)
    # ------------------------------------------------------------------

    def _classify(
        self,
        state: MovementClassificationState,
        measurement: MovementMeasurement,
    ) -> tuple[str, str | None, datetime | None, MovementClassificationTransition | None]:
        """One deterministic classification step for a measured pair.

        Returns (next_state, pending_state, qualification_started,
        transition) — the caller materializes the state.
        """
        policy = self._policy
        distance = measurement.distance
        event_time = measurement.event_time
        current = state.current_state

        if current == "unknown":
            # First measurement of the track: no prior state exists for a
            # qualification window to protect, so it classifies directly.
            # A band measurement (below the enter threshold) is the
            # conservative STATIONARY — the entity is not clearly moving.
            if distance > policy.movement_enter_threshold:
                next_state = self._fsm.transition("unknown", "observed_moving")
                return (
                    next_state,
                    None,
                    None,
                    self._build_transition(
                        key=state.key,
                        from_state="unknown",
                        to_state=next_state,
                        event_time=event_time,
                        measurement=measurement,
                        qualification_started=None,
                    ),
                )
            next_state = self._fsm.transition("unknown", "observed_stationary")
            return (
                next_state,
                None,
                None,
                self._build_transition(
                    key=state.key,
                    from_state="unknown",
                    to_state=next_state,
                    event_time=event_time,
                    measurement=measurement,
                    qualification_started=None,
                ),
            )

        # Hysteresis evidence — strictly-above / strictly-below. A pair
        # exactly at a threshold is NOT on the far side.
        if distance > policy.movement_enter_threshold:
            evidence = "moving"
        elif distance < policy.movement_exit_threshold:
            evidence = "stationary"
        else:
            evidence = "band"

        if evidence == "band":
            # The hysteresis band: retain the current classification (the
            # measurement is between the stationary and movement policies,
            # so it never flips state). An ambiguous measurement also
            # breaks any in-progress qualification run — the pending
            # evidence was not sustained, so it is not "qualified".
            self._fsm.transition(current, "observed_band")
            return current, None, None, None

        if current == "stationary":
            if evidence == "moving":
                return self._qualify(
                    state, target="moving", event_time=event_time, measurement=measurement
                )
            # Stationary evidence while stationary: stay. A below-exit
            # measurement after an above-enter run means the movement was
            # NOT sustained — the run is cancelled.
            self._fsm.transition(current, "observed_stationary")
            return current, None, None, None

        # current == "moving"
        if evidence == "stationary":
            return self._qualify(
                state, target="stationary", event_time=event_time, measurement=measurement
            )
        # Moving evidence while moving: stay, cancel any run.
        self._fsm.transition(current, "observed_moving")
        return current, None, None, None

    def _qualify(
        self,
        state: MovementClassificationState,
        *,
        target: str,
        event_time: datetime,
        measurement: MovementMeasurement,
    ) -> tuple[str, str | None, datetime | None, MovementClassificationTransition | None]:
        """Advance (or start) a qualification run toward ``target``.

        The run completes when the confirming measurement arrives at least
        ``movement_qualification_seconds`` of EVENT time after the run
        started — the transition event_time is that confirming
        measurement's event_time, never the run start, never processing
        time. ``0.0`` disables qualification (one qualifying measurement
        changes the state immediately).
        """
        policy = self._policy
        started = state.qualification_started
        if state.pending_state == target and started is not None:
            elapsed = (event_time - started).total_seconds()
            if elapsed >= policy.movement_qualification_seconds:
                next_state = self._fsm.transition(state.current_state, f"qualify_{target}")
                return (
                    next_state,
                    None,
                    None,
                    self._build_transition(
                        key=state.key,
                        from_state=state.current_state,
                        to_state=next_state,
                        event_time=event_time,
                        measurement=measurement,
                        qualification_started=started,
                    ),
                )
            # Qualification still running: stay, keep the run anchored at
            # its original event-time start.
            return (
                state.current_state,
                target,
                started,
                None,
            )
        if policy.movement_qualification_seconds == 0:
            # No qualification window configured: the first qualifying
            # measurement changes the state immediately.
            next_state = self._fsm.transition(state.current_state, f"qualify_{target}")
            return (
                next_state,
                None,
                None,
                self._build_transition(
                    key=state.key,
                    from_state=state.current_state,
                    to_state=next_state,
                    event_time=event_time,
                    measurement=measurement,
                    qualification_started=None,
                ),
            )
        # First qualifying measurement of the run: anchor it here (event
        # time), stay in the current state.
        return state.current_state, target, event_time, None

    # ------------------------------------------------------------------
    # Transition derivation (pure, content-derived identity)
    # ------------------------------------------------------------------

    def _build_transition(
        self,
        *,
        key: TemporalStateKey,
        from_state: str,
        to_state: str,
        event_time: datetime,
        measurement: MovementMeasurement,
        qualification_started: datetime | None,
    ) -> MovementClassificationTransition:
        """One deterministic classification-change fact.

        The ID is content-derived (UUID5 over the key + transition +
        event-time + confirming measurement), so replaying the same
        timeline reproduces the same identities (Task 7 idempotency
        principle, reused).
        """
        canonical = "|".join([
            key.canonical(),
            from_state,
            to_state,
            event_time.isoformat(),
            str(measurement.measurement_id),
            qualification_started.isoformat() if qualification_started is not None else "",
            self._fsm.version,
            self._policy.revision,
        ])
        transition_id = EventId(uuid5(TEMPORAL_ID_NAMESPACE, canonical))
        return MovementClassificationTransition(
            transition_id=transition_id,
            fsm_kind=self._fsm.name,
            key=key,
            from_state=from_state,
            to_state=to_state,
            event_time=event_time,
            measurement_id=measurement.measurement_id,
            qualification_started=qualification_started,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )


def classification_input_from_movement(
    key: TemporalStateKey,
    observation: SpatialObservation,
    movement_result: MovementResult,
    processing_time: datetime,
) -> MovementClassificationInput:
    """Derive the 15.5.2 classification input from one 15.5.1 movement step.

    The ONLY sanctioned wiring (mirrors ``dwell_event_from_presence`` /
    ``occupancy_event_from_presence``): the classification consumes the
    movement engine's measurement verbatim — never recomputing distance —
    plus the observation's event-time position. When the movement step
    emitted no measurement (the track anchor, or a deduplicated/reordered
    step) the classification receives a measurement-less step and
    reproduces the same no-op deterministically (its own dedup/reorder
    position discipline then mirrors the movement engine's).
    """
    measurement = movement_result.measurement
    if measurement is not None and measurement.event_time != observation.event_time:
        raise InvalidTemporalInputError(
            "movement measurement event_time must match the observation event_time "
            "(mis-wired movement -> classification step)"
        )
    return MovementClassificationInput(
        key=key,
        measurement=measurement,
        event_time=observation.event_time,
        frame_id=observation.frame_id,
        processing_time=processing_time,
    )
