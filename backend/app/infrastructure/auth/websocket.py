"""WebSocket authorization boundary for HotelOps AI.

WebSocket connections do NOT bypass normal security.

FLOW:
    Connection (with JWT token in query param)
        ↓
    authenticate_websocket_connection()
        ↓ Verification
    Authenticate (verify JWT signature, expiry) → AuthenticationError
        ↓
    Authorize (resolve ActorContext via ActorContextBuilder)
        ↓
    For each subscription request:
        authorize_channel_subscription(actor, request)
            ↓ Validate tenant scope
            ↓ Validate venue scope
            ↓ Validate resource permission
            Accept or reject

LONG-LIVED CONNECTIONS:
    - Authentication is validated at connect time via JWT
    - Connection state is cached in ConnectionState
    - validate_connection_state() checks actor.active synchronously
    - For complete runtime revocation (membership changes, role changes,
      venue permission changes, disabled users), the application should
      re-resolve the actor's membership state at appropriate intervals
      or close the connection when notified via an event mechanism.
    - The ConnectionState.actor.active flag provides a first line of
      defense: any code path that sets active=False will cause
      validate_connection_state() to return False.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from backend.app.infrastructure.auth.context import ActorContextBuilder
from backend.app.infrastructure.auth.service import AuthService
from backend.app.infrastructure.config import Settings
from contracts.identity import ActorContext, Permission
from contracts.realtime import (
    ChannelResourceType,
    ConnectionState,
    SubscriptionRequest,
    SubscriptionResponse,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Permission map: channel type → required permission
# =============================================================================

_CHANNEL_PERMISSIONS: dict[ChannelResourceType, Permission] = {
    ChannelResourceType.VIDEO_FEED: Permission.VIDEO_READ,
    ChannelResourceType.ANALYTICS: Permission.ANALYTICS_READ,
    ChannelResourceType.ALERTS: Permission.ALERT_READ,
    ChannelResourceType.EVIDENCE: Permission.EVIDENCE_READ,
    ChannelResourceType.RECOMMENDATIONS: Permission.RECOMMENDATION_READ,
    ChannelResourceType.SYSTEM: Permission.ANALYTICS_READ,
}


def _required_permission(channel: ChannelResourceType) -> Permission | None:
    """Return the Permission required to subscribe to a channel."""
    return _CHANNEL_PERMISSIONS.get(channel)


# =============================================================================
# Connection Authentication & Authorization
# =============================================================================


def authenticate_websocket_connection(
    token: str,
    settings: Settings,
) -> ActorContext:
    """Authenticate a WebSocket connection using a JWT token.

    This is the connection-level authentication boundary for WebSockets.
    The token is typically extracted from the WebSocket query parameters
    (since WebSocket headers are limited).

    Steps:
    1. Verify JWT signature, expiry, issuer via AuthService
    2. Build authoritative ActorContext via ActorContextBuilder
    3. Return ActorContext for subscription authorization

    Args:
        token: JWT token string (from WebSocket query param).
        settings: Application settings with JWT configuration.

    Returns:
        ActorContext with server-resolved authorization state.

    Raises:
        AuthenticationError (→ 401) if:
        - Token is missing, invalid, expired, or tampered
        - User is unknown or disabled
        - Tenant is unknown or disabled
        - Membership is inactive or missing

    Usage:
        actor = authenticate_websocket_connection(token, settings)
        state = create_connection_state(actor)
    """
    service = AuthService(settings)
    token_data = service.verify(token)
    builder = ActorContextBuilder()
    return builder.build(token_data)


def authorize_channel_subscription(
    actor: ActorContext,
    request: SubscriptionRequest,
) -> SubscriptionResponse:
    """Authorize a client's subscription request against their ActorContext.

    The client's channel, tenant_id, and venue_id are SELECTORS —
    they are validated against the server-built ActorContext.

    A client MUST NOT be able to subscribe to:
    - Another tenant's data (tenant_id in request must match actor.tenant_id)
    - An unauthorized venue (venue_id must be in actor's venue scope)
    - An unauthorized resource type (actor must have the required permission)

    Args:
        actor: The authenticated actor's server-built context.
        request: The client's subscription request.

    Returns:
        SubscriptionResponse with authorized=True/False and reason.
    """
    # 1. Validate tenant scope
    if request.tenant_id != actor.tenant_id:
        return SubscriptionResponse(
            authorized=False,
            channel=request.channel,
            reason=f"Tenant mismatch: request={request.tenant_id}, actor={actor.tenant_id}",
        )

    # 2. Validate venue scope (if specified)
    # Empty venue_scope = ALL_VENUES (tenant-wide access)
    if (
        request.venue_id is not None
        and actor.venue_scope
        and request.venue_id not in actor.venue_scope
    ):
        return SubscriptionResponse(
            authorized=False,
            channel=request.channel,
            reason=f"No access to venue: {request.venue_id}",
        )

    # 3. Validate resource permission
    required = _required_permission(request.channel)
    if required is not None and not actor.has_permission(required):
        return SubscriptionResponse(
            authorized=False,
            channel=request.channel,
            reason=f"Missing required permission: {required.value}",
        )

    return SubscriptionResponse(
        authorized=True,
        channel=request.channel,
    )


# =============================================================================
# Connection State & Validation
# =============================================================================


def validate_connection_state(state: ConnectionState) -> bool:
    """Check whether a connection state is still valid.

    For long-lived WebSocket connections, the actor's state may change
    after the initial connection (user disabled, membership revoked).
    This function provides a lightweight synchronous validity check.

    For full runtime revocation:
    - Check this function at appropriate intervals or before each
      subscription operation.
    - To detect membership/role/venue changes, the application should
      re-resolve the actor's state at runtime (e.g., via a periodic
      check or an invalidation event).
    - Close the connection when this returns False.

    Args:
        state: The connection state to validate.

    Returns:
        True if the actor is still active and the connection is valid.
        False if the actor has been disabled.
    """
    return state.actor.active


def create_connection_state(
    actor: ActorContext,
    connection_id: str | None = None,
) -> ConnectionState:
    """Create a connection state for an authorized WebSocket connection.

    Args:
        actor: The authenticated actor's resolved context.
        connection_id: Optional connection ID (auto-generated if not provided).

    Returns:
        ConnectionState with the actor and connection metadata.
    """
    return ConnectionState(
        connection_id=connection_id or str(uuid4()),
        actor=actor,
        connected_at=datetime.now(UTC),
    )


def authorize_subscription_on_state(
    state: ConnectionState,
    request: SubscriptionRequest,
) -> SubscriptionResponse:
    """Authorize a subscription against an existing connection state.

    Convenience wrapper that extracts the ActorContext from the
    ConnectionState and delegates to authorize_channel_subscription.

    Args:
        state: The authorized connection state.
        request: The client's subscription request.

    Returns:
        SubscriptionResponse with authorized=True/False and reason.
    """
    return authorize_channel_subscription(state.actor, request)


def is_channel_accessible(actor: ActorContext, channel: ChannelResourceType) -> bool:
    """Check if an actor has the minimum permission to access a channel.

    Useful for pre-filtering available channels before a client subscribes.

    Args:
        actor: The authenticated actor's context.
        channel: The channel to check.

    Returns:
        True if the actor has the required permission for the channel.
    """
    required = _required_permission(channel)
    if required is None:
        return False
    return actor.has_permission(required)
