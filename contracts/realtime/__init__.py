"""Canonical realtime/subscription contracts for HotelOps AI.

Defines the authorization boundary for WebSocket connections
and channel subscriptions. Client-supplied subscription selectors
are NEVER trusted as authorization — they are validated against
the server-resolved ActorContext.
"""

from contracts.realtime.models import (
    ChannelResourceType,
    ConnectionState,
    SubscriptionRequest,
    SubscriptionResponse,
)

__all__ = [
    "ChannelResourceType",
    "ConnectionState",
    "SubscriptionRequest",
    "SubscriptionResponse",
]
