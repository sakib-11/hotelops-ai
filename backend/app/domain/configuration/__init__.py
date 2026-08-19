"""Configuration Domain Package (Task 10).

Versioned physical model for CV pipeline with strict state machine lifecycle.
"""

from backend.app.domain.configuration.service import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationImmutablePublishedError,
    ConfigurationNotFoundError,
    ConfigurationService,
    ConfigurationStaleValidationError,
    PublishResult,
)
from backend.app.domain.configuration.state_machine import (
    STATE_INVARIANTS,
    ConfigurationImmutableError,
    ConfigurationStateMachine,
    ConfigurationTransitionError,
    TransitionResult,
    validate_state_invariant,
)

__all__ = [
    "STATE_INVARIANTS",
    "ConfigurationConflictError",
    "ConfigurationError",
    "ConfigurationImmutableError",
    "ConfigurationImmutablePublishedError",
    "ConfigurationNotFoundError",
    "ConfigurationService",
    "ConfigurationStaleValidationError",
    "ConfigurationStateMachine",
    "ConfigurationTransitionError",
    "PublishResult",
    "TransitionResult",
    "validate_state_invariant",
]
