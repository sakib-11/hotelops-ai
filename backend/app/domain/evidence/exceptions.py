"""Evidence domain exceptions (Task 17.10).

Typed, deterministic failures for the evidence state machine and
related domain operations.
"""

from __future__ import annotations


class EvidenceStateError(Exception):
    """Base error for evidence state-machine failures."""


class InvalidEvidenceTransitionError(EvidenceStateError):
    """A transition is not declared in the evidence state machine.

    Raised when an event is not legal from the current state — the FSM
    contract forbids arbitrary state mutation (Task 17.10).
    """


class EvidenceStateMismatchError(EvidenceStateError):
    """The persisted state does not match the expected from-state.

    Raised when a concurrent/duplicate worker already transitioned the
    evidence (Task 17.10 — the from-state guard lost the race).
    """


class FinalizedEvidenceMutationError(EvidenceStateError):
    """An attempt to mutate FINALIZED evidence was rejected.

    FINALIZED is immutable: no transition is ever legal out of it.
    """
