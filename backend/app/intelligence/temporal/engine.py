"""Deterministic temporal engine foundation (Task 15 Step 1).

Consumes canonical Task 14 observations and converts them into stable,
checkpointable time-based state under an explicit event-time discipline.

    SpatialObservation
        ↓ TemporalInput (key + observation + kind + processing_time)
    TemporalEngine
        ├── input/provenance validation (key vs observation)
        ├── event-time ordering  (event_time is authoritative; watermark)
        ├── late/out-of-order policy (configurable reorder window)
        ├── idempotency         (same observation applied once)
        ├── FSM semantics       (hysteresis / occlusion / grace)
        └── checkpoint/restore  (serializable, versioned)
        ↓
    TemporalResult (state + deterministic transition history)

PURE CORE (Task 15 §17): no PostgreSQL, Redis, S3, HTTP, FastAPI, or LLM
calls, and NO current-time reads — ``processing_time`` is caller-supplied
metadata and is NEVER used for ordering. Persistence is the caller's
boundary: ``checkpoint()`` returns a serializable ``TemporalCheckpoint``
and ``restore()`` validates versions before resuming.

Event-time policy (Task 15 §3/§4/§5/§6):
  - Ordering uses ``(event_time, frame_id)`` — never processing order,
    database insertion order, or current system time.
  - A duplicate of the last applied position -> DEDUPLICATED (no FSM
    advance; the same transition identity would be reproduced).
  - A position older than the watermark is OUT-OF-ORDER:
      * within ``policy.reorder_window_seconds`` -> accepted with a
        REORDERED fact: refreshes ``last_seen`` if newer, and NEVER
        rewinds state, counters, or the watermark. This is the
        documented "accept-with-no-rewind" policy for this foundation:
        the late observation is acknowledged deterministically but its
        FSM semantics are NOT applied (a true replay-based reorder that
        reinterprets state is future work and must never be silent).
        Because reorders do not advance ``last_applied_frame_id``,
        replaying the same reordered observation reproduces the same
        content-derived REORDERED identity (never DEDUPLICATED).
      * older -> ``LateEventError`` (deterministic typed rejection —
        never silently discarded, never forced into event order).
  - ``session_closed`` finalizes the FSM explicitly.

Isolation (Task 15 §9/§16/§17/§18): each ``TemporalStateKey`` is an
independent state machine; the engine verifies the key's
session/track/camera/configuration-version match the observation and
raises ``StateKeyMismatchError`` otherwise. Tenant/venue are
authoritative on the key (the caller's authorized boundary) and are part
of the canonical identity — states never mix across scopes.

The base engine implements the temporal discipline; FSM-specific
semantics (how observations map to FSM events) live in ``_evaluate`` and
are provided by subclasses (``PresenceTemporalEngine`` here; future
dwell/occupancy/queue FSMs override it).
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
from backend.app.intelligence.temporal.fsm import DeterministicFsm
from contracts.common import EventId, FrameId
from contracts.spatial import LineCrossingObservation, SpatialObservation
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    DwellInterval,
    TemporalCheckpoint,
    TemporalOcclusionState,
    TemporalPolicy,
    TemporalReason,
    TemporalState,
    TemporalStateKey,
    TemporalTransition,
)

__all__ = [
    "Evaluation",
    "PresenceTemporalEngine",
    "TemporalEngine",
    "TemporalInput",
    "TemporalResult",
]

_OBSERVATION_TYPES = (SpatialObservation, LineCrossingObservation)


def _observation_position(
    obs: SpatialObservation | LineCrossingObservation,
) -> tuple[datetime, FrameId]:
    """The canonical event-time position of one observation.

    SpatialObservation anchors on its own (event_time, frame_id); a
    LineCrossingObservation anchors on the CURRENT observation of the
    transition (current_event_time, current_frame_id). This is the ONLY
    place observation types are translated into a position — ordering
    never branches elsewhere.
    """
    if isinstance(obs, SpatialObservation):
        return (obs.event_time, obs.frame_id)
    return (obs.current_event_time, obs.current_frame_id)


@dataclass(frozen=True, slots=True)
class TemporalInput:
    """Pure-engine input: state key + canonical observation + kind + metadata.

    ``observation_kind`` is the deterministic observation classification
    for the FSM family (e.g. ``presence_kind`` yields
    present/absent/not_observed). ``processing_time`` is metadata only —
    event ordering ALWAYS uses ``observation.event_time``.
    """

    key: TemporalStateKey
    observation: SpatialObservation | LineCrossingObservation
    observation_kind: str
    processing_time: datetime


@dataclass(frozen=True, slots=True)
class TemporalResult:
    """Deterministic result of applying one observation."""

    state: TemporalState
    transitions: tuple[TemporalTransition, ...]
    deduplicated: bool = False
    # Family-specific facts emitted by this step (e.g. one closed
    # ``DwellInterval`` from the dwell FSM); empty for families that
    # emit none.
    dwell_intervals: tuple[DwellInterval, ...] = ()


@dataclass(frozen=True, slots=True)
class Evaluation:
    """FSM-semantics outcome computed by ``_evaluate`` (subclass hook)."""

    state: str
    reason: TemporalReason
    present_seen: bool = False
    state_since: datetime | None = None
    entry_confirm_count: int = 0
    exit_confirm_count: int = 0
    occlusion_state: TemporalOcclusionState = TemporalOcclusionState.NOT_OBSERVED
    missing_since: datetime | None = None


class TemporalEngine:
    """Pure temporal discipline: validation, ordering, dedup, checkpoint.

    Subclasses implement ``_evaluate`` to map observation kinds to FSM
    events using their policy semantics, and declare the ``observation
    kinds`` they accept. The base class is fully deterministic and
    performs no I/O. Every input path (in-order, dedup, reorder) is
    validated uniformly BEFORE the ordering branches, so an unknown kind
    or a state from a different FSM family is always rejected.
    """

    def __init__(
        self,
        *,
        fsm: DeterministicFsm,
        policy: TemporalPolicy,
        observation_kinds: tuple[str, ...] = (),
    ) -> None:
        self._fsm = fsm
        self._policy = policy
        self._observation_kinds = observation_kinds

    @property
    def fsm(self) -> DeterministicFsm:
        return self._fsm

    @property
    def policy(self) -> TemporalPolicy:
        return self._policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initial_state(self, key: TemporalStateKey) -> TemporalState:
        """The pristine state for ``key`` (deterministic)."""
        if not isinstance(key, TemporalStateKey):
            raise InvalidTemporalInputError(
                f"key must be a TemporalStateKey, got {type(key).__name__}"
            )
        return TemporalState(
            fsm_version=self._fsm.version,
            key=key,
            current_state=self._fsm.initial_state,
        )

    def apply(self, state: TemporalState, inp: TemporalInput) -> TemporalResult:
        """Apply one canonical observation to ``state`` (pure, deterministic).

        Raises the typed ``TemporalError`` taxonomy on any failure; a
        failure is never encoded as a state or a transition.
        """
        self._validate(state, inp)
        obs = inp.observation
        kind = inp.observation_kind
        event_time, frame_id = _observation_position(obs)
        position = (event_time, frame_id)
        watermark = (state.watermark_event_time, state.last_applied_frame_id)

        if watermark[0] is not None:
            if position == watermark:
                transition = self._build_transition(
                    key=inp.key,
                    frame_id=frame_id,
                    event_time=event_time,
                    kind=kind,
                    from_state=state.current_state,
                    to_state=state.current_state,
                    reason=TemporalReason.DEDUPLICATED,
                    processing_time=inp.processing_time,
                )
                return TemporalResult(state=state, transitions=(transition,), deduplicated=True)
            if position < watermark:
                return self._apply_out_of_order(state, inp)

        evaluation = self._evaluate(state, kind=kind, event_time=event_time, obs=obs)

        transition = self._build_transition(
            key=inp.key,
            frame_id=frame_id,
            event_time=event_time,
            kind=kind,
            from_state=state.current_state,
            to_state=evaluation.state,
            reason=evaluation.reason,
            processing_time=inp.processing_time,
        )
        updated = self._apply_evaluation(
            state, evaluation, inp, transition, event_time=event_time, frame_id=frame_id
        )
        return TemporalResult(state=updated, transitions=(transition,))

    def checkpoint(self, state: TemporalState) -> TemporalCheckpoint:
        """Serialize ``state`` into a versioned, resumable checkpoint."""
        return TemporalCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision=self._policy.revision,
            state=state,
        )

    def restore(self, checkpoint: TemporalCheckpoint) -> TemporalState:
        """Restore a checkpoint, rejecting version/policy drift (typed)."""
        if not isinstance(checkpoint, TemporalCheckpoint):
            raise InvalidTemporalInputError(
                f"checkpoint must be a TemporalCheckpoint, got {type(checkpoint).__name__}"
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
        if checkpoint.state.key.fsm_kind != self._fsm.name:
            raise InvalidTemporalInputError(
                f"checkpoint state key fsm_kind {checkpoint.state.key.fsm_kind!r} does "
                f"not match engine FSM '{self._fsm.name}' — cross-FSM restore is rejected"
            )
        return checkpoint.state

    # ------------------------------------------------------------------
    # Discipline (base)
    # ------------------------------------------------------------------

    def _validate(self, state: TemporalState, inp: TemporalInput) -> None:
        if not isinstance(state, TemporalState):
            raise InvalidTemporalInputError(
                f"state must be a TemporalState, got {type(state).__name__}"
            )
        if not isinstance(inp, TemporalInput):
            raise InvalidTemporalInputError(
                f"input must be a TemporalInput, got {type(inp).__name__}"
            )
        obs = inp.observation
        if not isinstance(obs, _OBSERVATION_TYPES):
            raise InvalidTemporalInputError(
                "observation must be a canonical SpatialObservation or "
                f"LineCrossingObservation, got {type(obs).__name__}"
            )
        if not isinstance(inp.observation_kind, str) or not inp.observation_kind:
            raise InvalidTemporalInputError("observation_kind is required")
        # Kind membership and FSM-family consistency are validated HERE,
        # before the ordering/dedup branches, so every input path rejects
        # them identically (never a silent fact for a bad kind).
        if self._observation_kinds and inp.observation_kind not in self._observation_kinds:
            raise InvalidTemporalInputError(
                f"unknown observation_kind {inp.observation_kind!r} for FSM "
                f"'{self._fsm.name}'; allowed: {', '.join(self._observation_kinds)}"
            )
        if state.key.fsm_kind != self._fsm.name:
            raise InvalidTemporalInputError(
                f"state key fsm_kind {state.key.fsm_kind!r} does not match engine "
                f"FSM '{self._fsm.name}' — cross-FSM state is rejected"
            )
        if inp.processing_time.tzinfo is None:
            raise InvalidTemporalInputError(
                "processing_time must be timezone-aware UTC (metadata only, "
                "never used for ordering)"
            )
        event_time, _ = _observation_position(obs)
        if event_time.tzinfo is None:
            raise InvalidTemporalInputError("observation event_time must be timezone-aware UTC")
        if state.fsm_version != self._fsm.version:
            raise FsmVersionMismatchError(
                f"state FSM version {state.fsm_version!r} does not match FSM "
                f"'{self._fsm.name}' version {self._fsm.version!r}"
            )
        self._check_key_matches_observation(inp.key, obs)

    def _check_key_matches_observation(
        self,
        key: TemporalStateKey,
        obs: SpatialObservation | LineCrossingObservation,
    ) -> None:
        """Provenance integrity: the key and observation must agree."""
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
                f"temporal state key does not match the observation provenance "
                f"({', '.join(mismatches)}); cross-scope evaluation is rejected"
            )

    def _apply_out_of_order(self, state: TemporalState, inp: TemporalInput) -> TemporalResult:
        """Deterministic out-of-order policy (§5/§6): window or reject."""
        obs = inp.observation
        event_time, frame_id = _observation_position(obs)
        assert state.watermark_event_time is not None
        delta = (state.watermark_event_time - event_time).total_seconds()
        if delta <= self._policy.reorder_window_seconds:
            # Within the allowed reordering window: accepted, NEVER rewinds
            # state/counters/watermark; refreshes last_seen only if newer.
            last_seen = event_time
            if state.last_seen is not None and state.last_seen > event_time:
                last_seen = state.last_seen
            updated = state.model_copy(update={"last_seen": last_seen})
            transition = self._build_transition(
                key=inp.key,
                frame_id=frame_id,
                event_time=event_time,
                kind=inp.observation_kind,
                from_state=state.current_state,
                to_state=state.current_state,
                reason=TemporalReason.REORDERED,
                processing_time=inp.processing_time,
            )
            return TemporalResult(state=updated, transitions=(transition,))
        raise LateEventError(
            f"observation event_time {event_time.isoformat()} is "
            f"{delta:.3f}s older than the watermark "
            f"{state.watermark_event_time.isoformat()}, beyond the reordering "
            f"window of {self._policy.reorder_window_seconds}s — rejected "
            "deterministically (never silently discarded or force-ordered)"
        )

    def _apply_evaluation(
        self,
        state: TemporalState,
        evaluation: Evaluation,
        inp: TemporalInput,
        transition: TemporalTransition,
        *,
        event_time: datetime,
        frame_id: FrameId,
    ) -> TemporalState:
        """Materialize the FSM-semantics outcome into a new state."""
        limit = self._policy.transition_history_limit
        history = (*state.recent_transitions, transition)
        if limit > 0 and len(history) > limit:
            history = history[-limit:]
        return state.model_copy(
            update={
                "current_state": evaluation.state,
                "state_since": (
                    evaluation.state_since
                    if evaluation.state_since is not None
                    else state.state_since
                ),
                "last_seen": event_time,
                "last_present_seen": (
                    event_time if evaluation.present_seen else state.last_present_seen
                ),
                "watermark_event_time": event_time,
                "last_applied_frame_id": frame_id,
                "entry_confirm_count": evaluation.entry_confirm_count,
                "exit_confirm_count": evaluation.exit_confirm_count,
                "occlusion_state": evaluation.occlusion_state,
                "missing_since": evaluation.missing_since,
                "recent_transitions": history,
            }
        )

    def _build_transition(
        self,
        *,
        key: TemporalStateKey,
        frame_id: FrameId,
        event_time: datetime,
        kind: str,
        from_state: str,
        to_state: str,
        reason: TemporalReason,
        processing_time: datetime,
    ) -> TemporalTransition:
        """Build a transition with a CONTENT-DERIVED deterministic ID.

        The same (key, observation position, outcome) always reproduces the
        same transition identity — the idempotency marker (Task 7
        principle: identity-keyed dedup, reused here, not re-architected).
        """
        canonical = "|".join([
            key.canonical(),
            str(frame_id),
            event_time.isoformat(),
            from_state,
            to_state,
            kind,
            reason.value,
        ])
        transition_id = EventId(uuid5(TEMPORAL_ID_NAMESPACE, canonical))
        return TemporalTransition(
            transition_id=transition_id,
            fsm_kind=key.fsm_kind,
            key=key,
            from_state=from_state,
            to_state=to_state,
            event_kind=kind,
            reason=reason,
            observation_frame_id=frame_id,
            event_time=event_time,
            processing_time=processing_time,
            configuration_version_id=key.configuration_version_id,
            fsm_version=self._fsm.version,
        )

    def _evaluate(
        self,
        state: TemporalState,
        *,
        kind: str,
        event_time: datetime,
        obs: SpatialObservation | LineCrossingObservation,
    ) -> Evaluation:
        """FSM-specific semantics — implemented by subclasses."""
        raise NotImplementedError(f"TemporalEngine '{self._fsm.name}' must implement _evaluate")


class PresenceTemporalEngine(TemporalEngine):
    """Presence (enter/exit) semantics on the foundation discipline (Task 15.2).

    The full four-state enter/exit model — ABSENT / ENTERING / PRESENT /
    EXITING (state values ``absent``/``entering``/``present``/``exiting``,
    package convention) — driven by the same configurable ``TemporalPolicy``
    knobs. Observation kinds (from ``presence_kind`` or explicit input):

      - ``present``       — positively in the context: from ABSENT it
        starts the ENTERING intermediate state and accumulates toward
        ``entry_confirmation``, then PRESENT; a present while EXITING
        recovers PRESENT (exit aborted); always resets exit/occlusion
        state and refreshes ``last_present_seen``.
      - ``absent``        — observed outside the context: from PRESENT it
        qualifies toward exit ONLY when the dwell and grace conditions
        hold (anti-jitter), moving to EXITING (or directly ABSENT when
        ``exit_confirmation == 1``); from ENTERING it aborts the
        unconfirmed entry back to ABSENT.
      - ``not_observed``  — policy-intercepted/missing: from PRESENT,
        within ``occlusion_tolerance_seconds`` of the last positive
        presence the state stays PRESENT with occlusion
        TEMPORARILY_MISSING; beyond it (and dwell-ok) -> ABSENT (missing
        expired). From EXITING it counts toward exit confirmation.

    Recorded Task 15.2 decisions (both deterministic, both surfaced here):

      - ``EXITING -> PRESENT`` recovery is NOT grace-gated — ANY positive
        ``present`` while EXITING recovers, because a real positive
        observation is stronger evidence of presence than a timed
        deadline. Grace only gates the absence side (what may start an
        exit).
      - Occlusion tolerance applies ONLY to the confirmed PRESENT state.
        A ``not_observed`` during ENTERING aborts the entry
        unconditionally (matching the task's "presence lost before
        confirmation -> ABSENT"): the entry was never confirmed, so
        there is no confirmed presence to protect from a short gap.
      - ``session_closed``— explicit closure -> ABSENT from any state.

    All thresholds come from ``TemporalPolicy`` — never hardcoded.
    """

    OBSERVATION_KINDS: tuple[str, ...] = (
        "present",
        "absent",
        "not_observed",
        "session_closed",
    )

    def __init__(self, *, fsm: DeterministicFsm, policy: TemporalPolicy) -> None:
        super().__init__(
            fsm=fsm,
            policy=policy,
            observation_kinds=self.OBSERVATION_KINDS,
        )

    def _evaluate(
        self,
        state: TemporalState,
        *,
        kind: str,
        event_time: datetime,
        obs: SpatialObservation | LineCrossingObservation,
    ) -> Evaluation:
        policy = self._policy
        current = state.current_state

        if kind == "present":
            if current == "absent":
                # First positive observation. entry_confirmation == 1 means
                # the configured policy explicitly allows instant entry;
                # otherwise begin the ENTERING intermediate state.
                if policy.entry_confirmation == 1:
                    next_state = self._fsm.transition("absent", "enter_confirmed")
                    return Evaluation(
                        state=next_state,
                        reason=TemporalReason.ENTER_CONFIRMED,
                        present_seen=True,
                        state_since=event_time,
                    )
                next_state = self._fsm.transition("absent", "enter_pending")
                return Evaluation(
                    state=next_state,
                    reason=TemporalReason.OBSERVED_STAY,
                    present_seen=True,
                    state_since=event_time,
                    entry_confirm_count=1,
                    occlusion_state=TemporalOcclusionState.OBSERVED,
                )
            if current == "entering":
                entry = state.entry_confirm_count + 1
                if entry >= policy.entry_confirmation:
                    next_state = self._fsm.transition("entering", "enter_confirmed")
                    return Evaluation(
                        state=next_state,
                        reason=TemporalReason.ENTER_CONFIRMED,
                        present_seen=True,
                        state_since=event_time,
                    )
                # Still accumulating entry confirmation.
                return Evaluation(
                    state="entering",
                    reason=TemporalReason.OBSERVED_STAY,
                    present_seen=True,
                    entry_confirm_count=entry,
                    occlusion_state=TemporalOcclusionState.OBSERVED,
                )
            if current == "exiting":
                # Positive presence returns during exit confirmation:
                # recovery — no ENTER/EXIT fact is emitted, the entity
                # never left.
                next_state = self._fsm.transition("exiting", "recovered")
                return Evaluation(
                    state=next_state,
                    reason=TemporalReason.OBSERVED_STAY,
                    present_seen=True,
                    state_since=event_time,
                    occlusion_state=TemporalOcclusionState.OBSERVED,
                )
            # Already present: stay.
            return Evaluation(
                state="present",
                reason=TemporalReason.OBSERVED_STAY,
                present_seen=True,
                occlusion_state=TemporalOcclusionState.OBSERVED,
            )

        if kind == "absent":
            if current == "absent":
                # Already absent: stay; an absent observation resets streaks.
                return Evaluation(state="absent", reason=TemporalReason.OBSERVED_STAY)
            if current == "entering":
                # Presence lost before entry confirmation: abort the entry.
                next_state = self._fsm.transition("entering", "enter_aborted")
                return Evaluation(
                    state=next_state,
                    reason=TemporalReason.OBSERVED_STAY,
                    state_since=event_time,
                )
            if current == "present":
                dwell_ok = self._dwell_ok(state, event_time)
                grace_ok = self._grace_ok(state, event_time)
                if dwell_ok and grace_ok:
                    # Qualified loss of presence. exit_confirmation == 1
                    # confirms directly; otherwise begin EXITING.
                    if policy.exit_confirmation == 1:
                        next_state = self._fsm.transition("present", "exit_confirmed")
                        return Evaluation(
                            state=next_state,
                            reason=TemporalReason.EXIT_CONFIRMED,
                            state_since=event_time,
                        )
                    next_state = self._fsm.transition("present", "exit_pending")
                    return Evaluation(
                        state=next_state,
                        reason=TemporalReason.OBSERVED_STAY,
                        state_since=event_time,
                        exit_confirm_count=1,
                        occlusion_state=TemporalOcclusionState.OBSERVED,
                    )
                # Within dwell/grace: noise near a boundary — no count.
                return Evaluation(
                    state="present",
                    reason=TemporalReason.OBSERVED_STAY,
                    occlusion_state=TemporalOcclusionState.OBSERVED,
                )
            # current == "exiting": further absents count toward confirmation.
            exit_count = state.exit_confirm_count + 1
            if exit_count >= policy.exit_confirmation:
                next_state = self._fsm.transition("exiting", "exit_confirmed")
                return Evaluation(
                    state=next_state,
                    reason=TemporalReason.EXIT_CONFIRMED,
                    state_since=event_time,
                )
            return Evaluation(
                state="exiting",
                reason=TemporalReason.OBSERVED_STAY,
                exit_confirm_count=exit_count,
                occlusion_state=TemporalOcclusionState.OBSERVED,
            )

        if kind == "not_observed":
            if current == "absent":
                # Missing while absent: stay absent; missing timer irrelevant.
                return Evaluation(state="absent", reason=TemporalReason.OBSERVED_STAY)
            if current == "entering":
                # Missing before confirmation: presence lost, abort entry.
                next_state = self._fsm.transition("entering", "enter_aborted")
                return Evaluation(
                    state=next_state,
                    reason=TemporalReason.OBSERVED_STAY,
                    state_since=event_time,
                )
            if current == "present":
                dwell_ok = self._dwell_ok(state, event_time)
                last_present = state.last_present_seen
                gap = (
                    (event_time - last_present).total_seconds()
                    if last_present is not None
                    else float("inf")
                )
                if dwell_ok and gap >= policy.occlusion_tolerance_seconds:
                    next_state = self._fsm.transition("present", "missing_expired")
                    return Evaluation(
                        state=next_state,
                        reason=TemporalReason.MISSING_EXPIRED,
                        state_since=event_time,
                    )
                # Short gap: temporarily missing, state NOT flipped.
                return Evaluation(
                    state="present",
                    reason=TemporalReason.OBSERVED_STAY,
                    occlusion_state=TemporalOcclusionState.TEMPORARILY_MISSING,
                    missing_since=state.missing_since or event_time,
                )
            # current == "exiting": a missing observation during exit
            # confirmation counts toward it (consistent with ``absent``).
            exit_count = state.exit_confirm_count + 1
            if exit_count >= policy.exit_confirmation:
                next_state = self._fsm.transition("exiting", "exit_confirmed")
                return Evaluation(
                    state=next_state,
                    reason=TemporalReason.EXIT_CONFIRMED,
                    state_since=event_time,
                )
            return Evaluation(
                state="exiting",
                reason=TemporalReason.OBSERVED_STAY,
                exit_confirm_count=exit_count,
                occlusion_state=TemporalOcclusionState.TEMPORARILY_MISSING,
                missing_since=state.missing_since or event_time,
            )

        if kind == "session_closed":
            next_state = self._fsm.transition(current, "session_closed")
            return Evaluation(
                state=next_state,
                reason=TemporalReason.SESSION_CLOSED,
                state_since=event_time,
            )

        raise InvalidTemporalInputError(
            f"unknown observation_kind {kind!r} for FSM '{self._fsm.name}'; "
            f"allowed: present, absent, not_observed, session_closed"
        )

    def _dwell_ok(self, state: TemporalState, event_time: datetime) -> bool:
        """PRESENT must have persisted at least ``minimum_dwell_seconds``."""
        if state.state_since is None:
            return True
        return (
            event_time - state.state_since
        ).total_seconds() >= self._policy.minimum_dwell_seconds

    def _grace_ok(self, state: TemporalState, event_time: datetime) -> bool:
        """An absent observation must be ``exit_grace_seconds`` past the
        last positive presence before it can count toward exit."""
        if state.last_present_seen is None:
            return True
        return (
            event_time - state.last_present_seen
        ).total_seconds() >= self._policy.exit_grace_seconds
