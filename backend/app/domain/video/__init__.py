"""Video domain — live session FSM and ingestion boundary (Task 19.2)."""

from backend.app.domain.video.live_session_state_machine import (
    LIVE_SESSION_INVARIANTS,
    LiveSessionTerminalError,
    LiveSessionTransitionError,
    LiveVideoSessionStateMachine,
    LiveSessionTransitionRecord,
    TransitionResult,
    validate_live_session_invariant,
)

__all__ = [
    "LIVE_SESSION_INVARIANTS",
    "LiveSessionTerminalError",
    "LiveSessionTransitionError",
    "LiveVideoSessionStateMachine",
    "LiveSessionTransitionRecord",
    "TransitionResult",
    "validate_live_session_invariant",
]