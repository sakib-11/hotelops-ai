"""Tests for Task 5.11 — WebSocket Authorization Foundation.

Tests cover:
- ChannelResourceType serialization and enum completeness
- SubscriptionRequest/Response contract validation
- ConnectionState creation
- authorize_channel_subscription:
  - Valid subscription (all conditions met)
  - Tenant mismatch (cross-tenant subscription blocked)
  - Unauthorized venue (venue-specific scope enforcement)
  - ALL_VENUES scope (tenant-wide access)
  - Missing required permission
  - Forged subscription (client cannot bypass)
- is_channel_accessible permission checks
- Edge cases: None venue_id, unknown channel type
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.app.infrastructure.auth.exceptions import AuthenticationError
from backend.app.infrastructure.auth.websocket import (
    authenticate_websocket_connection,
    authorize_channel_subscription,
    authorize_subscription_on_state,
    create_connection_state,
    is_channel_accessible,
    validate_connection_state,
)
from backend.app.infrastructure.config import Settings
from contracts.common import TenantId, UserId, VenueId
from contracts.identity import ActorContext, RoleName, permissions_for_role
from contracts.realtime import (
    ChannelResourceType,
    ConnectionState,
    SubscriptionRequest,
    SubscriptionResponse,
)

# =============================================================================
# Helpers
# =============================================================================


def _uid() -> str:
    return str(uuid4())


def _make_token(settings: Settings) -> str:
    """Create a JWT token for testing WebSocket connections."""

    from backend.app.infrastructure.auth.service import create_access_token

    return create_access_token(str(uuid4()), settings)


def _make_actor(
    user_id: str | None = None,
    tenant_id: str | None = None,
    role_name: RoleName = RoleName.OPERATOR,
    venue_ids: list[str] | None = None,
) -> ActorContext:
    """Create a test ActorContext."""
    uid = UserId(UUID(user_id or _uid()))
    tid = TenantId(UUID(tenant_id or _uid()))
    venues = frozenset(VenueId(UUID(v)) for v in (venue_ids or []))
    return ActorContext(
        actor_id=uid,
        tenant_id=tid,
        role_name=role_name,
        permissions=permissions_for_role(role_name),
        venue_scope=venues,
        authenticated_at=datetime.now(UTC),
        active=True,
    )


# =============================================================================
# ChannelResourceType Enum Tests
# =============================================================================


class TestChannelResourceType:
    """ChannelResourceType enum completeness and serialization."""

    def test_all_channels_defined(self) -> None:
        """All expected realtime channels are defined."""
        values = {c.value for c in ChannelResourceType}
        expected = {
            "video.feed",
            "analytics",
            "alerts",
            "evidence",
            "recommendations",
            "system",
        }
        assert values == expected

    def test_serializes_to_string(self) -> None:
        """Enum serializes to its string value."""
        assert ChannelResourceType.VIDEO_FEED.value == "video.feed"
        assert ChannelResourceType.ANALYTICS.value == "analytics"

    def test_invalid_channel_rejected(self) -> None:
        """Invalid channel string raises ValueError."""
        with pytest.raises(ValueError):
            ChannelResourceType("invalid_channel")


# =============================================================================
# SubscriptionRequest Contract Tests
# =============================================================================


class TestSubscriptionRequest:
    """SubscriptionRequest contract validation."""

    def test_valid_request(self) -> None:
        """A valid subscription request can be created."""
        tid = TenantId(uuid4())
        vid = VenueId(uuid4())
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=tid,
            venue_id=vid,
            resource_id="camera-01",
        )
        assert request.channel == ChannelResourceType.VIDEO_FEED
        assert request.tenant_id == tid
        assert request.venue_id == vid
        assert request.resource_id == "camera-01"

    def test_request_without_venue(self) -> None:
        """venue_id can be None for tenant-scoped subscriptions."""
        tid = TenantId(uuid4())
        request = SubscriptionRequest(
            channel=ChannelResourceType.ANALYTICS,
            tenant_id=tid,
        )
        assert request.venue_id is None

    def test_unknown_fields_rejected(self) -> None:
        """Extra fields in subscription request are rejected."""
        tid = TenantId(uuid4())
        with pytest.raises(ValidationError):
            SubscriptionRequest(
                channel=ChannelResourceType.ALERTS,
                tenant_id=tid,
                unknown_field="test",  # type: ignore[call-arg]
            )

    def test_frozen_immutable(self) -> None:
        """SubscriptionRequest should be immutable."""
        tid = TenantId(uuid4())
        request = SubscriptionRequest(
            channel=ChannelResourceType.SYSTEM,
            tenant_id=tid,
        )
        with pytest.raises(ValidationError):
            request.channel = ChannelResourceType.ALERTS  # type: ignore[misc]


# =============================================================================
# ConnectionState Tests
# =============================================================================


class TestConnectionState:
    """ConnectionState contract and creation."""

    def test_create_connection_state(self) -> None:
        """ConnectionState can be created with an ActorContext."""
        actor = _make_actor()
        conn_id = str(uuid4())
        state = ConnectionState(
            connection_id=conn_id,
            actor=actor,
        )
        assert state.connection_id == conn_id
        assert state.actor.actor_id == actor.actor_id
        assert state.connected_at.tzinfo is not None
        assert state.subscriptions == frozenset()

    def test_create_connection_state_with_subscriptions(self) -> None:
        """ConnectionState can include initial subscriptions."""
        actor = _make_actor()
        state = ConnectionState(
            connection_id="conn-1",
            actor=actor,
            subscriptions=frozenset({ChannelResourceType.VIDEO_FEED, ChannelResourceType.ALERTS}),
        )
        assert ChannelResourceType.VIDEO_FEED in state.subscriptions
        assert ChannelResourceType.ANALYTICS not in state.subscriptions

    def test_create_connection_state_helper(self) -> None:
        """create_connection_state helper creates a valid state."""
        actor = _make_actor()
        state = create_connection_state(actor)
        assert state.actor.actor_id == actor.actor_id
        assert len(state.connection_id) > 0
        assert state.connected_at.tzinfo is not None

    def test_create_connection_state_with_id(self) -> None:
        """create_connection_state accepts a custom connection_id."""
        actor = _make_actor()
        state = create_connection_state(actor, connection_id="custom-id")
        assert state.connection_id == "custom-id"


# =============================================================================
# authorize_channel_subscription Tests
# =============================================================================


# =============================================================================
# WebSocket Connection Authentication
# =============================================================================


class TestAuthenticateWebSocket:
    """Connection-level WebSocket authentication."""

    @staticmethod
    def _make_settings() -> Settings:
        return Settings(
            app_env="test",
            SECRET_KEY="test-secret-key-32-chars-long-ok!!!",
            JWT_ALGORITHM="HS256",
            JWT_EXPIRATION_MINUTES=60,
            _env_file=None,
        )

    def test_valid_token_accepted(self) -> None:
        """A valid JWT token produces an ActorContext."""
        settings = self._make_settings()
        token = _make_token(settings)
        ctx = authenticate_websocket_connection(token, settings)
        assert ctx is not None
        assert ctx.role_name == RoleName.OPERATOR
        assert ctx.active is True

    def test_invalid_token_rejected(self) -> None:
        """An invalid JWT token is rejected with AuthenticationError."""
        settings = self._make_settings()
        with pytest.raises(AuthenticationError):
            authenticate_websocket_connection("not-a-valid-token", settings)

    def test_empty_token_rejected(self) -> None:
        """An empty token is rejected."""
        settings = self._make_settings()
        with pytest.raises(AuthenticationError):
            authenticate_websocket_connection("", settings)

    def test_expired_token_rejected(self) -> None:
        """An expired JWT token is rejected with AuthenticationError."""
        from datetime import timedelta

        from backend.app.infrastructure.auth.service import create_access_token

        settings = self._make_settings()
        # Create a token that is already expired
        expired_token = create_access_token(
            str(uuid4()),
            settings,
            extra_claims={"exp": datetime.now(UTC) - timedelta(hours=1)},
        )
        with pytest.raises(AuthenticationError):
            authenticate_websocket_connection(expired_token, settings)


# =============================================================================
# Connection State Validation
# =============================================================================


class TestValidateConnectionState:
    """Long-lived connection state validation."""

    def test_active_actor_valid(self) -> None:
        """Active actor returns True from validate_connection_state."""
        actor = _make_actor()
        state = create_connection_state(actor, connection_id="test-conn")
        assert validate_connection_state(state) is True

    def test_inactive_actor_invalid(self) -> None:
        """Inactive (disabled) actor returns False."""
        tid = _uid()
        inactive_actor = ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(UUID(tid)),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            authenticated_at=datetime.now(UTC),
            active=False,  # Disabled
        )
        state = create_connection_state(inactive_actor, connection_id="test-conn")
        assert validate_connection_state(state) is False


class TestAuthorizeSubscription:
    """Core subscription authorization logic."""

    def test_valid_subscription_allowed(self) -> None:
        """Valid subscription request for authorized channel is allowed."""
        tid = _uid()
        actor = _make_actor(tenant_id=tid, role_name=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=actor.tenant_id,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is True
        assert response.channel == ChannelResourceType.VIDEO_FEED

    def test_analytics_subscription_allowed(self) -> None:
        """Operator can subscribe to analytics."""
        tid = _uid()
        actor = _make_actor(tenant_id=tid, role_name=RoleName.OPERATOR)
        request = SubscriptionRequest(
            channel=ChannelResourceType.ANALYTICS,
            tenant_id=actor.tenant_id,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is True

    def test_tenant_mismatch_denied(self) -> None:
        """Cross-tenant subscription is denied."""
        actor_tid = _uid()
        other_tid = _uid()
        actor = _make_actor(tenant_id=actor_tid, role_name=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.ALERTS,
            tenant_id=TenantId(UUID(other_tid)),  # Different tenant
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False
        assert "Tenant mismatch" in (response.reason or "")

    def test_unauthorized_venue_denied(self) -> None:
        """Subscription to an unauthorized venue is denied."""
        tid = _uid()
        allowed_vid = _uid()
        blocked_vid = _uid()
        actor = _make_actor(
            tenant_id=tid,
            role_name=RoleName.MANAGER,
            venue_ids=[allowed_vid, allowed_vid],  # Only first venue
        )
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=actor.tenant_id,
            venue_id=VenueId(UUID(blocked_vid)),  # Unauthorized venue
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False
        assert "venue" in (response.reason or "").lower()

    def test_all_venues_scope_grants_access(self) -> None:
        """Empty venue_scope (ALL_VENUES) grants access to any venue."""
        tid = _uid()
        any_venue = _uid()
        # Empty venue_scope = ALL_VENUES
        actor = _make_actor(tenant_id=tid, role_name=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=actor.tenant_id,
            venue_id=VenueId(UUID(any_venue)),
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is True

    def test_specific_venue_scope(self) -> None:
        """Access to specific venue is granted."""
        tid = _uid()
        allowed_vid = _uid()
        actor = _make_actor(
            tenant_id=tid,
            role_name=RoleName.OPERATOR,
            venue_ids=[allowed_vid],
        )
        request = SubscriptionRequest(
            channel=ChannelResourceType.ALERTS,
            tenant_id=actor.tenant_id,
            venue_id=VenueId(UUID(allowed_vid)),
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is True

    def test_missing_permission_denied(self) -> None:
        """Subscription to a channel without the required permission is denied."""
        # Create an actor with NO permissions for testing
        tid = _uid()
        no_perms = ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(UUID(tid)),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),  # Empty permissions
            authenticated_at=datetime.now(UTC),
            active=True,
        )
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=no_perms.tenant_id,
        )
        response = authorize_channel_subscription(no_perms, request)
        assert response.authorized is False
        assert "permission" in (response.reason or "").lower()

    def test_forged_tenant_different_from_actor_denied(self) -> None:
        """Client cannot forge a different tenant_id to bypass authorization."""
        real_tid = _uid()
        fake_tid = _uid()
        actor = _make_actor(tenant_id=real_tid, role_name=RoleName.ADMIN)
        # Client sends fake_tid in request
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=TenantId(UUID(fake_tid)),
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False
        # Actor's real tenant is not compromised
        assert str(actor.tenant_id) == real_tid

    def test_forged_venue_id_denied(self) -> None:
        """Client cannot forge a venue_id to access unauthorized venue."""
        tid = _uid()
        actor = _make_actor(
            tenant_id=tid,
            role_name=RoleName.MANAGER,
            venue_ids=[_uid()],  # Has access to one venue
        )
        first_venue = next(iter(actor.venue_scope))
        forged_venue = _uid()
        # Request with a different venue
        request = SubscriptionRequest(
            channel=ChannelResourceType.ALERTS,
            tenant_id=actor.tenant_id,
            venue_id=VenueId(UUID(forged_venue)),
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False
        # Forged venue is still not in actor's scope
        assert VenueId(UUID(forged_venue)) not in actor.venue_scope
        assert first_venue in actor.venue_scope

    def test_authorize_subscription_on_state(self) -> None:
        """authorize_subscription_on_state uses ConnectionState's actor."""
        tid = _uid()
        actor = _make_actor(tenant_id=tid, role_name=RoleName.ADMIN)
        state = create_connection_state(actor, connection_id="test-conn")
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=actor.tenant_id,
        )
        response = authorize_subscription_on_state(state, request)
        assert response.authorized is True


# =============================================================================
# is_channel_accessible Tests
# =============================================================================


# =============================================================================
# SubscriptionResponse Tests
# =============================================================================


class TestSubscriptionResponse:
    """SubscriptionResponse contract tests."""

    def test_authorized_response(self) -> None:
        """Authorized response can be created."""
        response = SubscriptionResponse(
            authorized=True,
            channel=ChannelResourceType.VIDEO_FEED,
        )
        assert response.authorized is True
        assert response.reason is None

    def test_denied_response_with_reason(self) -> None:
        """Denied response includes a reason."""
        response = SubscriptionResponse(
            authorized=False,
            channel=ChannelResourceType.ALERTS,
            reason="No access to alerts",
        )
        assert response.authorized is False
        assert response.reason == "No access to alerts"


class TestIsChannelAccessible:
    """Permission-based channel accessibility checks."""

    def test_admin_can_access_all_channels(self) -> None:
        """Admin has permission for all channels."""
        actor = _make_actor(role_name=RoleName.ADMIN)
        for channel in ChannelResourceType:
            assert is_channel_accessible(actor, channel) is True

    def test_operator_can_access_read_channels(self) -> None:
        """Operator can access read-only channels."""
        actor = _make_actor(role_name=RoleName.OPERATOR)
        assert is_channel_accessible(actor, ChannelResourceType.VIDEO_FEED) is True
        assert is_channel_accessible(actor, ChannelResourceType.ANALYTICS) is True
        assert is_channel_accessible(actor, ChannelResourceType.ALERTS) is True
        assert is_channel_accessible(actor, ChannelResourceType.EVIDENCE) is True
        assert is_channel_accessible(actor, ChannelResourceType.RECOMMENDATIONS) is True

    def test_actor_with_no_permissions_has_no_access(self) -> None:
        """An actor with empty permissions cannot access any channel."""
        tid = _uid()
        no_perms = ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(UUID(tid)),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),
            authenticated_at=datetime.now(UTC),
            active=True,
        )
        for channel in ChannelResourceType:
            assert is_channel_accessible(no_perms, channel) is False
