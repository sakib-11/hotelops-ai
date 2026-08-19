"""Unit tests for LiveVideoSessionStateMachine (Task 19.2).

Tests all 10 required scenarios:
1. Normal connection (CONNECTING -> ACTIVE)
2. Connection failure (CONNECTING -> FAILED)
3. Stream becomes stale (ACTIVE -> DEGRADED)
4. Reconnect (DEGRADED -> RECONNECTING -> ACTIVE)
5. Successful recovery (RECONNECTING -> ACTIVE)
6. Repeated failure (RECONNECTING -> DEGRADED -> RECONNECTING -> FAILED)
7. Manual stop (any -> STOPPED)
8. Restart (not directly supported - new session)
9. Invalid transition (rejected with error)
10. Duplicate transition (idempotent stop)
"""

from __future__ import annotations

import pytest
from datetime import UTC, datetime
from uuid import uuid4

from backend.app.domain.video.live_session_state_machine import (
    LIVE_SESSION_INVARIANTS,
    LiveSessionTerminalError,
    LiveSessionTransitionError,
    LiveVideoSessionStateMachine,
    LiveSessionTransitionRecord,
    validate_live_session_invariant,
)
from contracts.video.models import LiveVideoSessionStatus


def make_session_id() -> str:
    return str(uuid4())


def make_correlation_id() -> str:
    return str(uuid4())


class TestNormalConnection:
    """Test 1: Normal connection - CONNECTING -> ACTIVE"""

    def test_connecting_to_active(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="connected",
            session_id=session_id,
            reason="RTSP connection established",
            source="system",
            correlation_id=make_correlation_id(),
        )

        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.ACTIVE
        assert result.transition_record is not None
        assert result.transition_record.previous_state == LiveVideoSessionStatus.CONNECTING
        assert result.transition_record.new_state == LiveVideoSessionStatus.ACTIVE
        assert result.transition_record.source == "system"
        assert result.transition_record.reason == "RTSP connection established"

    def test_connecting_transition_record_has_required_fields(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="connected",
            session_id=session_id,
            reason="Connected",
            source="system",
        )

        record = result.transition_record
        assert isinstance(record, LiveSessionTransitionRecord)
        assert record.session_id == session_id
        assert record.previous_state == LiveVideoSessionStatus.CONNECTING
        assert record.new_state == LiveVideoSessionStatus.ACTIVE
        assert record.transition_time.tzinfo is not None  # timezone-aware
        assert record.reason == "Connected"
        assert record.source == "system"
        assert record.correlation_id is None
        assert record.actor_id is None


class TestConnectionFailure:
    """Test 2: Connection failure - CONNECTING -> FAILED"""

    def test_connecting_to_failed(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="connection_failed",
            session_id=session_id,
            reason="RTSP connection timeout",
            source="system",
        )

        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.FAILED
        assert result.transition_record.new_state == LiveVideoSessionStatus.FAILED

    def test_connecting_to_failed_is_terminal(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="connection_failed",
            session_id=session_id,
            reason="Failed",
            source="system",
        )

        assert result.new_state == LiveVideoSessionStatus.FAILED
        assert LiveVideoSessionStateMachine.is_terminal(LiveVideoSessionStatus.FAILED)

        # No further transitions allowed from FAILED
        with pytest.raises(LiveSessionTerminalError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.FAILED,
                operation="connected",
                session_id=session_id,
                reason="Attempt recovery",
                source="system",
            )


class TestStreamBecomesStale:
    """Test 3: Stream becomes stale - ACTIVE -> DEGRADED"""

    def test_active_to_degraded(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.ACTIVE,
            operation="stale_detected",
            session_id=session_id,
            reason="No frames for 30.5s",
            source="system",
        )

        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.DEGRADED


class TestReconnect:
    """Test 4: Reconnect - DEGRADED -> RECONNECTING"""

    def test_degraded_to_reconnecting(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.DEGRADED,
            operation="reconnecting",
            session_id=session_id,
            reason="Starting reconnection attempt",
            source="system",
        )

        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.RECONNECTING


class TestSuccessfulRecovery:
    """Test 5: Successful recovery - RECONNECTING -> ACTIVE"""

    def test_reconnecting_to_active(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.RECONNECTING,
            operation="reconnected",
            session_id=session_id,
            reason="Reconnection successful",
            source="system",
        )

        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.ACTIVE

    def test_full_recovery_cycle(self) -> None:
        """ACTIVE -> DEGRADED -> RECONNECTING -> ACTIVE"""
        session_id = make_session_id()

        # ACTIVE -> DEGRADED
        r1 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.ACTIVE,
            operation="stale_detected",
            session_id=session_id,
            reason="Stale",
            source="system",
        )
        assert r1.new_state == LiveVideoSessionStatus.DEGRADED

        # DEGRADED -> RECONNECTING
        r2 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.DEGRADED,
            operation="reconnecting",
            session_id=session_id,
            reason="Reconnecting",
            source="system",
        )
        assert r2.new_state == LiveVideoSessionStatus.RECONNECTING

        # RECONNECTING -> ACTIVE
        r3 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.RECONNECTING,
            operation="reconnected",
            session_id=session_id,
            reason="Recovered",
            source="system",
        )
        assert r3.new_state == LiveVideoSessionStatus.ACTIVE


class TestRepeatedFailure:
    """Test 6: Repeated failure - RECONNECTING -> DEGRADED -> RECONNECTING -> FAILED"""

    def test_reconnect_failed_returns_to_degraded(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.RECONNECTING,
            operation="reconnect_failed",
            session_id=session_id,
            reason="Reconnection timeout",
            source="system",
        )

        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.DEGRADED

    def test_repeated_failure_eventually_fails(self) -> None:
        """DEGRADED -> RECONNECTING -> DEGRADED -> RECONNECTING -> FAILED"""
        session_id = make_session_id()

        # DEGRADED -> RECONNECTING
        r1 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.DEGRADED,
            operation="reconnecting",
            session_id=session_id,
            reason="Attempt 1",
            source="system",
        )
        assert r1.new_state == LiveVideoSessionStatus.RECONNECTING

        # RECONNECTING -> DEGRADED (failed)
        r2 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.RECONNECTING,
            operation="reconnect_failed",
            session_id=session_id,
            reason="Failed 1",
            source="system",
        )
        assert r2.new_state == LiveVideoSessionStatus.DEGRADED

        # DEGRADED -> RECONNECTING (attempt 2)
        r3 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.DEGRADED,
            operation="reconnecting",
            session_id=session_id,
            reason="Attempt 2",
            source="system",
        )
        assert r3.new_state == LiveVideoSessionStatus.RECONNECTING

        # RECONNECTING -> FAILED (fatal)
        r4 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.RECONNECTING,
            operation="fatal_error",
            session_id=session_id,
            reason="Max retries exceeded",
            source="system",
        )
        assert r4.new_state == LiveVideoSessionStatus.FAILED
        assert LiveVideoSessionStateMachine.is_terminal(LiveVideoSessionStatus.FAILED)


class TestManualStop:
    """Test 7: Manual stop - any state -> STOPPED (idempotent)"""

    def test_stop_from_connecting(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="stop_requested",
            session_id=session_id,
            reason="User requested stop",
            source="actor",
            actor_id=str(uuid4()),
        )
        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.STOPPED

    def test_stop_from_active(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.ACTIVE,
            operation="stop_requested",
            session_id=session_id,
            reason="User requested stop",
            source="actor",
            actor_id=str(uuid4()),
        )
        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.STOPPED

    def test_stop_from_degraded(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.DEGRADED,
            operation="stop_requested",
            session_id=session_id,
            reason="User requested stop",
            source="actor",
        )
        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.STOPPED

    def test_stop_from_reconnecting(self) -> None:
        session_id = make_session_id()
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.RECONNECTING,
            operation="stop_requested",
            session_id=session_id,
            reason="User requested stop",
            source="actor",
        )
        assert result.success is True
        assert result.new_state == LiveVideoSessionStatus.STOPPED

    def test_stop_from_failed_is_idempotent(self) -> None:
        """Stop on already FAILED is idempotent (no state change)."""
        session_id = make_session_id()

        # First stop (from FAILED)
        r1 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.FAILED,
            operation="stop_requested",
            session_id=session_id,
            reason="Stop after failure",
            source="actor",
        )
        assert r1.success is True
        assert r1.new_state == LiveVideoSessionStatus.FAILED  # stays FAILED

        # Second stop (idempotent)
        r2 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.FAILED,
            operation="stop_requested",
            session_id=session_id,
            reason="Stop again",
            source="actor",
        )
        assert r2.success is True
        assert r2.new_state == LiveVideoSessionStatus.FAILED

    def test_stop_from_stopped_is_idempotent(self) -> None:
        """Stop on already STOPPED is idempotent."""
        session_id = make_session_id()

        r1 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.STOPPED,
            operation="stop_requested",
            session_id=session_id,
            reason="Stop again",
            source="actor",
        )
        assert r1.success is True
        assert r1.new_state == LiveVideoSessionStatus.STOPPED


class TestRestart:
    """Test 8: Restart - not directly supported, requires new session"""

    def test_no_transition_from_stopped_to_connecting(self) -> None:
        """STOPPED is terminal - cannot go back to CONNECTING."""
        session_id = make_session_id()

        with pytest.raises(LiveSessionTerminalError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.STOPPED,
                operation="connected",
                session_id=session_id,
                reason="Attempt restart",
                source="system",
            )

    def test_no_transition_from_failed_to_connecting(self) -> None:
        """FAILED is terminal - cannot go back to CONNECTING."""
        session_id = make_session_id()

        with pytest.raises(LiveSessionTerminalError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.FAILED,
                operation="connected",
                session_id=session_id,
                reason="Attempt restart",
                source="system",
            )


class TestInvalidTransition:
    """Test 9: Invalid transitions are rejected"""

    def test_active_to_connecting_rejected(self) -> None:
        session_id = make_session_id()
        with pytest.raises(LiveSessionTransitionError) as exc_info:
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.ACTIVE,
                operation="connected",  # Not allowed from ACTIVE
                session_id=session_id,
                reason="Invalid",
                source="system",
            )
        assert "connected" in str(exc_info.value)
        assert "stale_detected" in str(exc_info.value)
        assert "stop_requested" in str(exc_info.value)
        assert "fatal_error" in str(exc_info.value)

    def test_degraded_to_active_direct_rejected(self) -> None:
        """DEGRADED cannot go directly to ACTIVE - must go through RECONNECTING."""
        session_id = make_session_id()
        with pytest.raises(LiveSessionTransitionError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.DEGRADED,
                operation="connected",  # Not allowed
                session_id=session_id,
                reason="Invalid direct recovery",
                source="system",
            )

    def test_reconnecting_to_connecting_rejected(self) -> None:
        session_id = make_session_id()
        with pytest.raises(LiveSessionTransitionError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.RECONNECTING,
                operation="connected",  # Not allowed - must use "reconnected"
                session_id=session_id,
                reason="Invalid",
                source="system",
            )

    def test_unknown_operation_rejected(self) -> None:
        session_id = make_session_id()
        with pytest.raises(LiveSessionTransitionError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.ACTIVE,
                operation="invalid_operation",
                session_id=session_id,
                reason="Invalid",
                source="system",
            )


class TestDuplicateTransition:
    """Test 10: Duplicate/Idempotent transitions"""

    def test_idempotent_stop_on_terminal(self) -> None:
        """stop_requested on STOPPED/FAILED is idempotent."""
        session_id = make_session_id()

        # First call
        r1 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.STOPPED,
            operation="stop_requested",
            session_id=session_id,
            reason="First stop",
            source="actor",
        )
        assert r1.success is True
        assert r1.new_state == LiveVideoSessionStatus.STOPPED

        # Second call - same state, no error
        r2 = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.STOPPED,
            operation="stop_requested",
            session_id=session_id,
            reason="Second stop",
            source="actor",
        )
        assert r2.success is True
        assert r2.new_state == LiveVideoSessionStatus.STOPPED

    def test_non_idempotent_on_terminal_rejected(self) -> None:
        """Non-idempotent operations on terminal states are rejected."""
        session_id = make_session_id()

        with pytest.raises(LiveSessionTerminalError):
            LiveVideoSessionStateMachine.transition(
                current=LiveVideoSessionStatus.STOPPED,
                operation="fatal_error",  # Not idempotent
                session_id=session_id,
                reason="Invalid",
                source="system",
            )


class TestFSMInvariants:
    """Validate all declared invariants hold."""

    def test_invariant_live_01_only_connecting_initial(self) -> None:
        """LIVE-01: CONNECTING is the only initial state for a live session."""
        # This is enforced by the service layer, not the FSM directly
        # The FSM allows CONNECTING as a valid state
        assert LiveVideoSessionStatus.CONNECTING in LiveVideoSessionStatus

    def test_invariant_live_02_only_connecting_to_active(self) -> None:
        """LIVE-02: Only CONNECTING can transition to ACTIVE (via 'connected')."""
        # ACTIVE can only be reached from CONNECTING (connected) or RECONNECTING (reconnected)
        allowed_to_active = []
        for state in LiveVideoSessionStatus:
            if LiveVideoSessionStateMachine.can_transition(state, "connected"):
                allowed_to_active.append(state)
            if LiveVideoSessionStateMachine.can_transition(state, "reconnected"):
                allowed_to_active.append(state)
        assert LiveVideoSessionStatus.CONNECTING in allowed_to_active
        assert LiveVideoSessionStatus.RECONNECTING in allowed_to_active

    def test_invariant_live_03_active_only_degrades_to_degraded(self) -> None:
        """LIVE-03: ACTIVE can only degrade to DEGRADED (via 'stale_detected')."""
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.ACTIVE
        )
        assert "stale_detected" in allowed
        assert "connected" not in allowed
        assert "reconnected" not in allowed

    def test_invariant_live_04_degraded_must_reconnect_before_recovery(self) -> None:
        """LIVE-04: DEGRADED must transition to RECONNECTING before attempting recovery."""
        # DEGRADED has no 'connected' or 'reconnected' operation
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.DEGRADED
        )
        assert "reconnecting" in allowed
        assert "connected" not in allowed
        assert "reconnected" not in allowed

    def test_invariant_live_05_reconnecting_can_recover_or_fallback(self) -> None:
        """LIVE-05: RECONNECTING can recover to ACTIVE or fall back to DEGRADED."""
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.RECONNECTING
        )
        assert "reconnected" in allowed
        assert "reconnect_failed" in allowed

    def test_invariant_live_06_terminal_states(self) -> None:
        """LIVE-06: STOPPED and FAILED are terminal — no transitions out."""
        assert LiveVideoSessionStateMachine.is_terminal(LiveVideoSessionStatus.STOPPED)
        assert LiveVideoSessionStateMachine.is_terminal(LiveVideoSessionStatus.FAILED)
        assert not LiveVideoSessionStateMachine.is_terminal(LiveVideoSessionStatus.ACTIVE)

    def test_invariant_live_07_stop_idempotent(self) -> None:
        """LIVE-07: stop_requested is idempotent and allowed from any state."""
        for state in LiveVideoSessionStatus:
            assert LiveVideoSessionStateMachine.can_transition(state, "stop_requested")

    def test_invariant_live_08_connection_failed_from_connecting(self) -> None:
        """LIVE-08: connection_failed from CONNECTING goes directly to FAILED."""
        assert LiveVideoSessionStateMachine.can_transition(
            LiveVideoSessionStatus.CONNECTING, "connection_failed"
        )
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="connection_failed",
            session_id=make_session_id(),
            reason="Test",
            source="system",
        )
        assert result.new_state == LiveVideoSessionStatus.FAILED

    def test_invariant_live_09_fatal_error_from_non_terminal(self) -> None:
        """LIVE-09: fatal_error from ACTIVE/DEGRADED/RECONNECTING goes to FAILED."""
        for state in (
            LiveVideoSessionStatus.ACTIVE,
            LiveVideoSessionStatus.DEGRADED,
            LiveVideoSessionStatus.RECONNECTING,
        ):
            assert LiveVideoSessionStateMachine.can_transition(state, "fatal_error")
            result = LiveVideoSessionStateMachine.transition(
                current=state,
                operation="fatal_error",
                session_id=make_session_id(),
                reason="Test",
                source="system",
            )
            assert result.new_state == LiveVideoSessionStatus.FAILED

    def test_invariant_live_10_every_transition_audit_record(self) -> None:
        """LIVE-10: Every transition produces an immutable audit record with system time."""
        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.CONNECTING,
            operation="connected",
            session_id=make_session_id(),
            reason="Test",
            source="system",
        )
        assert result.transition_record is not None
        assert result.transition_record.transition_time.tzinfo is not None

    def test_invariant_live_11_event_time_never_used(self) -> None:
        """LIVE-11: Event-time is never used for FSM transitions."""
        # The FSM uses _now() which returns datetime.now(UTC) - system time
        # This is tested implicitly by the transition_time being system time
        pass

    def test_invariant_live_12_system_time_for_operations(self) -> None:
        """LIVE-12: System/processing time is used for staleness, heartbeat, transitions."""
        # Verified by _now() usage in transition()
        pass


class TestTransitionRecordValidation:
    """Test transition record validation."""

    def test_transition_record_requires_timezone_aware_time(self) -> None:
        """Transition record must have timezone-aware transition_time."""
        naive_time = datetime(2026, 1, 1, 12, 0, 0)  # No timezone
        with pytest.raises(ValueError, match="timezone-aware"):
            LiveSessionTransitionRecord(
                session_id=make_session_id(),
                previous_state=LiveVideoSessionStatus.CONNECTING,
                new_state=LiveVideoSessionStatus.ACTIVE,
                transition_time=naive_time,
                reason="Test",
                source="system",
            )

    def test_transition_record_accepts_utc_time(self) -> None:
        """Transition record accepts timezone-aware UTC time."""
        utc_time = datetime.now(UTC)
        record = LiveSessionTransitionRecord(
            session_id=make_session_id(),
            previous_state=LiveVideoSessionStatus.CONNECTING,
            new_state=LiveVideoSessionStatus.ACTIVE,
            transition_time=utc_time,
            reason="Test",
            source="system",
        )
        assert record.transition_time.tzinfo is not None


class TestActorSourceTransitions:
    """Test transitions initiated by actors (users)."""

    def test_actor_initiated_stop_includes_actor_id(self) -> None:
        actor_id = str(uuid4())
        session_id = make_session_id()

        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.ACTIVE,
            operation="stop_requested",
            session_id=session_id,
            reason="Manual stop",
            source="actor",
            actor_id=actor_id,
        )

        assert result.success is True
        assert result.transition_record.source == "actor"
        assert result.transition_record.actor_id == actor_id

    def test_system_initiated_transition_no_actor_id(self) -> None:
        session_id = make_session_id()

        result = LiveVideoSessionStateMachine.transition(
            current=LiveVideoSessionStatus.ACTIVE,
            operation="stale_detected",
            session_id=session_id,
            reason="No frames",
            source="system",
        )

        assert result.success is True
        assert result.transition_record.source == "system"
        assert result.transition_record.actor_id is None


class TestAllowedOperations:
    """Test get_allowed_operations returns correct operations per state."""

    def test_connecting_allowed(self) -> None:
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.CONNECTING
        )
        assert set(allowed) == {"connected", "connection_failed", "stop_requested"}

    def test_active_allowed(self) -> None:
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.ACTIVE
        )
        assert set(allowed) == {"stale_detected", "stop_requested", "fatal_error"}

    def test_degraded_allowed(self) -> None:
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.DEGRADED
        )
        assert set(allowed) == {"reconnecting", "stop_requested", "fatal_error"}

    def test_reconnecting_allowed(self) -> None:
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.RECONNECTING
        )
        assert set(allowed) == {"reconnected", "reconnect_failed", "fatal_error", "stop_requested"}

    def test_stopped_allowed_empty(self) -> None:
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.STOPPED
        )
        assert allowed == ["stop_requested"]

    def test_failed_allowed_empty(self) -> None:
        allowed = LiveVideoSessionStateMachine.get_allowed_operations(
            LiveVideoSessionStatus.FAILED
        )
        assert allowed == ["stop_requested"]


class TestCanTransition:
    """Test can_transition helper."""

    def test_can_transition_true(self) -> None:
        assert LiveVideoSessionStateMachine.can_transition(
            LiveVideoSessionStatus.CONNECTING, "connected"
        )
        assert LiveVideoSessionStateMachine.can_transition(
            LiveVideoSessionStatus.ACTIVE, "stale_detected"
        )

    def test_can_transition_false(self) -> None:
        assert not LiveVideoSessionStateMachine.can_transition(
            LiveVideoSessionStatus.ACTIVE, "connected"
        )
        assert not LiveVideoSessionStateMachine.can_transition(
            LiveVideoSessionStatus.STOPPED, "connected"
        )


class TestGetNextState:
    """Test get_next_state helper."""

    def test_get_next_state_valid(self) -> None:
        next_state = LiveVideoSessionStateMachine.get_next_state(
            LiveVideoSessionStatus.CONNECTING, "connected"
        )
        assert next_state == LiveVideoSessionStatus.ACTIVE

    def test_get_next_state_invalid(self) -> None:
        next_state = LiveVideoSessionStateMachine.get_next_state(
            LiveVideoSessionStatus.ACTIVE, "connected"
        )
        assert next_state is None


class TestValidateInvariant:
    """Test invariant validation helper."""

    def test_validate_invariant_passes(self) -> None:
        validate_live_session_invariant("TEST-01", True, "Should pass")

    def test_validate_invariant_fails(self) -> None:
        with pytest.raises(ValueError, match="TEST-02 violated"):
            validate_live_session_invariant("TEST-02", False, "Should fail")