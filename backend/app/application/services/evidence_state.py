"""Durable, recoverable evidence processing state (Task 17.10).

The ``EvidenceStateMachine`` (pure, explicit transition table) decides
what is legal; this service makes evidence processing DURABLE and
RECOVERABLE by persisting the state on the evidence ref's variable
metadata (JSONB policy) and enforcing the workflow rules:

- Every transition is persisted as ``processing_state`` +
  ``processing_state_version`` (the machine version, so old persisted
  state is never silently reinterpreted by a newer machine).
- Transitions are ATOMIC via a from-state guard: the caller supplies the
  observed current state, and the service only applies the transition
  when the PERSISTED state still matches — a duplicate worker that lost
  the race gets ``EvidenceStateMismatchError`` instead of a double
  transition (Task 7-style claim semantics).
- RESTART: a worker crash mid-extraction leaves the persisted state
  (e.g. EXTRACTING) intact; ``restart()`` returns the durable checkpoint
  and the worker resumes legally from it (e.g. EXTRACTING →
  retryable_failure → QUEUED → EXTRACTING), never re-running from
  REQUESTED and never losing the queue position.
- IDEMPOTENCY: applying the same event twice when the first already
  produced the target state is a NO-OP success (Task 7 duplicate
  delivery) — the second worker's ``apply`` returns the same state.
- FINALIZED is immutable: the FSM rejects any mutation attempt with the
  typed error.

The service performs no infrastructure side effects itself — it mutates
the evidence ref's metadata in memory; the caller's transaction persists
it (Task 7 outbox/DB contract).
"""

from __future__ import annotations

from typing import Any

from backend.app.application.services.idempotency import (
    validate_idempotency_key,
)
from backend.app.domain.evidence.exceptions import (
    EvidenceStateMismatchError,
)
from backend.app.domain.evidence.state_machine import (
    EVIDENCE_PROCESSING_STATE_KEY,
    EVIDENCE_PROCESSING_VERSION_KEY,
    EVIDENCE_STATE_MACHINE_VERSION,
    EvidenceEvent,
    EvidenceProcessingState,
    EvidenceStateMachine,
)
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel

__all__ = [
    "EvidenceCheckpoint",
    "EvidenceStateService",
    "EvidenceTransitionResult",
]


class EvidenceTransitionResult:
    """The deterministic result of one persisted transition."""

    def __init__(
        self,
        *,
        from_state: EvidenceProcessingState,
        to_state: EvidenceProcessingState,
        applied: bool,
    ) -> None:
        self._from = from_state
        self._to = to_state
        self._applied = applied

    @property
    def from_state(self) -> EvidenceProcessingState:
        return self._from

    @property
    def to_state(self) -> EvidenceProcessingState:
        return self._to

    @property
    def applied(self) -> bool:
        """True when the transition was newly applied; False = idempotent no-op."""
        return self._applied

    def __repr__(self) -> str:
        return (
            f"EvidenceTransitionResult(from={self._from.value}, "
            f"to={self._to.value}, applied={self._applied})"
        )


class EvidenceCheckpoint:
    """A durable, recoverable snapshot of the evidence processing state."""

    def __init__(
        self,
        *,
        ref_id: Any,
        state: EvidenceProcessingState,
        version: str,
    ) -> None:
        self._ref_id = ref_id
        self._state = state
        self._version = version

    @property
    def ref_id(self) -> Any:
        return self._ref_id

    @property
    def state(self) -> EvidenceProcessingState:
        return self._state

    @property
    def version(self) -> str:
        return self._version

    def __repr__(self) -> str:
        return (
            f"EvidenceCheckpoint(ref_id={self._ref_id}, "
            f"state={self._state.value}, version={self._version})"
        )


class EvidenceStateService:
    """Durable, recoverable, idempotent evidence state workflow."""

    def __init__(self, machine: EvidenceStateMachine | None = None) -> None:
        self._machine = machine or EvidenceStateMachine()

    @property
    def machine(self) -> EvidenceStateMachine:
        return self._machine

    # ------------------------------------------------------------------
    # State persistence on the evidence metadata (JSONB policy)
    # ------------------------------------------------------------------

    def current_state(self, ref: EvidenceRefModel) -> EvidenceProcessingState:
        """The durable persisted state (REQUESTED when never recorded)."""
        metadata = ref.metadata_ or {}
        raw = metadata.get(EVIDENCE_PROCESSING_STATE_KEY)
        if raw is None:
            return self._machine.initial_state
        return EvidenceProcessingState(str(raw))

    def restart(self, ref: EvidenceRefModel) -> EvidenceCheckpoint:
        """The durable checkpoint after a worker restart.

        The persisted state is returned verbatim — a crash never rewinds
        to REQUESTED and never loses the queue position. The caller then
        resumes legally from the checkpoint state.
        """
        state = self.current_state(ref)
        metadata = ref.metadata_ or {}
        version = str(metadata.get(EVIDENCE_PROCESSING_VERSION_KEY) or self._machine.version)
        return EvidenceCheckpoint(ref_id=ref.ref_id, state=state, version=version)

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def apply(
        self,
        ref: EvidenceRefModel,
        event: EvidenceEvent,
        *,
        expected_state: EvidenceProcessingState | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceTransitionResult:
        """Apply and persist one legal transition (durable, idempotent).

        Args:
            ref: The evidence ref whose metadata carries the durable state.
            event: The event to apply.
            expected_state: The caller-observed from-state. When provided
                and different from the PERSISTED state, the transition is
                REJECTED with ``EvidenceStateMismatchError`` (a duplicate
                worker already moved it) — the atomic from-state guard.
            idempotency_key: Optional Task 7 idempotency key; validated
                (never trusted unvalidated).

        Returns:
            The transition result. ``applied=False`` means the event was
            idempotent — the ref was ALREADY at the event's declared
            target (a duplicate delivery), so nothing changed and no
            error is raised (Task 7 semantics).

        Raises:
            EvidenceStateMismatchError: the persisted state does not
                match ``expected_state`` (lost a race to another worker).
            FinalizedEvidenceMutationError: a mutation of FINALIZED was
                attempted.
            InvalidEvidenceTransitionError: the event is not legal from
                the current state.
        """
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)

        persisted = self.current_state(ref)
        if expected_state is not None and persisted is not expected_state:
            msg = (
                f"evidence state mismatch: expected {expected_state.value!r} but "
                f"persisted state is {persisted.value!r} — a concurrent worker "
                f"already transitioned this evidence"
            )
            raise EvidenceStateMismatchError(msg)

        # Idempotency (Task 7): when the ref is ALREADY at the event's
        # declared target, the event was already applied — duplicate
        # delivery → no-op success. Terminal states are excluded: a
        # FINALIZED/EXPIRED/TERMINAL_FAILURE ref is immutable, so the
        # typed rejection always wins over the duplicate shortcut (a
        # FINALIZED ref must never be touched).
        if not self._machine.is_terminal(persisted):
            declared_target = self._machine.event_target(event)
            if declared_target is persisted:
                return EvidenceTransitionResult(
                    from_state=persisted,
                    to_state=persisted,
                    applied=False,
                )

        # Delegate legality to the FSM: raises the typed error for
        # undeclared transitions and for every event on terminal states.
        to_state = self._machine.next_state(persisted, event)
        self._persist(ref, to_state)
        return EvidenceTransitionResult(
            from_state=persisted,
            to_state=to_state,
            applied=True,
        )

    def claim_for_extraction(self, ref: EvidenceRefModel) -> EvidenceTransitionResult:
        """Atomically claim QUEUED evidence for extraction.

        The canonical duplicate-worker guard: only ONE worker can move
        QUEUED → EXTRACTING; a second worker observing the same QUEUED
        state loses the from-state guard and gets a mismatch error.
        """
        return self.apply(
            ref,
            EvidenceEvent.START_EXTRACTING,
            expected_state=EvidenceProcessingState.QUEUED,
        )

    def finalize(self, ref: EvidenceRefModel) -> EvidenceTransitionResult:
        """Finalize UPLOADING evidence (FINALIZED is immutable afterward)."""
        return self.apply(
            ref,
            EvidenceEvent.FINALIZE,
            expected_state=EvidenceProcessingState.UPLOADING,
        )

    # ------------------------------------------------------------------

    def _persist(self, ref: EvidenceRefModel, state: EvidenceProcessingState) -> None:
        metadata: dict[str, Any] = dict(ref.metadata_ or {})
        metadata[EVIDENCE_PROCESSING_STATE_KEY] = state.value
        metadata[EVIDENCE_PROCESSING_VERSION_KEY] = EVIDENCE_STATE_MACHINE_VERSION
        ref.metadata_ = metadata
