"""Live Video Session State Machine (Task 19.2).

Enforces the authoritative operational lifecycle for live video ingestion:

    CONNECTING → ACTIVE
    CONNECTING → FAILED
    CONNECTING → STOPPED

    ACTIVE → DEGRADED
    ACTIVE → STOPPED
    ACTIVE → FAILED

    DEGRADED → RECONNECTING
    DEGRADED → STOPPED
    DEGRADED → FAILED

    RECONNECTING → ACTIVE
    RECONNECTING → DEGRADED
    RECONNECTING → FAILED
    RECONNECTING → STOPPED

No backward transitions to CONNECTING. STOPPED and FAILED are terminal.
All transitions are explicit operations with full audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from contracts.video.models import LiveVideoSessionStatus


class LiveSessionTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(
        self,
        current: LiveVideoSessionStatus,
        attempted: str,
        allowed: list[str],
        session_id: str,
    ):
        self.current = current
        self.attempted = attempted
        self.allowed = allowed
        self.session_id = session_id
        super().__init__(
            f"Session {session_id}: invalid transition from {current.value}: "
            f"'{attempted}' not allowed. Allowed: {', '.join(allowed) if allowed else 'none'}"
        )


class LiveSessionTerminalError(Exception):
    """Raised when attempting to transition from a terminal state."""

    def __init__(self, session_id: str, state: LiveVideoSessionStatus):
        self.session_id = session_id
        self.state = state
        super().__init__(
            f"Session {session_id} is in terminal state {state.value}; "
            f"no further transitions allowed"
        )


# =============================================================================
# Legal Transitions Definition
# =============================================================================

# Mapping: current_state -> {operation: next_state}
# Only these transitions are legal. All others raise LiveSessionTransitionError.
_LEGAL_TRANSITIONS: Final[dict[LiveVideoSessionStatus, dict[str, LiveVideoSessionStatus]]] = {
    LiveVideoSessionStatus.CONNECTING: {
        "connected": LiveVideoSessionStatus.ACTIVE,
        "connection_failed": LiveVideoSessionStatus.FAILED,
        "stop_requested": LiveVideoSessionStatus.STOPPED,
    },
    LiveVideoSessionStatus.ACTIVE: {
        "stale_detected": LiveVideoSessionStatus.DEGRADED,
        "stop_requested": LiveVideoSessionStatus.STOPPED,
        "fatal_error": LiveVideoSessionStatus.FAILED,
    },
    LiveVideoSessionStatus.DEGRADED: {
        "reconnecting": LiveVideoSessionStatus.RECONNECTING,
        "stop_requested": LiveVideoSessionStatus.STOPPED,
        "fatal_error": LiveVideoSessionStatus.FAILED,
    },
    LiveVideoSessionStatus.RECONNECTING: {
        "reconnected": LiveVideoSessionStatus.ACTIVE,
        "reconnect_failed": LiveVideoSessionStatus.DEGRADED,
        "fatal_error": LiveVideoSessionStatus.FAILED,
        "stop_requested": LiveVideoSessionStatus.STOPPED,
    },
    LiveVideoSessionStatus.STOPPED: {
        # Terminal - no outgoing transitions
    },
    LiveVideoSessionStatus.FAILED: {
        # Terminal - no outgoing transitions
    },
}

# Terminal states - no transitions out
_TERMINAL_STATES: Final[set[LiveVideoSessionStatus]] = {
    LiveVideoSessionStatus.STOPPED,
    LiveVideoSessionStatus.FAILED,
}

# Operations that are idempotent (safe to call multiple times)
_IDEMPOTENT_OPERATIONS: Final[set[str]] = {"stop_requested"}


@dataclass(frozen=True, slots=True)
class LiveSessionTransitionRecord:
    """Immutable record of a single state transition.

    Persisted to database for audit trail and operational debugging.
    Uses system/processing time (transition_time), NOT event-time.
    """

    session_id: str
    previous_state: LiveVideoSessionStatus
    new_state: LiveVideoSessionStatus
    transition_time: datetime
    reason: str
    source: str  # "system" or "actor"
    correlation_id: str | None = None
    actor_id: str | None = None

    def __post_init__(self) -> None:
        if self.transition_time.tzinfo is None:
            raise ValueError("transition_time must be timezone-aware (UTC)")


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Result of a state transition attempt."""

    success: bool
    new_state: LiveVideoSessionStatus | None = None
    error: str | None = None
    transition_record: LiveSessionTransitionRecord | None = None


def _now() -> datetime:
    """Current system/processing time (UTC). Not event-time."""
    return datetime.now(UTC)


class LiveVideoSessionStateMachine:
    """
    Strict state machine for LiveVideoSession operational lifecycle.

    Guarantees:
    - Only legal transitions are allowed
    - Terminal states (STOPPED, FAILED) have no outgoing transitions
    - Every transition produces an immutable audit record
    - Transitions are explicit operations, not generic status mutations
    - Idempotent stop_requested on terminal states
    """

    @staticmethod
    def can_transition(current: LiveVideoSessionStatus, operation: str) -> bool:
        """Check if a transition is legal from the current state."""
        # Check regular transitions
        if operation in _LEGAL_TRANSITIONS.get(current, {}):
            return True
        # Check idempotent operations on terminal states
        if current in _TERMINAL_STATES and operation in _IDEMPOTENT_OPERATIONS:
            return True
        return False

    @staticmethod
    def get_next_state(
        current: LiveVideoSessionStatus, operation: str
    ) -> LiveVideoSessionStatus | None:
        """Get the next state for a legal transition, or None if illegal."""
        return _LEGAL_TRANSITIONS.get(current, {}).get(operation)

    @staticmethod
    def get_allowed_operations(current: LiveVideoSessionStatus) -> list[str]:
        """Get list of allowed operations from the current state."""
        operations = list(_LEGAL_TRANSITIONS.get(current, {}).keys())
        # Include idempotent operations for terminal states
        if current in _TERMINAL_STATES:
            operations.extend(_IDEMPOTENT_OPERATIONS)
        return operations

    @staticmethod
    def is_terminal(state: LiveVideoSessionStatus) -> bool:
        """Check if a state is terminal (no outgoing transitions)."""
        return state in _TERMINAL_STATES

    @staticmethod
    def transition(
        current: LiveVideoSessionStatus,
        operation: str,
        *,
        session_id: str,
        reason: str,
        source: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
        allow_idempotent: bool = True,
    ) -> TransitionResult:
        """
        Execute a state transition with full audit record.

        Args:
            current: Current state of the session
            operation: Operation to perform (connected, connection_failed, stale_detected,
                       reconnecting, reconnected, reconnect_failed, fatal_error, stop_requested)
            session_id: Unique session identifier
            reason: Human-readable reason for the transition
            source: "system" for automated transitions, "actor" for manual
            correlation_id: Optional correlation ID for request tracing
            actor_id: Optional actor ID when source="actor"
            allow_idempotent: If True, allow idempotent operations on terminal states

        Returns:
            TransitionResult with success status, new state, and transition record

        Raises:
            LiveSessionTransitionError: If transition is illegal (unless idempotent)
            LiveSessionTerminalError: If attempting non-idempotent transition from terminal state
        """
        # Handle idempotent stop_requested on already terminal states
        if (
            allow_idempotent
            and operation in _IDEMPOTENT_OPERATIONS
            and current in _TERMINAL_STATES
            and operation == "stop_requested"
        ):
            transition_time = _now()
            record = LiveSessionTransitionRecord(
                session_id=session_id,
                previous_state=current,
                new_state=current,
                transition_time=transition_time,
                reason=reason,
                source=source,
                correlation_id=correlation_id,
                actor_id=actor_id,
            )
            return TransitionResult(
                success=True, new_state=current, transition_record=record
            )

        # Reject non-idempotent transitions from terminal states
        if current in _TERMINAL_STATES:
            raise LiveSessionTerminalError(session_id, current)

        next_state = LiveVideoSessionStateMachine.get_next_state(current, operation)

        if next_state is None:
            allowed = LiveVideoSessionStateMachine.get_allowed_operations(current)
            raise LiveSessionTransitionError(current, operation, allowed, session_id)

        transition_time = _now()
        record = LiveSessionTransitionRecord(
            session_id=session_id,
            previous_state=current,
            new_state=next_state,
            transition_time=transition_time,
            reason=reason,
            source=source,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )

        return TransitionResult(success=True, new_state=next_state, transition_record=record)

    @staticmethod
    def assert_can_transition(
        current: LiveVideoSessionStatus, operation: str, session_id: str
    ) -> None:
        """Assert that a transition is legal, raising with context if not."""
        if current in _TERMINAL_STATES:
            if not (operation in _IDEMPOTENT_OPERATIONS and operation == "stop_requested"):
                raise LiveSessionTerminalError(session_id, current)
        if not LiveVideoSessionStateMachine.can_transition(current, operation):
            allowed = LiveVideoSessionStateMachine.get_allowed_operations(current)
            raise LiveSessionTransitionError(current, operation, allowed, session_id)


# =============================================================================
# State Invariants
# =============================================================================

LIVE_SESSION_INVARIANTS: Final[dict[str, str]] = {
    "LIVE-01": "CONNECTING is the only initial state for a live session",
    "LIVE-02": "Only CONNECTING can transition to ACTIVE (via 'connected')",
    "LIVE-03": "ACTIVE can only degrade to DEGRADED (via 'stale_detected')",
    "LIVE-04": "DEGRADED must transition to RECONNECTING before attempting recovery",
    "LIVE-05": "RECONNECTING can recover to ACTIVE or fall back to DEGRADED",
    "LIVE-06": "STOPPED and FAILED are terminal — no transitions out",
    "LIVE-07": "stop_requested is idempotent and allowed from any state",
    "LIVE-08": "connection_failed from CONNECTING goes directly to FAILED",
    "LIVE-09": "fatal_error from ACTIVE/DEGRADED/RECONNECTING goes to FAILED",
    "LIVE-10": "Every transition produces an immutable audit record with system time",
    "LIVE-11": "Event-time (FramePacket.event_time) is never used for FSM transitions",
    "LIVE-12": "System/processing time is used for staleness, heartbeat, and transitions",
}


def validate_live_session_invariant(invariant_id: str, condition: bool, message: str) -> None:
    """Validate a live session state invariant, raising if violated."""
    if not condition:
        raise ValueError(f"Invariant {invariant_id} violated: {message}")