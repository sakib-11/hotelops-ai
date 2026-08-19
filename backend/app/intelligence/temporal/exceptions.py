"""Exception taxonomy for the deterministic temporal engine (Task 15 Step 1).

Mirrors the provider-isolation convention of the sibling packages
(``detectors``, ``tracking``, ``geometry``, ``spatial``): downstream
business logic depends only on these types, never on raw ``ValueError``
or math errors leaking from predicate internals.

Semantics:

- ``TemporalError`` is the base for every temporal-engine failure.
- ``InvalidTemporalInputError`` — the inputs are missing, wrong-typed,
  or violate the canonical contract (unknown observation kind, naive
  timestamps, missing key components). Malformed input is never repaired.
- ``LateEventError`` — an observation's event_time is older than the
  configured reordering window relative to the watermark. The event is
  rejected deterministically — never silently discarded or reordered
  beyond the policy.
- ``InvalidTransitionError`` — the FSM was asked for a transition that is
  not in its legal transition table. No arbitrary state mutation.
- ``StateKeyMismatchError`` — the temporal state key does not match the
  observation's canonical provenance (session/track/camera/configuration
  version). Cross-scope evaluation is impossible; explicit rejection.
- ``FsmVersionMismatchError`` — restoring a checkpoint whose FSM/engine
  version differs from the engine's. Historical state is never silently
  reinterpreted by changed semantics.
- ``CheckpointIntegrityError`` — a checkpoint is structurally invalid or
  was produced under a different policy revision.

All failures are deterministic: identical input always produces the same
typed error.
"""

from __future__ import annotations


class TemporalError(Exception):
    """Base exception for all temporal engine errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class InvalidTemporalInputError(TemporalError):
    """Inputs are missing, wrong-typed, or violate the canonical contract."""


class LateEventError(TemporalError):
    """Observation event_time is older than the reordering window allows."""


class InvalidTransitionError(TemporalError):
    """The FSM does not define the requested state transition."""


class StateKeyMismatchError(TemporalError):
    """The state key does not match the observation's canonical provenance."""


class FsmVersionMismatchError(TemporalError):
    """Checkpoint FSM/engine version differs from the engine's."""


class CheckpointIntegrityError(TemporalError):
    """Checkpoint is invalid or was produced under a different policy."""


__all__ = [
    "CheckpointIntegrityError",
    "FsmVersionMismatchError",
    "InvalidTemporalInputError",
    "InvalidTransitionError",
    "LateEventError",
    "StateKeyMismatchError",
    "TemporalError",
]
