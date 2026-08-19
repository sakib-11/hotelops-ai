"""Evidence processing state machine (Task 17.10).

Makes evidence processing durable and recoverable with EXPLICIT states
and an EXPLICIT, immutable transition table. Reuses the canonical
``DeterministicFsm`` from Task 15 — the project's established FSM
convention — with evidence-typed states and events:

    REQUESTED ──queue──▶ QUEUED ──start_extracting──▶ EXTRACTING
        │                    │                            │
        │                    │                     extraction_complete
        │                    │                            ▼
        │                    │                         UPLOADING ──finalize──▶ FINALIZED
        │                    │                            │  │
        │                    │                     retryable_failure │ terminal_failure
        │                    │                            ▼  ▼
        │                    │              RETRYABLE_FAILURE   TERMINAL_FAILURE
        │                    │                    │
        │                    │                 retry (back to QUEUED)
        │                    │
        │                    └── expire ──▶ EXPIRED
        └────── expire ───────▶ EXPIRED

Rules:

- Allowed transitions are declared ONCE in a transition table; anything
  not declared is rejected with ``InvalidEvidenceTransitionError`` —
  no arbitrary state mutation.
- FINALIZED is IMMUTABLE: no transition is legal out of it (a
  ``FinalizedEvidenceMutationError``).
- EXPIRED and TERMINAL_FAILURE are terminal failure states (no outgoing
  transitions).
- RETRYABLE_FAILURE → QUEUED is the only recovery path; retries are
  bounded by the caller (Task 7), not by the FSM.
- The FSM is PURE and deterministic: it returns the next state; it
  performs no side effects. State durability/recovery lives in the
  service layer (``EvidenceStateService``) which persists the state on
  the evidence ref's variable metadata (JSONB policy).

Every state carries a machine version so changed semantics never
reinterpret old persisted state (Task 15 convention).
"""

from __future__ import annotations

from enum import StrEnum

from backend.app.domain.evidence.exceptions import (
    FinalizedEvidenceMutationError,
    InvalidEvidenceTransitionError,
)
from backend.app.intelligence.temporal.exceptions import InvalidTransitionError
from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule

# Canonical metadata keys on the evidence ref's variable metadata (JSONB).
EVIDENCE_PROCESSING_STATE_KEY = "processing_state"
EVIDENCE_PROCESSING_VERSION_KEY = "processing_state_version"

# Semantics version — carried on every state so a future FSM change never
# silently reinterprets old persisted state (Task 15 convention).
EVIDENCE_STATE_MACHINE_VERSION = "1"


class EvidenceProcessingState(StrEnum):
    """Durable processing states of one evidence artifact (Task 17.10)."""

    REQUESTED = "requested"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    UPLOADING = "uploading"
    FINALIZED = "finalized"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    EXPIRED = "expired"


class EvidenceEvent(StrEnum):
    """Events that drive the evidence state machine."""

    QUEUE = "queue"
    START_EXTRACTING = "start_extracting"
    EXTRACTION_COMPLETE = "extraction_complete"
    FINALIZE = "finalize"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    RETRY = "retry"
    EXPIRE = "expire"


# The EXPLICIT, immutable transition table. Each entry is one legal
# transition (from_state, event) -> to_state.
_TRANSITIONS: tuple[FsmRule, ...] = (
    FsmRule(EvidenceProcessingState.REQUESTED, EvidenceEvent.QUEUE, EvidenceProcessingState.QUEUED),
    FsmRule(
        EvidenceProcessingState.QUEUED,
        EvidenceEvent.START_EXTRACTING,
        EvidenceProcessingState.EXTRACTING,
    ),
    FsmRule(
        EvidenceProcessingState.EXTRACTING,
        EvidenceEvent.EXTRACTION_COMPLETE,
        EvidenceProcessingState.UPLOADING,
    ),
    FsmRule(
        EvidenceProcessingState.UPLOADING, EvidenceEvent.FINALIZE, EvidenceProcessingState.FINALIZED
    ),
    # Failure paths.
    FsmRule(
        EvidenceProcessingState.EXTRACTING,
        EvidenceEvent.RETRYABLE_FAILURE,
        EvidenceProcessingState.RETRYABLE_FAILURE,
    ),
    FsmRule(
        EvidenceProcessingState.RETRYABLE_FAILURE,
        EvidenceEvent.RETRY,
        EvidenceProcessingState.QUEUED,
    ),
    FsmRule(
        EvidenceProcessingState.EXTRACTING,
        EvidenceEvent.TERMINAL_FAILURE,
        EvidenceProcessingState.TERMINAL_FAILURE,
    ),
    FsmRule(
        EvidenceProcessingState.UPLOADING,
        EvidenceEvent.RETRYABLE_FAILURE,
        EvidenceProcessingState.RETRYABLE_FAILURE,
    ),
    FsmRule(
        EvidenceProcessingState.UPLOADING,
        EvidenceEvent.TERMINAL_FAILURE,
        EvidenceProcessingState.TERMINAL_FAILURE,
    ),
    # Expiry — any non-terminal, non-failed processing state can expire.
    FsmRule(
        EvidenceProcessingState.REQUESTED, EvidenceEvent.EXPIRE, EvidenceProcessingState.EXPIRED
    ),
    FsmRule(EvidenceProcessingState.QUEUED, EvidenceEvent.EXPIRE, EvidenceProcessingState.EXPIRED),
    FsmRule(
        EvidenceProcessingState.EXTRACTING, EvidenceEvent.EXPIRE, EvidenceProcessingState.EXPIRED
    ),
    FsmRule(
        EvidenceProcessingState.UPLOADING, EvidenceEvent.EXPIRE, EvidenceProcessingState.EXPIRED
    ),
)

# States with no outgoing transitions (immutable).
_TERMINAL_STATES: frozenset[EvidenceProcessingState] = frozenset({
    EvidenceProcessingState.FINALIZED,
    EvidenceProcessingState.TERMINAL_FAILURE,
    EvidenceProcessingState.EXPIRED,
})


class EvidenceStateMachine:
    """Typed, explicit, immutable evidence processing FSM (Task 17.10)."""

    def __init__(self) -> None:
        self._fsm = DeterministicFsm(
            name="evidence_processing",
            version=EVIDENCE_STATE_MACHINE_VERSION,
            states=tuple(s.value for s in EvidenceProcessingState),
            initial_state=EvidenceProcessingState.REQUESTED.value,
            rules=_TRANSITIONS,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def version(self) -> str:
        return EVIDENCE_STATE_MACHINE_VERSION

    @property
    def initial_state(self) -> EvidenceProcessingState:
        return EvidenceProcessingState.REQUESTED

    def states(self) -> tuple[EvidenceProcessingState, ...]:
        return tuple(EvidenceProcessingState)

    def allowed_events(self, state: EvidenceProcessingState) -> tuple[EvidenceEvent, ...]:
        """All events legal from ``state`` (deterministic tuple order)."""
        return tuple(EvidenceEvent(event) for event in self._fsm.allowed_events(state.value))

    def can_transition(self, state: EvidenceProcessingState, event: EvidenceEvent) -> bool:
        return self._fsm.can_transition(state.value, event.value)

    def event_target(self, event: EvidenceEvent) -> EvidenceProcessingState | None:
        """The unique declared target state of ``event`` (None when undeclared).

        Derived from the explicit transition table — every event in the
        evidence machine has exactly one target, so this is the canonical
        "what state does this event produce" lookup used by the service
        to detect duplicate deliveries (Task 7 idempotency).
        """
        targets: set[EvidenceProcessingState] = {
            EvidenceProcessingState(rule.to_state)
            for rule in _TRANSITIONS
            if rule.event == event.value
        }
        if len(targets) == 1:
            return next(iter(targets))
        return None

    def is_terminal(self, state: EvidenceProcessingState) -> bool:
        """True for immutable states (FINALIZED / TERMINAL_FAILURE / EXPIRED)."""
        return state in _TERMINAL_STATES

    # ------------------------------------------------------------------
    # Transition
    # ------------------------------------------------------------------

    def next_state(
        self, state: EvidenceProcessingState, event: EvidenceEvent
    ) -> EvidenceProcessingState:
        """The next state for a legal transition (pure, no side effects).

        Raises:
            FinalizedEvidenceMutationError: ``state`` is FINALIZED —
                finalization is immutable.
            InvalidEvidenceTransitionError: the transition is not
                declared in the table.
        """
        if state is EvidenceProcessingState.FINALIZED:
            msg = (
                f"FINALIZED evidence is immutable — no transition is legal "
                f"from it (event {event.value!r} rejected)"
            )
            raise FinalizedEvidenceMutationError(msg)

        try:
            raw = self._fsm.transition(state.value, event.value)
        except InvalidTransitionError as exc:
            allowed = ", ".join(e.value for e in self.allowed_events(state)) or "none"
            raise InvalidEvidenceTransitionError(
                f"invalid evidence transition from {state.value!r} on event "
                f"{event.value!r}; allowed: {allowed}"
            ) from exc
        return EvidenceProcessingState(raw)


__all__ = [
    "EVIDENCE_PROCESSING_STATE_KEY",
    "EVIDENCE_PROCESSING_VERSION_KEY",
    "EVIDENCE_STATE_MACHINE_VERSION",
    "EvidenceEvent",
    "EvidenceProcessingState",
    "EvidenceStateMachine",
]
