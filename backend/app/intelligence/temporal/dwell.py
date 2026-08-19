"""Dwell FSM — deterministic dwell-time intelligence (Task 15.3).

Derives dwell intervals from the Enter/Exit FSM's confirmed transitions.
Architecture (Task 15.3):

    SpatialObservation
        ↓ Task 15.1 Temporal Foundation — event-time ordering, watermark,
          late/out-of-order policy, idempotent dedup, checkpoint/restore
    Enter/Exit FSM (Task 15.2, ``PresenceTemporalEngine``)
        ↓ presence TemporalTransition
    DWELL FSM — this module
        ↓
    DwellInterval (fact)

Dwell = the amount of event-time for which an entity remains continuously
PRESENT within the same spatial context. It is never computed from raw
frames, wall-clock time, or processing time.

The dwell FSM is a SEPARATE family (fsm_kind="dwell") on the SAME
foundation discipline: it consumes the presence engine's transitions as
its observation kinds (``dwell_event_from_presence`` is the only
sanctioned mapping), so it inherits dedup, event-time ordering,
key/provenance integrity, isolation, and checkpointing without
duplicating any of it. The presence FSM is NOT modified (§2: the
four-state Enter/Exit model stays untouched; dwell is derived from it).

State model (§2 — minimum states; no extra DWELLING state is added to
the presence FSM):

    idle --enter_confirmed--> dwelling    (confirmed PRESENT -> dwell start)
    dwelling --exit_confirmed--> idle     (confirmed ABSENT -> dwell end)
    dwelling --missing_expired--> idle    (occlusion gap beyond tolerance)
    * --session_closed--> idle            (explicit session closure)
    * --stay--> same state                (interval continues / stays idle)

Semantics:
  - dwell_start (§3): the event-time of the ENTER_CONFIRMED transition
    (the confirmed PRESENT) — never processing time, never the first
    observation.
  - dwell_end (§4): the event-time of the confirming ABSENT transition;
    duration = dwell_end - dwell_start, never wall clock.
  - Continuous presence (§5): the interval spans start..end; individual
    observations are never summed (no double counting).
  - Short occlusion (§6): while the presence FSM stays PRESENT (within
    the configured grace/occlusion tolerance), the interval stays OPEN
    and dwell_start is NOT reset.
  - Confirmed exit only (§7): EXITING does not end dwell — only
    EXIT_CONFIRMED / MISSING_EXPIRED / session closure do.
  - Re-entry (§8): each confirmed entry opens a NEW interval; intervals
    are never merged.
  - Running dwell (§11): ``open_interval`` exposes the open interval
    (dwell_end=None, last_seen=latest) — no fabricated end.
  - Minimum dwell (§9): ``TemporalPolicy.dwell_minimum_seconds``
    qualifies the fact (``qualified`` flag) but NEVER alters the recorded
    interval — the actual presence span is preserved regardless.

Wiring (pure and deterministic — the coordinator feeds BOTH engines the
same observations in the same order):

    presence_result = presence_engine.apply(presence_state, inp)
    dwell_kind = dwell_event_from_presence(presence_result.transitions[0])
    dwell_result = dwell_engine.apply(
        dwell_state,
        TemporalInput(key=dwell_key, observation=inp.observation,
                      observation_kind=dwell_kind,
                      processing_time=inp.processing_time),
    )

``DwellEngine`` performs NO I/O and reads no current time.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid5

from backend.app.intelligence.temporal.engine import (
    Evaluation,
    TemporalEngine,
    TemporalInput,
    TemporalResult,
)
from backend.app.intelligence.temporal.exceptions import InvalidTemporalInputError
from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule
from contracts.common import EventId
from contracts.spatial import LineCrossingObservation, SpatialObservation
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    DwellInterval,
    TemporalPolicy,
    TemporalReason,
    TemporalState,
    TemporalStateKey,
    TemporalTransition,
)

__all__ = [
    "DWELL_FSM",
    "DwellEngine",
    "dwell_event_from_presence",
]

DWELL_FSM = DeterministicFsm(
    name="dwell",
    version=TEMPORAL_ENGINE_VERSION,
    states=("idle", "dwelling"),
    initial_state="idle",
    rules=(
        FsmRule(from_state="idle", event="enter_confirmed", to_state="dwelling"),
        FsmRule(from_state="dwelling", event="exit_confirmed", to_state="idle"),
        FsmRule(from_state="dwelling", event="missing_expired", to_state="idle"),
        FsmRule(from_state="idle", event="stay", to_state="idle"),
        FsmRule(from_state="dwelling", event="stay", to_state="dwelling"),
        FsmRule(from_state="idle", event="session_closed", to_state="idle"),
        FsmRule(from_state="dwelling", event="session_closed", to_state="idle"),
    ),
)

# Presence reasons that CLOSE an open dwell interval (confirmed ABSENT).
_INTERVAL_CLOSING_REASONS = (
    TemporalReason.EXIT_CONFIRMED,
    TemporalReason.MISSING_EXPIRED,
    TemporalReason.SESSION_CLOSED,
)


def dwell_event_from_presence(transition: TemporalTransition) -> str:
    """Map one presence transition to its dwell observation kind.

    This is the ONLY sanctioned presence -> dwell mapping (deterministic):
      - ENTER_CONFIRMED   -> ``enter_confirmed``  (opens the interval)
      - EXIT_CONFIRMED    -> ``exit_confirmed``   (closes it)
      - MISSING_EXPIRED   -> ``missing_expired``  (closes it)
      - SESSION_CLOSED    -> ``session_closed``   (closes it / stays idle)
      - OBSERVED_STAY     -> ``stay``
      - DEDUPLICATED/REORDERED -> ``stay`` — the dwell engine reproduces
        the same dedup/reorder deterministically from the position.
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


class DwellEngine(TemporalEngine):
    """Derives dwell intervals from confirmed Enter/Exit transitions.

    A ``TemporalEngine`` of family ``dwell`` driven by presence
    transitions (see module docstring). Closed intervals are emitted as
    ``DwellInterval`` facts on the result; ``open_interval`` exposes the
    running interval while dwelling.
    """

    OBSERVATION_KINDS: tuple[str, ...] = (
        "enter_confirmed",
        "exit_confirmed",
        "missing_expired",
        "stay",
        "session_closed",
    )

    def __init__(self, *, fsm: DeterministicFsm, policy: TemporalPolicy) -> None:
        super().__init__(
            fsm=fsm,
            policy=policy,
            observation_kinds=self.OBSERVATION_KINDS,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(self, state: TemporalState, inp: TemporalInput) -> TemporalResult:
        """Apply one presence-derived dwell input (pure, deterministic).

        Delegates the full discipline (validation, ordering, dedup,
        watermark, checkpoint semantics) to the base engine, then emits
        one closed ``DwellInterval`` fact when a transition confirms the
        end of an OPEN interval.
        """
        result = super().apply(state, inp)
        transition = result.transitions[0]
        if transition.reason in _INTERVAL_CLOSING_REASONS and state.current_state == "dwelling":
            interval = self._build_closed_interval(
                key=inp.key,
                dwell_start=state.state_since,
                dwell_end=transition.event_time,
                reason=transition.reason,
            )
            # model-copy semantics: future TemporalResult fields survive.
            return replace(result, dwell_intervals=(interval,))
        return result

    def open_interval(self, state: TemporalState) -> DwellInterval | None:
        """The RUNNING dwell interval, or None when idle (§11).

        ``dwell_end`` is None; ``last_seen`` is the most recent accepted
        observation event_time. The interval id is stable while open
        (derived from key + dwell_start only), so it does not change as
        ``last_seen`` advances.
        """
        if state.current_state != "dwelling":
            return None
        dwell_start = state.state_since
        if dwell_start is None:
            raise InvalidTemporalInputError(
                "corrupted dwell state: dwelling without a dwell_start "
                "(the interval was never opened by a confirmed-PRESENT event)"
            )
        last_seen = state.last_seen if state.last_seen is not None else dwell_start
        duration = (last_seen - dwell_start).total_seconds()
        return DwellInterval(
            interval_id=EventId(
                uuid5(
                    TEMPORAL_ID_NAMESPACE,
                    self._interval_identity(key=state.key, dwell_start=dwell_start, is_open=True),
                )
            ),
            fsm_kind=self._fsm.name,
            key=state.key,
            dwell_start=dwell_start,
            dwell_end=None,
            last_seen=last_seen,
            duration_seconds=duration,
            qualified=duration >= self._policy.dwell_minimum_seconds,
            minimum_dwell_seconds=self._policy.dwell_minimum_seconds,
            reason=None,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )

    # ------------------------------------------------------------------
    # Interval derivation (pure)
    # ------------------------------------------------------------------

    def _build_closed_interval(
        self,
        *,
        key: TemporalStateKey,
        dwell_start: datetime | None,
        dwell_end: datetime,
        reason: TemporalReason,
    ) -> DwellInterval:
        """A closed interval fact from the dwelling state at exit time."""
        if dwell_start is None:
            raise InvalidTemporalInputError(
                "corrupted dwell state: interval closing without a dwell_start "
                "(the interval was never opened by a confirmed-PRESENT event)"
            )
        duration = (dwell_end - dwell_start).total_seconds()
        return DwellInterval(
            interval_id=EventId(
                uuid5(
                    TEMPORAL_ID_NAMESPACE,
                    self._interval_identity(key=key, dwell_start=dwell_start, dwell_end=dwell_end),
                )
            ),
            fsm_kind=self._fsm.name,
            key=key,
            dwell_start=dwell_start,
            dwell_end=dwell_end,
            last_seen=dwell_end,
            duration_seconds=duration,
            qualified=duration >= self._policy.dwell_minimum_seconds,
            minimum_dwell_seconds=self._policy.dwell_minimum_seconds,
            reason=reason,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )

    def _interval_identity(
        self,
        *,
        key: TemporalStateKey,
        dwell_start: datetime,
        dwell_end: datetime | None = None,
        is_open: bool = False,
    ) -> str:
        """Content-derived identity string for an interval (deterministic).

        Open intervals derive the id from key + dwell_start only, so the
        id is stable while the interval runs; closed intervals additionally
        bind the dwell_end.
        """
        if is_open:
            return "|".join([
                key.canonical(),
                "open",
                dwell_start.isoformat(),
                self._fsm.version,
                self._policy.revision,
            ])
        if dwell_end is None:
            raise InvalidTemporalInputError("closed interval identity requires dwell_end")
        return "|".join([
            key.canonical(),
            "closed",
            dwell_start.isoformat(),
            dwell_end.isoformat(),
            self._fsm.version,
            self._policy.revision,
        ])

    # ------------------------------------------------------------------
    # FSM semantics
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        state: TemporalState,
        *,
        kind: str,
        event_time: datetime,
        obs: SpatialObservation | LineCrossingObservation,
    ) -> Evaluation:
        current = state.current_state
        if kind == "enter_confirmed":
            next_state = self._fsm.transition(current, "enter_confirmed")
            return Evaluation(
                state=next_state,
                reason=TemporalReason.ENTER_CONFIRMED,
                state_since=event_time,
            )
        if kind == "exit_confirmed":
            next_state = self._fsm.transition(current, "exit_confirmed")
            return Evaluation(
                state=next_state,
                reason=TemporalReason.EXIT_CONFIRMED,
                state_since=event_time,
            )
        if kind == "missing_expired":
            next_state = self._fsm.transition(current, "missing_expired")
            return Evaluation(
                state=next_state,
                reason=TemporalReason.MISSING_EXPIRED,
                state_since=event_time,
            )
        if kind == "session_closed":
            next_state = self._fsm.transition(current, "session_closed")
            return Evaluation(
                state=next_state,
                reason=TemporalReason.SESSION_CLOSED,
                state_since=event_time,
            )
        if kind == "stay":
            # No state change; the base materializes last_seen = event_time
            # so a running interval's last_seen advances.
            return Evaluation(state=current, reason=TemporalReason.OBSERVED_STAY)
        raise InvalidTemporalInputError(
            f"unknown observation_kind {kind!r} for FSM '{self._fsm.name}'; "
            f"allowed: enter_confirmed, exit_confirmed, missing_expired, stay, session_closed"
        )
