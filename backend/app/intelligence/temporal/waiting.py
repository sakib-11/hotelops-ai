"""Waiting FSM — deterministic waiting detection (Task 15.5.3).

Waiting is an OPERATIONAL temporal interpretation, never merely "not
moving": a tracked entity becomes WAITING only when it is confirmed
PRESENT (Task 15.2), inside a configured waiting-capable spatial context,
classified STATIONARY by Task 15.5.2, and stays that way for a configured
qualification duration of EVENT time. Architecture (Task 15.5.3):

    SpatialObservation
        ↓ Task 15.1 Temporal Foundation — event-time ordering, watermark,
          late/out-of-order policy, idempotent dedup, checkpoint/restore
    Enter/Exit FSM (Task 15.2, ``PresenceTemporalEngine``)
        ↓ presence TemporalTransition
    Movement Classification (Task 15.5.2, ``MovementClassificationEngine``)
        ↓ MovementClassificationState (current classification per track)
    WAITING FSM — this module (presence + movement + spatial context)
        ↓
    WaitingInterval (fact)

The waiting FSM is a SEPARATE family (fsm_kind="waiting") on the SAME
foundation discipline. Its input is one observation step in lockstep: the
presence transition AND the classification state AFTER that observation.
``waiting_event_from_presence`` is the ONLY sanctioned presence -> waiting
mapping, and the stationary signal is read verbatim from the 15.5.2
classification state — the waiting engine NEVER re-derives movement, exit
confirmation, or presence. It therefore inherits dedup, event-time
ordering, key/provenance integrity, isolation, and checkpointing without
duplicating any of it.

State model (§5 — minimal states; NO extra states):

    not_waiting --candidate_started--> waiting_candidate
    waiting_candidate --waiting_confirmed--> waiting
    waiting_candidate --candidate_aborted--> not_waiting
    waiting --waiting_ended--> not_waiting
    * --stay--> same state

Semantics:
  - WAITING is NOT STATIONARY (§3): STATIONARY is the 15.5.2 movement
    classification; WAITING additionally requires confirmed presence, a
    configured waiting-capable context, and the qualification duration.
    A stationary person in a lobby is STATIONARY, never WAITING.
  - Waiting context (§4): ``TemporalPolicy.waiting_contexts`` is the
    EXPLICIT set of waiting-capable ``semantic_context`` values (Task 10
    profile ids). Nothing is inferred — restaurant/lobby/hallway/table/
    entrance are never waiting contexts by themselves. Populate the set
    from a Task 10 configuration snapshot with
    ``waiting_contexts_from_configuration`` (queue areas, service areas,
    and ``ZoneType.WAITING_AREA`` zones only). An empty set means no
    context can ever produce WAITING.
  - Candidate start (§5/§6): the first observation at which confirmed
    presence + stationary + waiting context ALL hold starts the candidate;
    ``candidate_start`` is that EVENT time (never processing time).
  - Qualification (§7): the classification becomes WAITING only once the
    candidate remains (present + stationary + context) for
    ``waiting_qualification_seconds`` of event time. The confirming
    observation's event_time is ``waiting_start`` (never candidate_start,
    never wall clock). ``0.0`` disables the delay (first qualifying step
    confirms immediately).
  - Candidate cancellation (§8/§9): a MOVING classification (15.5.2
    confirmed) or a confirmed presence loss before qualification returns
    the candidate to NOT_WAITING with NO waiting fact. Leaving the
    waiting context is expressed through the presence FSM's confirmed
    exit (the context capability is pinned per key) — no second
    mechanism.
  - Waiting continuation (§11): while WAITING, an in-order present +
    stationary + context observation is a ``stay`` — no new fact per
    frame. ``open_interval`` exposes the RUNNING interval.
  - Waiting end (§12): confirmed exit / occlusion expiry / session
    closure / a 15.5.2 MOVING confirmation. Confirmed presence loss is
    authoritative over movement (an exiting entity's interval ends as a
    confirmed exit, not a movement exceed). Session closure follows the
    existing ``session_closed`` transition — no Task 15.6 logic here.
  - Short occlusion (§13): the presence FSM's grace/occlusion policy is
    reused verbatim — a short gap keeps PRESENT (TEMPORARILY_MISSING,
    ``stay`` here), so waiting is preserved; only a confirmed
    ``missing_expired`` ends it.
  - Re-entry (§17): each confirmed entry (after a confirmed exit) opens a
    NEW candidate and a NEW interval; intervals are never merged. The
    interval id binds waiting_start (+ waiting_end when closed), so two
    intervals of the same track/context are distinct facts.
  - Movement is NOT re-derived (§2): the stationary signal is the 15.5.2
    classification state's ``current_state``. A mis-wired classification
    (wrong family key, invalid state value) is rejected explicitly.

Wiring (pure and deterministic — the coordinator feeds all engines the
same observations in the same order):

    presence_result = presence_engine.apply(presence_state, inp)
    movement_result = movement_engine.apply(movement_state, m_inp)
    classification_state = classification_engine.apply(
        classification_state, classification_input_from_movement(...)
    ).state
    waiting_kind = waiting_event_from_presence(presence_result.transitions[0])
    waiting_result = waiting_engine.apply(
        waiting_state,
        WaitingInput(
            key=waiting_key,
            presence_transition=presence_result.transitions[0],
            classification_state=classification_state,
            observation_kind=waiting_kind,
            processing_time=inp.processing_time,
        ),
    )

``WaitingEngine`` performs NO I/O and reads no current time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid5

from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    LateEventError,
    StateKeyMismatchError,
)
from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule
from contracts.common import EventId, FrameId
from contracts.configuration import ConfigurationVersionModel, ZoneType
from contracts.temporal import (
    MOVEMENT_STATES,
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    WAITING_STATES,
    MovementClassificationState,
    TemporalPolicy,
    TemporalReason,
    TemporalStateKey,
    TemporalTransition,
    WaitingCheckpoint,
    WaitingInterval,
    WaitingState,
)

__all__ = [
    "WAITING_FSM",
    "WaitingEngine",
    "WaitingInput",
    "WaitingResult",
    "waiting_contexts_from_configuration",
    "waiting_event_from_presence",
]

WAITING_FSM = DeterministicFsm(
    name="waiting",
    version=TEMPORAL_ENGINE_VERSION,
    states=WAITING_STATES,
    initial_state="not_waiting",
    rules=(
        FsmRule(from_state="not_waiting", event="candidate_started", to_state="waiting_candidate"),
        FsmRule(from_state="not_waiting", event="stay", to_state="not_waiting"),
        FsmRule(from_state="waiting_candidate", event="waiting_confirmed", to_state="waiting"),
        FsmRule(from_state="waiting_candidate", event="candidate_aborted", to_state="not_waiting"),
        FsmRule(from_state="waiting_candidate", event="stay", to_state="waiting_candidate"),
        FsmRule(from_state="waiting", event="waiting_ended", to_state="not_waiting"),
        FsmRule(from_state="waiting", event="stay", to_state="waiting"),
    ),
)

# Presence kinds that CLOSE an open waiting interval (confirmed loss).
_PRESENCE_LOST_KINDS = ("exit_confirmed", "missing_expired", "session_closed")


def waiting_event_from_presence(transition: TemporalTransition) -> str:
    """Map one presence transition to its waiting observation kind.

    This is the ONLY sanctioned presence -> waiting mapping (deterministic,
    mirroring ``dwell_event_from_presence`` — presence CONFIRMATION is
    authoritative; the intermediate ENTERING/EXITING states are never
    re-derived here):
      - ENTER_CONFIRMED   -> ``enter_confirmed``  (confirmed PRESENT)
      - EXIT_CONFIRMED    -> ``exit_confirmed``   (confirmed ABSENT)
      - MISSING_EXPIRED   -> ``missing_expired``  (occlusion gap expired)
      - SESSION_CLOSED    -> ``session_closed``   (explicit closure)
      - OBSERVED_STAY / DEDUPLICATED / REORDERED -> ``stay`` — the waiting
        engine reproduces the same dedup/reorder deterministically from
        the position.
    """
    if transition.reason is TemporalReason.ENTER_CONFIRMED:
        return "enter_confirmed"
    if transition.reason is TemporalReason.EXIT_CONFIRMED:
        return "exit_confirmed"
    if transition.reason is TemporalReason.MISSING_EXPIRED:
        return "missing_expired"
    if transition.reason is TemporalReason.SESSION_CLOSED:
        return "session_closed"
    return "stay"


def waiting_contexts_from_configuration(version: ConfigurationVersionModel) -> frozenset[str]:
    """The EXPLICIT waiting-capable contexts of a Task 10 configuration.

    Deterministic and non-inferred (§4): a context (version-owned profile
    id, the ``TemporalStateKey.semantic_context`` value) is waiting-capable
    ONLY when the configuration snapshot declares it as a queue area, a
    service area, or a ``ZoneType.WAITING_AREA`` zone. Restaurant, lobby,
    hallway, table, and entrance profiles are NEVER waiting contexts by
    themselves — the venue operator must declare a waiting context.
    """
    contexts: set[str] = set()
    for zone in version.zones:
        if zone.zone_type is ZoneType.WAITING_AREA:
            contexts.add(zone.profile_id)
    contexts.update(queue.profile_id for queue in version.queue_areas)
    contexts.update(service.profile_id for service in version.service_areas)
    return frozenset(contexts)


@dataclass(frozen=True, slots=True)
class WaitingInput:
    """Pure-engine input: waiting key + one lockstep presence/classification step.

    ``presence_transition`` is the presence engine's transition for this
    observation; ``classification_state`` is the 15.5.2 classification
    state AFTER this observation (the stationary signal is read from its
    ``current_state`` — never re-derived). ``observation_kind`` is derived
    via ``waiting_event_from_presence`` (re-validated). ``processing_time``
    is metadata only — ordering ALWAYS uses the transition's event-time
    position.
    """

    key: TemporalStateKey
    presence_transition: TemporalTransition
    classification_state: MovementClassificationState
    observation_kind: str
    processing_time: datetime


@dataclass(frozen=True, slots=True)
class WaitingResult:
    """Deterministic result of applying one lockstep presence/classification step."""

    state: WaitingState
    # One closed WaitingInterval whenever a confirmed WAITING ended; None
    # for stays, candidate changes, deduplicated, and reordered inputs.
    interval: WaitingInterval | None = None
    deduplicated: bool = False
    reordered: bool = False


class WaitingEngine:
    """Pure per-track waiting interpreter over presence + classification.

    A standalone deterministic engine (the waiting state shape —
    classification + candidate_start + waiting_start — differs from the
    per-entity ``TemporalState``, so it is not a ``TemporalEngine``
    subclass) that REUSES the foundation's discipline wholesale: the same
    ``TemporalPolicy`` (reorder window, revision, waiting knobs), the same
    single-watermark ordering, the same typed error taxonomy, the same
    versioned checkpoint, and the same content-derived fact identities.
    It NEVER computes movement or presence itself.
    """

    OBSERVATION_KINDS: tuple[str, ...] = (
        "enter_confirmed",
        "exit_confirmed",
        "missing_expired",
        "session_closed",
        "stay",
    )

    def __init__(
        self,
        *,
        fsm: DeterministicFsm = WAITING_FSM,
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

    def initial_state(self, key: TemporalStateKey) -> WaitingState:
        """The pristine per-track classification: NOT_WAITING, no evidence."""
        if not isinstance(key, TemporalStateKey):
            raise InvalidTemporalInputError(
                f"key must be a TemporalStateKey, got {type(key).__name__}"
            )
        if key.fsm_kind != "waiting":
            raise InvalidTemporalInputError(
                f"waiting state key fsm_kind must be 'waiting', got {key.fsm_kind!r}"
            )
        return WaitingState(fsm_version=self._fsm.version, key=key)

    def apply(self, state: WaitingState, inp: WaitingInput) -> WaitingResult:
        """Apply one lockstep presence/classification step (pure, deterministic).

        Raises the typed ``TemporalError`` taxonomy on any failure; a
        failure is never encoded as a state or an interval.
        """
        self._validate(state, inp)
        transition = inp.presence_transition
        event_time = transition.event_time
        frame_id = transition.observation_frame_id
        position = (event_time, frame_id)
        watermark = (state.watermark_event_time, state.last_applied_frame_id)

        if watermark[0] is not None:
            if position == watermark:
                return WaitingResult(state=state, deduplicated=True)
            if position < watermark:
                return self._apply_out_of_order(state, event_time=event_time, frame_id=frame_id)

        next_state, candidate_start, waiting_start, interval, presence_confirmed = self._evaluate(
            state,
            kind=inp.observation_kind,
            event_time=event_time,
            stationary=inp.classification_state.current_state == "stationary",
        )
        updated = state.model_copy(
            update={
                "current_state": next_state,
                "presence_confirmed": presence_confirmed,
                "candidate_start": candidate_start,
                "waiting_start": waiting_start,
                "last_seen": event_time,
                "watermark_event_time": event_time,
                "last_applied_frame_id": frame_id,
            }
        )
        return WaitingResult(state=updated, interval=interval)

    def open_interval(self, state: WaitingState) -> WaitingInterval | None:
        """The RUNNING confirmed-waiting interval, or None when not WAITING.

        ``waiting_end`` is None; ``last_seen`` is the most recent accepted
        observation event_time. The interval id is stable while open
        (derived from key + waiting_start only), so it does not change as
        ``last_seen`` advances.
        """
        if state.current_state != "waiting":
            return None
        waiting_start = state.waiting_start
        if waiting_start is None:
            raise InvalidTemporalInputError(
                "corrupted waiting state: waiting without a waiting_start "
                "(the interval was never confirmed by a qualification completion)"
            )
        last_seen = state.last_seen if state.last_seen is not None else waiting_start
        duration = (last_seen - waiting_start).total_seconds()
        return WaitingInterval(
            interval_id=EventId(
                uuid5(
                    TEMPORAL_ID_NAMESPACE,
                    self._interval_identity(
                        key=state.key, waiting_start=waiting_start, is_open=True
                    ),
                )
            ),
            fsm_kind=self._fsm.name,
            key=state.key,
            waiting_start=waiting_start,
            waiting_end=None,
            last_seen=last_seen,
            duration_seconds=duration,
            qualified=duration >= self._policy.waiting_qualification_seconds,
            minimum_waiting_seconds=self._policy.waiting_qualification_seconds,
            reason=None,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )

    def checkpoint(self, state: WaitingState) -> WaitingCheckpoint:
        """Serialize ``state`` into a versioned, resumable checkpoint."""
        return WaitingCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision=self._policy.revision,
            state=state,
        )

    def restore(self, checkpoint: WaitingCheckpoint) -> WaitingState:
        """Restore a checkpoint, rejecting version/policy drift (typed)."""
        if not isinstance(checkpoint, WaitingCheckpoint):
            raise InvalidTemporalInputError(
                f"checkpoint must be a WaitingCheckpoint, got {type(checkpoint).__name__}"
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
        if checkpoint.state.key.fsm_kind != "waiting":
            raise InvalidTemporalInputError(
                "checkpoint state key fsm_kind is not 'waiting' — cross-FSM restore is rejected"
            )
        return checkpoint.state

    # ------------------------------------------------------------------
    # Validation (provenance + input integrity)
    # ------------------------------------------------------------------

    def _validate(self, state: WaitingState, inp: WaitingInput) -> None:
        if not isinstance(state, WaitingState):
            raise InvalidTemporalInputError(
                f"state must be a WaitingState, got {type(state).__name__}"
            )
        if not isinstance(inp, WaitingInput):
            raise InvalidTemporalInputError(
                f"input must be a WaitingInput, got {type(inp).__name__}"
            )
        if inp.key != state.key:
            raise InvalidTemporalInputError(
                "waiting input key must match the state key (cross-track apply is rejected)"
            )
        if inp.key.fsm_kind != "waiting":
            raise InvalidTemporalInputError(
                f"waiting input key fsm_kind must be 'waiting', got {inp.key.fsm_kind!r}"
            )
        if state.fsm_version != self._fsm.version:
            raise FsmVersionMismatchError(
                f"state FSM version {state.fsm_version!r} does not match FSM "
                f"'{self._fsm.name}' version {self._fsm.version!r}"
            )
        transition = inp.presence_transition
        if not isinstance(transition, TemporalTransition):
            raise InvalidTemporalInputError(
                f"presence_transition must be a TemporalTransition, got {type(transition).__name__}"
            )
        if transition.key.fsm_kind != "presence":
            raise InvalidTemporalInputError(
                f"waiting consumes presence-family transitions, got transition "
                f"fsm_kind {transition.key.fsm_kind!r}"
            )
        classification = inp.classification_state
        if not isinstance(classification, MovementClassificationState):
            raise InvalidTemporalInputError(
                "classification_state must be a MovementClassificationState, got "
                f"{type(classification).__name__}"
            )
        if classification.key.fsm_kind != "movement_classification":
            raise InvalidTemporalInputError(
                "waiting consumes 15.5.2 classification states, got classification "
                f"fsm_kind {classification.key.fsm_kind!r}"
            )
        if classification.current_state not in MOVEMENT_STATES:
            raise InvalidTemporalInputError(
                f"classification current_state must be one of "
                f"{', '.join(MOVEMENT_STATES)}, got {classification.current_state!r}"
            )
        if inp.observation_kind not in self.OBSERVATION_KINDS:
            raise InvalidTemporalInputError(
                f"unknown observation_kind {inp.observation_kind!r} for FSM "
                f"'{self._fsm.name}'; allowed: {', '.join(self.OBSERVATION_KINDS)}"
            )
        if waiting_event_from_presence(transition) != inp.observation_kind:
            raise InvalidTemporalInputError(
                "observation_kind does not match the presence transition reason "
                "(derive the kind with waiting_event_from_presence)"
            )
        if inp.processing_time.tzinfo is None:
            raise InvalidTemporalInputError(
                "processing_time must be timezone-aware UTC (metadata only, "
                "never used for ordering)"
            )
        if transition.event_time.tzinfo is None:
            raise InvalidTemporalInputError("transition event_time must be timezone-aware UTC")
        self._check_key_matches_transition(inp.key, transition.key)
        self._check_key_matches_classification(inp.key, classification.key)

    def _check_key_matches_transition(
        self, waiting_key: TemporalStateKey, transition_key: TemporalStateKey
    ) -> None:
        """Provenance integrity: the waiting key and the presence transition
        must agree on every scope component (fsm_kind differs by design —
        presence vs waiting). Cross-session/tenant/venue/camera/config/
        track/context waiting is impossible — explicit rejection."""
        mismatches: list[str] = []
        if waiting_key.tenant_id != transition_key.tenant_id:
            mismatches.append("tenant_id")
        if waiting_key.venue_id != transition_key.venue_id:
            mismatches.append("venue_id")
        if waiting_key.session_id != transition_key.session_id:
            mismatches.append("session_id")
        if waiting_key.camera_id != transition_key.camera_id:
            mismatches.append("camera_id")
        if waiting_key.configuration_version_id != transition_key.configuration_version_id:
            mismatches.append("configuration_version_id")
        if waiting_key.track_id != transition_key.track_id:
            mismatches.append("track_id")
        if waiting_key.semantic_context != transition_key.semantic_context:
            mismatches.append("semantic_context")
        if mismatches:
            raise StateKeyMismatchError(
                f"waiting key does not match the presence transition provenance "
                f"({', '.join(mismatches)}); cross-scope waiting is rejected"
            )

    def _check_key_matches_classification(
        self, waiting_key: TemporalStateKey, classification_key: TemporalStateKey
    ) -> None:
        """Provenance integrity: the waiting key and the 15.5.2 classification
        state must agree on every scope component (fsm_kind differs by
        design — movement_classification vs waiting)."""
        mismatches: list[str] = []
        if waiting_key.tenant_id != classification_key.tenant_id:
            mismatches.append("tenant_id")
        if waiting_key.venue_id != classification_key.venue_id:
            mismatches.append("venue_id")
        if waiting_key.session_id != classification_key.session_id:
            mismatches.append("session_id")
        if waiting_key.camera_id != classification_key.camera_id:
            mismatches.append("camera_id")
        if waiting_key.configuration_version_id != classification_key.configuration_version_id:
            mismatches.append("configuration_version_id")
        if waiting_key.track_id != classification_key.track_id:
            mismatches.append("track_id")
        if waiting_key.semantic_context != classification_key.semantic_context:
            mismatches.append("semantic_context")
        if mismatches:
            raise StateKeyMismatchError(
                f"waiting key does not match the movement classification provenance "
                f"({', '.join(mismatches)}); cross-scope waiting is rejected"
            )

    # ------------------------------------------------------------------
    # Ordering (the 15.1 policy, per-track)
    # ------------------------------------------------------------------

    def _apply_out_of_order(
        self,
        state: WaitingState,
        *,
        event_time: datetime,
        frame_id: FrameId,
    ) -> WaitingResult:
        """Deterministic out-of-order policy (15.1 §5/§6): window or reject."""
        assert state.watermark_event_time is not None
        delta = (state.watermark_event_time - event_time).total_seconds()
        if delta <= self._policy.reorder_window_seconds:
            # Within the allowed reordering window: accepted, NEVER rewinds
            # the classification, the candidate, or the watermark; refreshes
            # last_seen only if newer (accept-with-no-rewind).
            last_seen = event_time
            if state.last_seen is not None and state.last_seen > event_time:
                last_seen = state.last_seen
            updated = state.model_copy(update={"last_seen": last_seen})
            return WaitingResult(state=updated, reordered=True)
        raise LateEventError(
            f"presence transition event_time {event_time.isoformat()} is "
            f"{delta:.3f}s older than the waiting watermark "
            f"{state.watermark_event_time.isoformat()}, beyond the reordering "
            f"window of {self._policy.reorder_window_seconds}s — rejected "
            "deterministically (never silently discarded or force-ordered)"
        )

    # ------------------------------------------------------------------
    # Waiting semantics (presence + movement + context, pure)
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        state: WaitingState,
        *,
        kind: str,
        event_time: datetime,
        stationary: bool,
    ) -> tuple[str, datetime | None, datetime | None, WaitingInterval | None, bool]:
        """One deterministic waiting step for a lockstep input.

        Returns (next_state, candidate_start, waiting_start, closed_interval,
        presence_confirmed) — the caller materializes the state.
        Presence-loss confirmation is evaluated FIRST (an exiting entity's
        interval ends as a confirmed exit, never as a movement exceed),
        then movement, then confirmed presence, then the context capability
        (pinned per key).
        """
        fsm = self._fsm
        current = state.current_state

        # Confirmed-presence bookkeeping for THIS step (§2.1): an entry
        # confirms, confirmed loss clears, a stay preserves.
        if kind == "enter_confirmed":
            presence_confirmed = True
        elif kind in _PRESENCE_LOST_KINDS:
            presence_confirmed = False
        else:  # stay
            presence_confirmed = state.presence_confirmed

        # --- Confirmed presence loss or session end (§12) ---
        if kind in _PRESENCE_LOST_KINDS:
            if current == "waiting":
                reason = {
                    "exit_confirmed": TemporalReason.EXIT_CONFIRMED,
                    "missing_expired": TemporalReason.MISSING_EXPIRED,
                    "session_closed": TemporalReason.SESSION_CLOSED,
                }[kind]
                next_state = fsm.transition("waiting", "waiting_ended")
                interval = self._build_closed_interval(
                    key=state.key,
                    waiting_start=state.waiting_start,
                    waiting_end=event_time,
                    reason=reason,
                )
                return next_state, None, None, interval, False
            if current == "waiting_candidate":
                # Presence lost before qualification: no waiting fact.
                next_state = fsm.transition("waiting_candidate", "candidate_aborted")
                return next_state, None, None, None, False
            # Already NOT_WAITING: a confirmed absence is a benign stay.
            return current, None, None, None, False

        # --- Movement: a 15.5.2 MOVING confirmation cancels/ends (§8/§12) ---
        if not stationary:
            if current == "waiting":
                next_state = fsm.transition("waiting", "waiting_ended")
                interval = self._build_closed_interval(
                    key=state.key,
                    waiting_start=state.waiting_start,
                    waiting_end=event_time,
                    reason=TemporalReason.MOVEMENT_EXCEEDED,
                )
                return next_state, None, None, interval, presence_confirmed
            if current == "waiting_candidate":
                next_state = fsm.transition("waiting_candidate", "candidate_aborted")
                return next_state, None, None, None, presence_confirmed
            # Moving in the zone never starts waiting (§26).
            return current, None, None, None, presence_confirmed

        # --- Confirmed presence is REQUIRED for waiting (§2.1) ---
        if not presence_confirmed:
            # The entity was never confirmed PRESENT to this engine (or the
            # confirmed loss already cleared it): a stationary stay never
            # starts a candidate on its own.
            return current, None, None, None, False

        # --- Waiting context capability (pinned per key, §4) ---
        context_ok = self._context_ok(state.key)
        if not context_ok:
            if current != "not_waiting":
                raise InvalidTemporalInputError(
                    "waiting/candidate state under a context not declared "
                    "waiting-capable by the pinned policy — corrupted state"
                )
            return current, None, None, None, presence_confirmed

        # --- Stationary + confirmed presence + waiting context ---
        if kind == "enter_confirmed":
            # A confirmed entry is only legal from NOT_WAITING (an in-order
            # presence stream always confirms the exit first) — the FSM
            # raises InvalidTransitionError from candidate/waiting, never
            # silent. Start the qualification run at the confirmation
            # event-time; with ``waiting_qualification_seconds == 0.0`` the
            # very first qualifying step confirms immediately
            # (candidate_start == waiting_start — the degenerate
            # single-step policy, mirroring the 15.5.2
            # ``movement_qualification_seconds == 0.0`` rule).
            next_state = fsm.transition(current, "candidate_started")
            if self._policy.waiting_qualification_seconds == 0:
                next_state = fsm.transition(next_state, "waiting_confirmed")
                return next_state, event_time, event_time, None, presence_confirmed
            return next_state, event_time, None, None, presence_confirmed

        # kind == "stay" from here (the only other legal kind once
        # stationary + presence confirmed + waiting context all hold).
        if current == "not_waiting":
            # The candidate may start on a stay: the entry was confirmed
            # earlier (e.g. while the entity was still moving in). With a
            # zero qualification duration the same stay confirms.
            if self._policy.waiting_qualification_seconds == 0:
                next_state = fsm.transition("not_waiting", "candidate_started")
                next_state = fsm.transition(next_state, "waiting_confirmed")
                return next_state, event_time, event_time, None, presence_confirmed
            next_state = fsm.transition("not_waiting", "candidate_started")
            return next_state, event_time, None, None, presence_confirmed
        if current == "waiting_candidate":
            if state.candidate_start is None:
                raise InvalidTemporalInputError(
                    "corrupted waiting state: candidate without a candidate_start "
                    "(the run was never anchored to a qualifying event-time)"
                )
            elapsed = (event_time - state.candidate_start).total_seconds()
            if elapsed >= self._policy.waiting_qualification_seconds:
                # Qualification satisfied: WAITING confirmed at THIS
                # observation's event_time (§6 — never candidate_start).
                next_state = fsm.transition("waiting_candidate", "waiting_confirmed")
                return next_state, state.candidate_start, event_time, None, presence_confirmed
            return (
                fsm.transition("waiting_candidate", "stay"),
                state.candidate_start,
                None,
                None,
                presence_confirmed,
            )
        # current == "waiting": continuation (§11) — no new fact per frame.
        return (
            fsm.transition("waiting", "stay"),
            state.candidate_start,
            state.waiting_start,
            None,
            presence_confirmed,
        )

    def _context_ok(self, key: TemporalStateKey) -> bool:
        """The key's spatial context is declared waiting-capable by the policy.

        Static per key (the policy is pinned to the session and the
        semantic_context is part of the key) — a context that is not
        waiting-capable can never produce a candidate.
        """
        return (
            key.semantic_context is not None
            and key.semantic_context in self._policy.waiting_contexts
        )

    # ------------------------------------------------------------------
    # Interval derivation (pure, content-derived identity)
    # ------------------------------------------------------------------

    def _build_closed_interval(
        self,
        *,
        key: TemporalStateKey,
        waiting_start: datetime | None,
        waiting_end: datetime,
        reason: TemporalReason,
    ) -> WaitingInterval:
        """A closed interval fact from the waiting state at termination."""
        if waiting_start is None:
            raise InvalidTemporalInputError(
                "corrupted waiting state: interval closing without a waiting_start "
                "(the interval was never confirmed by a qualification completion)"
            )
        duration = (waiting_end - waiting_start).total_seconds()
        return WaitingInterval(
            interval_id=EventId(
                uuid5(
                    TEMPORAL_ID_NAMESPACE,
                    self._interval_identity(
                        key=key, waiting_start=waiting_start, waiting_end=waiting_end
                    ),
                )
            ),
            fsm_kind=self._fsm.name,
            key=key,
            waiting_start=waiting_start,
            waiting_end=waiting_end,
            last_seen=waiting_end,
            duration_seconds=duration,
            qualified=duration >= self._policy.waiting_qualification_seconds,
            minimum_waiting_seconds=self._policy.waiting_qualification_seconds,
            reason=reason,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )

    def _interval_identity(
        self,
        *,
        key: TemporalStateKey,
        waiting_start: datetime,
        waiting_end: datetime | None = None,
        is_open: bool = False,
    ) -> str:
        """Content-derived identity string for an interval (deterministic).

        Open intervals derive the id from key + waiting_start only, so the
        id is stable while the interval runs; closed intervals additionally
        bind the waiting_end — two re-entry intervals are therefore distinct
        facts (§17).
        """
        if is_open:
            return "|".join([
                key.canonical(),
                "open",
                waiting_start.isoformat(),
                self._fsm.version,
                self._policy.revision,
            ])
        if waiting_end is None:
            raise InvalidTemporalInputError("closed interval identity requires waiting_end")
        return "|".join([
            key.canonical(),
            "closed",
            waiting_start.isoformat(),
            waiting_end.isoformat(),
            self._fsm.version,
            self._policy.revision,
        ])
