"""Enter/exit FSM — the full presence model on the Task 15 foundation (Task 15.2).

The canonical Task 15.2 state machine: whether a tracked entity is
stably present inside a configured spatial context, without noisy CV
observations producing repeated ENTER -> EXIT -> ENTER -> EXIT facts.
Built on the reusable ``DeterministicFsm`` with four explicit states
(state values follow the package convention — lowercase, like
``TemporalReason`` values — for the spec's ABSENT / ENTERING / PRESENT /
EXITING):

    ABSENT --enter_pending-----> ENTERING --enter_confirmed--> PRESENT
    ABSENT --enter_confirmed---> PRESENT   (only entry_confirmation == 1)
    ENTERING --enter_aborted---> ABSENT    (presence lost before confirmation)
    PRESENT --exit_pending-----> EXITING  --exit_confirmed--> ABSENT
    PRESENT --exit_confirmed---> ABSENT    (only exit_confirmation == 1)
    PRESENT --missing_expired--> ABSENT    (occlusion gap beyond tolerance)
    EXITING --recovered--------> PRESENT   (positive presence returns)
    *       --stay-------------> same state
    *       --session_closed---> ABSENT

The engine decides WHEN an event is emitted (confirmation counting,
grace/dwell/occlusion from ``TemporalPolicy``); the FSM decides whether
the event is LEGAL. No arbitrary mutation is possible.

Recorded Task 15.2 decisions:
  - ``EXITING --recovered--> PRESENT`` is NOT grace-gated: any positive
    ``present`` observation recovers, because real positive evidence
    outranks a timed exit deadline (grace gates only the absence side).
  - Occlusion tolerance protects only the confirmed PRESENT state; a
    missing observation during ENTERING aborts the unconfirmed entry
    (presence lost before confirmation).

``presence_kind`` is the deterministic structural mapping from a
canonical ``SpatialObservation`` to a presence observation kind:

  - INSIDE / AMBIGUOUS   -> ``present``      (a deterministic spatial match)
  - OUTSIDE              -> ``absent``       (observed, but not in the context)
  - EXCLUDED / PRIVACY   -> ``not_observed`` (policy-intercepted — the engine
    cannot see the entity; treated as a missing observation, not an exit)

This is a structural adapter (no hotel business thresholds); future FSMs
provide their own kind mappings.
"""

from __future__ import annotations

from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule
from contracts.spatial import SpatialObservation, SpatialStatus
from contracts.temporal import TEMPORAL_ENGINE_VERSION

PRESENCE_FSM = DeterministicFsm(
    name="presence",
    version=TEMPORAL_ENGINE_VERSION,
    states=("absent", "entering", "present", "exiting"),
    initial_state="absent",
    rules=(
        # --- Entry ---
        FsmRule(from_state="absent", event="enter_pending", to_state="entering"),
        FsmRule(from_state="absent", event="enter_confirmed", to_state="present"),
        FsmRule(from_state="entering", event="enter_confirmed", to_state="present"),
        FsmRule(from_state="entering", event="enter_aborted", to_state="absent"),
        # --- Stay (accepted observation, no state change) ---
        FsmRule(from_state="absent", event="stay", to_state="absent"),
        FsmRule(from_state="entering", event="stay", to_state="entering"),
        FsmRule(from_state="present", event="stay", to_state="present"),
        FsmRule(from_state="exiting", event="stay", to_state="exiting"),
        # --- Exit ---
        FsmRule(from_state="present", event="exit_pending", to_state="exiting"),
        FsmRule(from_state="present", event="exit_confirmed", to_state="absent"),
        FsmRule(from_state="present", event="missing_expired", to_state="absent"),
        FsmRule(from_state="exiting", event="exit_confirmed", to_state="absent"),
        FsmRule(from_state="exiting", event="recovered", to_state="present"),
        # --- Session closure ---
        FsmRule(from_state="absent", event="session_closed", to_state="absent"),
        FsmRule(from_state="entering", event="session_closed", to_state="absent"),
        FsmRule(from_state="present", event="session_closed", to_state="absent"),
        FsmRule(from_state="exiting", event="session_closed", to_state="absent"),
    ),
)


def presence_kind(observation: SpatialObservation) -> str:
    """Structural SpatialStatus -> presence-kind mapping (deterministic)."""
    if observation.status in (SpatialStatus.INSIDE, SpatialStatus.AMBIGUOUS):
        return "present"
    if observation.status is SpatialStatus.OUTSIDE:
        return "absent"
    # EXCLUDED / PRIVACY — policy-intercepted, not observed.
    return "not_observed"


__all__ = [
    "PRESENCE_FSM",
    "presence_kind",
]
