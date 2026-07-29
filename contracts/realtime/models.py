"""Canonical realtime authorization models.

Defines the vocabulary for WebSocket connection and subscription
authorization. These are authorization contracts, not transport
implementation — they describe WHAT is being subscribed to and
WHETHER it is authorized.

SECURITY:
    Subscription selectors (resource_id, channel) come from the
    client. They are NOT authoritative — they must be validated
    against the server-built ActorContext.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext


class ChannelResourceType(StrEnum):
    """Types of realtime channels an actor may subscribe to.

    Kept small and stable for v1. Extend as new realtime domains are added.
    """

    VIDEO_FEED = "video.feed"
    ANALYTICS = "analytics"
    ALERTS = "alerts"
    EVIDENCE = "evidence"
    RECOMMENDATIONS = "recommendations"
    SYSTEM = "system"


class ConnectionState(BaseModel, frozen=True):
    """Server-side state for an authorized WebSocket connection.

    Constructed entirely server-side after authentication and
    authorization. Contains the ActorContext and connection metadata
    needed for subscription authorization.

    The actor is the canonical identity for all authorization
    decisions during the connection lifetime.
    """

    model_config = {"extra": "forbid"}

    connection_id: str = Field(min_length=1)
    actor: ActorContext
    connected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    subscriptions: frozenset[ChannelResourceType] = Field(default_factory=frozenset)


class SubscriptionRequest(BaseModel, frozen=True):
    """A subscription request from a connected client.

    The client selects a channel and scope. These are SELECTORS,
    not authorization — the server validates access against the
    ConnectionState's ActorContext.

    A client MUST NOT be able to subscribe to:
    - Another tenant's data
    - An unauthorized venue
    - An unauthorized resource type
    """

    model_config = {"extra": "forbid"}

    channel: ChannelResourceType
    tenant_id: TenantId
    venue_id: VenueId | None = None
    resource_id: str | None = None


class SubscriptionResponse(BaseModel, frozen=True):
    """Authorization response for a subscription request.

    Indicates whether the requested subscription is authorized
    based on the client's ActorContext.
    """

    model_config = {"extra": "forbid"}

    authorized: bool
    channel: ChannelResourceType
    reason: str | None = None
