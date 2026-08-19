"""Configuration Version State Machine (Task 10.3).

Enforces the authoritative lifecycle:
    DRAFT → VALIDATING → VALIDATED → PUBLISHED

No backward transitions. No mutations of PUBLISHED versions.
All transitions are explicit, auditable, and tenant/venue authorized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from contracts.configuration.models import ConfigurationStatus


class ConfigurationTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: ConfigurationStatus, attempted: str, allowed: list[str]):
        self.current = current
        self.attempted = attempted
        self.allowed = allowed
        super().__init__(
            f"Invalid transition from {current.value}: '{attempted}' not allowed. "
            f"Allowed: {', '.join(allowed) if allowed else 'none'}"
        )


class ConfigurationImmutableError(Exception):
    """Raised when attempting to mutate a published configuration version."""

    def __init__(self, version_id: str):
        self.version_id = version_id
        super().__init__(f"Configuration version {version_id} is PUBLISHED and immutable")


# =============================================================================
# Legal Transitions Definition
# =============================================================================

# Mapping: current_state -> {operation: next_state}
# Only these transitions are legal. All others raise ConfigurationTransitionError.
_LEGAL_TRANSITIONS: Final[dict[ConfigurationStatus, dict[str, ConfigurationStatus]]] = {
    ConfigurationStatus.DRAFT: {
        "edit": ConfigurationStatus.DRAFT,
        "start_validation": ConfigurationStatus.VALIDATING,
    },
    ConfigurationStatus.VALIDATING: {
        "validation_succeeded": ConfigurationStatus.VALIDATED,
        "validation_failed": ConfigurationStatus.DRAFT,
    },
    ConfigurationStatus.VALIDATED: {
        "publish": ConfigurationStatus.PUBLISHED,
    },
    ConfigurationStatus.PUBLISHED: {
        # No transitions from PUBLISHED - it is terminal
    },
}

# Operations that require the version to be mutable (not PUBLISHED)
_MUTABLE_STATES: Final[set[ConfigurationStatus]] = {
    ConfigurationStatus.DRAFT,
    ConfigurationStatus.VALIDATING,
    ConfigurationStatus.VALIDATED,
}

# Operations that are idempotent (safe to call multiple times)
_IDEMPOTENT_OPERATIONS: Final[set[str]] = {"publish"}


@dataclass(frozen=True)
class TransitionResult:
    """Result of a state transition attempt."""

    success: bool
    new_state: ConfigurationStatus | None = None
    error: str | None = None


class ConfigurationStateMachine:
    """
    Strict state machine for ConfigurationVersion lifecycle.

    Guarantees:
    - Only legal transitions are allowed
    - PUBLISHED versions are immutable (no transitions out)
    - VALIDATING versions cannot be edited
    - Only VALIDATED versions can be published
    - Transitions are explicit operations, not generic status mutations
    """

    @staticmethod
    def can_transition(current: ConfigurationStatus, operation: str) -> bool:
        """Check if a transition is legal from the current state."""
        return operation in _LEGAL_TRANSITIONS.get(current, {})

    @staticmethod
    def get_next_state(current: ConfigurationStatus, operation: str) -> ConfigurationStatus | None:
        """Get the next state for a legal transition, or None if illegal."""
        return _LEGAL_TRANSITIONS.get(current, {}).get(operation)

    @staticmethod
    def get_allowed_operations(current: ConfigurationStatus) -> list[str]:
        """Get list of allowed operations from the current state."""
        return list(_LEGAL_TRANSITIONS.get(current, {}).keys())

    @staticmethod
    def is_mutable(state: ConfigurationStatus) -> bool:
        """Check if a version in this state can be modified."""
        return state in _MUTABLE_STATES

    @staticmethod
    def is_terminal(state: ConfigurationStatus) -> bool:
        """Check if a state is terminal (no outgoing transitions)."""
        return state == ConfigurationStatus.PUBLISHED

    @staticmethod
    def transition(
        current: ConfigurationStatus,
        operation: str,
        *,
        allow_idempotent: bool = True,
    ) -> TransitionResult:
        """
        Execute a state transition.

        Args:
            current: Current state of the configuration version
            operation: Operation to perform (edit, start_validation, validation_succeeded,
                      validation_failed, publish)
            allow_idempotent: If True, allow idempotent operations on terminal states

        Returns:
            TransitionResult with success status and new state (if successful)

        Raises:
            ConfigurationTransitionError: If transition is illegal (unless idempotent)
        """
        # Handle idempotent publish on already PUBLISHED
        if (
            allow_idempotent
            and operation in _IDEMPOTENT_OPERATIONS
            and current == ConfigurationStatus.PUBLISHED
            and operation == "publish"
        ):
            return TransitionResult(success=True, new_state=current)

        next_state = ConfigurationStateMachine.get_next_state(current, operation)

        if next_state is None:
            allowed = ConfigurationStateMachine.get_allowed_operations(current)
            raise ConfigurationTransitionError(current, operation, allowed)

        return TransitionResult(success=True, new_state=next_state)

    @staticmethod
    def assert_can_edit(state: ConfigurationStatus, version_id: str) -> None:
        """Assert that a version in this state can be edited."""
        if not ConfigurationStateMachine.is_mutable(state):
            raise ConfigurationImmutableError(version_id)
        if state == ConfigurationStatus.VALIDATING:
            raise ConfigurationTransitionError(
                state, "edit", ["validation_succeeded", "validation_failed"]
            )
        if state == ConfigurationStatus.VALIDATED:
            raise ConfigurationTransitionError(state, "edit", ["publish"])

    @staticmethod
    def assert_can_validate(state: ConfigurationStatus, version_id: str) -> None:
        """Assert that validation can be started."""
        if state != ConfigurationStatus.DRAFT:
            allowed = ConfigurationStateMachine.get_allowed_operations(state)
            raise ConfigurationTransitionError(state, "start_validation", allowed)

    @staticmethod
    def assert_can_publish(state: ConfigurationStatus, version_id: str) -> None:
        """Assert that publication can proceed."""
        if state != ConfigurationStatus.VALIDATED:
            allowed = ConfigurationStateMachine.get_allowed_operations(state)
            raise ConfigurationTransitionError(state, "publish", allowed)


# =============================================================================
# State Invariants (from Task 10.3 §35)
# =============================================================================

STATE_INVARIANTS: Final[dict[str, str]] = {
    "STATE-01": "Only DRAFT versions may be edited.",
    "STATE-02": "Only DRAFT versions may enter VALIDATING.",
    "STATE-03": "VALIDATING versions are immutable during validation.",
    "STATE-04": "Only successful validation can produce VALIDATED.",
    "STATE-05": "Only VALIDATED versions can become PUBLISHED.",
    "STATE-06": "PUBLISHED versions are immutable.",
    "STATE-07": "PUBLISHED versions can never transition backward.",
    "STATE-08": "Validation failure returns the version to DRAFT.",
    "STATE-09": "Publishing is atomic.",
    "STATE-10": "Only one published version may be current for a venue.",
    "STATE-11": "Replacing the current version does not mutate the previous published version.",
    "STATE-12": "A session pins exactly one published version.",
    "STATE-13": "A session's pinned version never changes.",
    "STATE-14": "A failed publication leaves the version VALIDATED.",
    "STATE-15": "Event delivery failure cannot roll back an already committed publication; the outbox retries it.",
    "STATE-16": "Every transition is tenant/venue authorized.",
    "STATE-17": "Transition requests must be idempotent where practical.",
    "STATE-18": "Version numbers are monotonically increasing and never reused.",
}


def validate_state_invariant(invariant_id: str, condition: bool, message: str) -> None:
    """Validate a state invariant, raising if violated."""
    if not condition:
        raise ValueError(f"Invariant {invariant_id} violated: {message}")
