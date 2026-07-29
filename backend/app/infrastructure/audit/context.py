"""Audit context builder.

Builds authoritative AuditEvent records from trusted server-side
ActorContext. Never trusts audit identity from client-provided data.

SECURITY RULE:
    Audit identity comes from trusted ActorContext.
    Never trust actor_id from a request body as the authoritative audit actor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from contracts.audit import AuditActionCategory, AuditEvent
from contracts.common import MembershipId, VenueId
from contracts.identity import ActorContext


class AuditEventBuilder:
    """Builds AuditEvent records from trusted ActorContext.

    The builder ensures audit identity is always derived from
    server-side authorization state, never from client input.

    Usage:
        actor: ActorContext = ...  # from FastAPI dependency
        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="venue.created",
            action_category=AuditActionCategory.VENUE,
            correlation_id=request_id,
            venue_id=venue_id,
        )
    """

    @staticmethod
    def from_actor(
        actor: ActorContext,
        action: str,
        action_category: AuditActionCategory,
        correlation_id: str | None = None,
        venue_id: VenueId | None = None,
        membership_id: MembershipId | None = None,
        metadata: dict[str, str] | None = None,
    ) -> AuditEvent:
        """Build an AuditEvent from a trusted ActorContext.

        All identity fields (actor_id, tenant_id) are derived from
        the ActorContext, which is constructed server-side from
        verified authentication and authorization state.

        Args:
            actor: Trusted server-side ActorContext.
            action: Human-readable description of the action.
            action_category: Category of the action being audited.
            correlation_id: Optional request/correlation identifier.
            venue_id: Optional venue scope of the action.
            membership_id: Optional membership through which acted.
            metadata: Optional non-sensitive metadata about the action.

        Returns:
            AuditEvent with identity derived exclusively from ActorContext.

        Raises:
            ValueError: If metadata contains sensitive keys (passwords,
                tokens, secrets, keys, credentials).
        """
        return AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action=action,
            action_category=action_category,
            correlation_id=correlation_id,
            venue_id=venue_id,
            membership_id=membership_id,
            metadata=metadata,
            timestamp=datetime.now(UTC),
        )
