"""Movement state foundation (Task 15.5.1).

Classifies an existing tracked entity as UNKNOWN / STATIONARY / MOVING
from consecutive canonical event-time spatial observations. This is the
deterministic foundation for the later movement-classification and
waiting-intelligence steps. Architecture (Task 15.5.1):

    TrackObservation
        ↓ Task 14 point policy (extract_point) — the canonical position
    SpatialObservation (canonical point + event_time + provenance)
        ↓ Movement Measurement (distance + time delta of the pair)
    MOVEMENT FSM (UNKNOWN / STATIONARY / MOVING)
        ↓
    MovementState + MovementMeasurement (facts)
        ↓
    Future movement classification / waiting intelligence

Position policy (Task 15.5.1 §2/§4): movement reuses the canonical
``SpatialPointModel`` verbatim — no new coordinate policy. The point's
declared ``coordinate_space`` (IMAGE_NORMALIZED [0, 1] x [0, 1] by
default, ADR-010) is preserved and the engine refuses to measure a pair
whose two points live in different spaces. Distance is a displacement
in that declared space ONLY: the engine deliberately computes NO
velocity — an image-normalized displacement is never pretended to be
physical speed (an explicit conversion policy does not exist, so none
is invented).

Measurement (Task 15.5.1 §3): for two consecutive in-order observations
of the same entity,

    distance   = Euclidean(previous_position, current_position)
    time_delta = current_event_time - previous_event_time

computed purely from event times (processing time is metadata only).
The measurement preserves full provenance via the nested state key
(tenant/venue/session/camera/configuration version/track).

States (Task 15.5.1 §6): ONLY ``unknown`` / ``stationary`` / ``moving``
(``MOVEMENT_STATES``, the single shared declaration). NO waiting /
queueing / service / session_closed states exist here.

Classification (Task 15.5.1 §7): the ONLY knob is the configuration-
driven ``TemporalPolicy.movement_threshold`` — a pair whose distance
EXCEEDS the threshold classifies as MOVING, at-or-below it as
STATIONARY (strictly-above semantics: a zero-displacement pair is
stationary even under the degenerate ``0.0`` default, and §12's
"movement exceeding the threshold" is honored verbatim). The first
observation of a track is the measurement anchor (state stays UNKNOWN,
no measurement yet — a measurement is a PAIR). Qualification durations
and hysteresis belong to 15.5.4/15.5.5 and are deliberately absent;
``state_since`` records when the current classification was entered as
their foundation.

Discipline (reuses the 15.1 policy verbatim — no second ordering
system, Task 15.5.1 §5): movement is per-track, so the ordering is the
single-watermark version of the foundation policy:

  - ``event_time`` is authoritative; ordering uses
    ``(event_time, frame_id)``.
  - A duplicate of the last applied position -> DEDUPLICATED (no
    measurement, no state change; the same content-derived fact would
    be reproduced).
  - A position older than the watermark within
    ``policy.reorder_window_seconds`` -> REORDERED: accepted, may
    refresh ``last_seen`` if newer, NEVER rewinds the measurement
    anchor, the classification, or the watermark (the documented
    accept-with-no-rewind policy). Older -> ``LateEventError``.
  - Equal event timestamps with later frame ids are applied (a valid
    pair with ``time_delta == 0`` — never a division by zero).

Isolation (Task 15.5.1 §8): each ``TemporalStateKey`` is an independent
per-track state; the engine verifies session/track/camera/configuration
version against the observation (``StateKeyMismatchError`` otherwise).
The same track identifier in two sessions has two keys and never shares
state.

Idempotency (Task 15.5.1 §9): the same observation applied twice is a
duplicate — no second distance, no second state transition (Task 7
principle, reused).

Checkpoint (Task 15.5.1 §15): ``MovementCheckpoint`` carries the state
that will need persistence — current classification, previous position,
previous event time, identity, configuration version (in the key),
watermark — under the same versioned discipline as the sibling
families. Restart recovery is 15.5.6; compatibility is verified now.

PURE CORE (Task 15.5.1 §16): no PostgreSQL, Redis, S3, HTTP, FastAPI,
or LLM calls, no current-time reads, no fallback to \\"the latest
configuration\\". Points are validated with the Task 14 Step 2
``validate_coordinate`` (the established geometry boundary) before any
arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import hypot
from uuid import uuid5

from backend.app.intelligence.geometry.points import validate_coordinate
from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    LateEventError,
    StateKeyMismatchError,
)
from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule
from contracts.common import EventId, FrameId
from contracts.spatial import SpatialObservation, SpatialPointModel
from contracts.temporal import (
    MOVEMENT_STATES,
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    MovementCheckpoint,
    MovementMeasurement,
    MovementState,
    TemporalPolicy,
    TemporalStateKey,
)

__all__ = [
    "MOVEMENT_FSM",
    "MovementEngine",
    "MovementInput",
    "MovementResult",
]

MOVEMENT_FSM = DeterministicFsm(
    name="movement",
    version=TEMPORAL_ENGINE_VERSION,
    states=MOVEMENT_STATES,
    initial_state="unknown",
    rules=(
        # The first observation of a track is the measurement anchor: it
        # cannot classify anything yet, so UNKNOWN stays UNKNOWN.
        FsmRule(from_state="unknown", event="first_observed", to_state="unknown"),
        # A measured pair classifies against the configured threshold.
        FsmRule(from_state="unknown", event="observed_stationary", to_state="stationary"),
        FsmRule(from_state="unknown", event="observed_moving", to_state="moving"),
        FsmRule(from_state="stationary", event="observed_stationary", to_state="stationary"),
        FsmRule(from_state="stationary", event="observed_moving", to_state="moving"),
        FsmRule(from_state="moving", event="observed_moving", to_state="moving"),
        FsmRule(from_state="moving", event="observed_stationary", to_state="stationary"),
    ),
)


def _euclidean_distance(a: SpatialPointModel, b: SpatialPointModel) -> float:
    """Euclidean distance between two canonical spatial points.

    ``sqrt((x2 - x1)^2 + (y2 - y1)^2)`` in the points' shared coordinate
    space (the engine guarantees the spaces match before calling). The
    Task 14 Step 2 geometry library ships no point-to-point helper (only
    ``distance_point_to_segment``), so this single pure function lives
    here, mirroring the library's ``math.hypot`` convention. It is a
    displacement in image/metric space — never a physical velocity.
    """
    return hypot(a.x - b.x, a.y - b.y)


@dataclass(frozen=True, slots=True)
class MovementInput:
    """Pure-engine input: the per-track key + one canonical spatial observation.

    The classification is DERIVED inside the engine from the measurement
    (distance vs the configured threshold) — it is never caller-supplied,
    so a mis-wired kind is impossible. ``processing_time`` is metadata
    only; ordering ALWAYS uses the observation's event time.
    """

    key: TemporalStateKey
    observation: SpatialObservation
    processing_time: datetime


@dataclass(frozen=True, slots=True)
class MovementResult:
    """Deterministic result of applying one spatial observation."""

    state: MovementState
    # One MovementMeasurement for every in-order observation after the
    # track's first (a measurement is a pair); None for the anchor,
    # deduplicated, and reordered inputs.
    measurement: MovementMeasurement | None = None
    deduplicated: bool = False
    reordered: bool = False


class MovementEngine:
    """Pure per-track movement classifier over canonical spatial observations.

    A standalone deterministic engine (the measurement anchor — previous
    position + previous event time — is movement-specific state, so it is
    not a ``TemporalEngine`` subclass) that REUSES the foundation's
    discipline wholesale: the same ``TemporalPolicy`` (reorder window,
    threshold, revision), the same single-watermark ordering, the same
    typed error taxonomy, the same versioned ``MovementCheckpoint``, and
    the same content-derived fact identities.
    """

    def __init__(
        self,
        *,
        fsm: DeterministicFsm = MOVEMENT_FSM,
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

    def initial_state(self, key: TemporalStateKey) -> MovementState:
        """The pristine per-track state: UNKNOWN, no measurement anchor."""
        if not isinstance(key, TemporalStateKey):
            raise InvalidTemporalInputError(
                f"key must be a TemporalStateKey, got {type(key).__name__}"
            )
        if key.fsm_kind != "movement":
            raise InvalidTemporalInputError(
                f"movement state key fsm_kind must be 'movement', got {key.fsm_kind!r}"
            )
        return MovementState(fsm_version=self._fsm.version, key=key)

    def apply(self, state: MovementState, inp: MovementInput) -> MovementResult:
        """Apply one canonical spatial observation (pure, deterministic).

        Raises the typed ``TemporalError`` taxonomy (and the geometry
        ``InvalidCoordinateError`` for malformed points) on any failure;
        a failure is never encoded as a state or a measurement.
        """
        self._validate(state, inp)
        obs = inp.observation
        event_time = obs.event_time
        frame_id = obs.frame_id
        position = (event_time, frame_id)
        watermark = (state.watermark_event_time, state.last_applied_frame_id)

        if watermark[0] is not None:
            if position == watermark:
                return MovementResult(state=state, deduplicated=True)
            if position < watermark:
                return self._apply_out_of_order(state, event_time=event_time, frame_id=frame_id)

        # In-order: measure the pair, then classify through the FSM.
        previous_position = state.previous_position
        if previous_position is None:
            # First observation: the measurement anchor. No pair exists
            # yet, so UNKNOWN stays UNKNOWN and no measurement is emitted.
            next_state = self._fsm.transition(state.current_state, "first_observed")
            measurement = None
        else:
            previous_time = state.previous_event_time
            assert previous_time is not None  # anchor invariant: set together
            distance = _euclidean_distance(previous_position, obs.spatial_point)
            time_delta = (event_time - previous_time).total_seconds()
            event_kind = (
                "observed_moving"
                if distance > self._policy.movement_threshold
                else "observed_stationary"
            )
            next_state = self._fsm.transition(state.current_state, event_kind)
            measurement = self._build_measurement(
                key=inp.key,
                previous_position=previous_position,
                previous_event_time=previous_time,
                current_position=obs.spatial_point,
                event_time=event_time,
                distance=distance,
                time_delta=time_delta,
            )

        updated = state.model_copy(
            update={
                "current_state": next_state,
                # state_since is the entry time of the CURRENT state; it
                # survives every stay and resets only on a change.
                "state_since": (
                    event_time if next_state != state.current_state else state.state_since
                ),
                "previous_position": obs.spatial_point,
                "previous_event_time": event_time,
                "last_seen": event_time,
                "watermark_event_time": event_time,
                "last_applied_frame_id": frame_id,
            }
        )
        return MovementResult(state=updated, measurement=measurement)

    def checkpoint(self, state: MovementState) -> MovementCheckpoint:
        """Serialize ``state`` into a versioned, resumable checkpoint."""
        return MovementCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision=self._policy.revision,
            state=state,
        )

    def restore(self, checkpoint: MovementCheckpoint) -> MovementState:
        """Restore a checkpoint, rejecting version/policy drift (typed)."""
        if not isinstance(checkpoint, MovementCheckpoint):
            raise InvalidTemporalInputError(
                f"checkpoint must be a MovementCheckpoint, got {type(checkpoint).__name__}"
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
        if checkpoint.state.key.fsm_kind != "movement":
            raise InvalidTemporalInputError(
                "checkpoint state key fsm_kind is not 'movement' — cross-FSM restore is rejected"
            )
        return checkpoint.state

    # ------------------------------------------------------------------
    # Validation (provenance + geometry boundary)
    # ------------------------------------------------------------------

    def _validate(self, state: MovementState, inp: MovementInput) -> None:
        if not isinstance(state, MovementState):
            raise InvalidTemporalInputError(
                f"state must be a MovementState, got {type(state).__name__}"
            )
        if not isinstance(inp, MovementInput):
            raise InvalidTemporalInputError(
                f"input must be a MovementInput, got {type(inp).__name__}"
            )
        obs = inp.observation
        if not isinstance(obs, SpatialObservation):
            raise InvalidTemporalInputError(
                "movement consumes canonical SpatialObservation positions only, "
                f"got {type(obs).__name__}"
            )
        if inp.key != state.key:
            raise InvalidTemporalInputError(
                "movement input key must match the state key (cross-track apply is rejected)"
            )
        if inp.key.fsm_kind != "movement":
            raise InvalidTemporalInputError(
                f"movement input key fsm_kind must be 'movement', got {inp.key.fsm_kind!r}"
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
        if obs.event_time.tzinfo is None:
            raise InvalidTemporalInputError("observation event_time must be timezone-aware UTC")
        self._check_key_matches_observation(inp.key, obs)
        # The canonical geometry boundary: reject non-finite/out-of-range
        # points even when they slipped past pydantic defaults.
        point = obs.spatial_point
        validate_coordinate(point.x, point.y, coordinate_space=point.coordinate_space)
        previous = state.previous_position
        if previous is not None:
            validate_coordinate(previous.x, previous.y, coordinate_space=previous.coordinate_space)
            if previous.coordinate_space != point.coordinate_space:
                raise InvalidTemporalInputError(
                    "previous and current positions must share a coordinate space "
                    "(mixing IMAGE_NORMALIZED and VENUE_LOCAL displacement is undefined)"
                )

    def _check_key_matches_observation(
        self, key: TemporalStateKey, obs: SpatialObservation
    ) -> None:
        """Provenance integrity: the per-track key and observation agree."""
        mismatches: list[str] = []
        if key.session_id != obs.session_id:
            mismatches.append("session_id")
        if key.track_id != obs.track_id:
            mismatches.append("track_id")
        if key.camera_id != obs.camera_id:
            mismatches.append("camera_id")
        if key.configuration_version_id != obs.configuration_version_id:
            mismatches.append("configuration_version_id")
        if mismatches:
            raise StateKeyMismatchError(
                f"movement state key does not match the observation provenance "
                f"({', '.join(mismatches)}); cross-scope evaluation is rejected"
            )

    # ------------------------------------------------------------------
    # Ordering (the 15.1 policy, per-track)
    # ------------------------------------------------------------------

    def _apply_out_of_order(
        self,
        state: MovementState,
        *,
        event_time: datetime,
        frame_id: FrameId,
    ) -> MovementResult:
        """Deterministic out-of-order policy (Task 15.1 §5/§6): window or reject."""
        assert state.watermark_event_time is not None
        delta = (state.watermark_event_time - event_time).total_seconds()
        if delta <= self._policy.reorder_window_seconds:
            # Within the allowed reordering window: accepted, NEVER rewinds
            # the measurement anchor, the classification, or the watermark;
            # refreshes last_seen only if newer (accept-with-no-rewind).
            last_seen = event_time
            if state.last_seen is not None and state.last_seen > event_time:
                last_seen = state.last_seen
            updated = state.model_copy(update={"last_seen": last_seen})
            return MovementResult(state=updated, reordered=True)
        raise LateEventError(
            f"observation event_time {event_time.isoformat()} is "
            f"{delta:.3f}s older than the watermark "
            f"{state.watermark_event_time.isoformat()}, beyond the reordering "
            f"window of {self._policy.reorder_window_seconds}s — rejected "
            "deterministically (never silently discarded or force-ordered)"
        )

    # ------------------------------------------------------------------
    # Measurement derivation (pure)
    # ------------------------------------------------------------------

    def _build_measurement(
        self,
        *,
        key: TemporalStateKey,
        previous_position: SpatialPointModel,
        previous_event_time: datetime,
        current_position: SpatialPointModel,
        event_time: datetime,
        distance: float,
        time_delta: float,
    ) -> MovementMeasurement:
        """One deterministic measurement fact for a consecutive pair."""
        measurement_id = EventId(
            uuid5(
                TEMPORAL_ID_NAMESPACE,
                self._measurement_identity(
                    key=key,
                    previous_event_time=previous_event_time,
                    event_time=event_time,
                    previous_position=previous_position,
                    current_position=current_position,
                ),
            )
        )
        return MovementMeasurement(
            measurement_id=measurement_id,
            fsm_kind=self._fsm.name,
            key=key,
            previous_position=previous_position,
            current_position=current_position,
            previous_event_time=previous_event_time,
            event_time=event_time,
            distance=distance,
            time_delta_seconds=time_delta,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )

    def _measurement_identity(
        self,
        *,
        key: TemporalStateKey,
        previous_event_time: datetime,
        event_time: datetime,
        previous_position: SpatialPointModel,
        current_position: SpatialPointModel,
    ) -> str:
        """Content-derived identity string for a measurement (deterministic)."""
        return "|".join([
            key.canonical(),
            previous_event_time.isoformat(),
            event_time.isoformat(),
            repr(previous_position.x),
            repr(previous_position.y),
            repr(current_position.x),
            repr(current_position.y),
            self._fsm.version,
            self._policy.revision,
        ])
