"""Configuration lifecycle audit events (Task 10.14).

Wires configuration lifecycle transitions into the existing transactional
outbox: the audit row and the outbox row commit ATOMICALLY with the
configuration state change in the caller's session (OutboxService
contract). No duplicate audit infrastructure is introduced.

Event types (project naming convention ``<domain>.<action>``):
  configuration.draft.created       — a new DRAFT version was created
  configuration.draft.updated       — a DRAFT version's entities changed
  configuration.validation.started  — DRAFT -> VALIDATING
  configuration.validation.completed — VALIDATING -> VALIDATED (or back to
                                       DRAFT on failure)
  configuration.published           — VALIDATED -> PUBLISHED (atomic)

Bounded payloads: identifiers + status only; never geometry internals,
video data, or personal data.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.outbox import OutboxService
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.models.configuration import ConfigurationVersionModel
from contracts.audit import AuditActionCategory
from contracts.common import EventId, TenantId, UserId, VenueId, utc_now
from contracts.events import EventEnvelope
from contracts.identity import ActorContext, Permission, RoleName

# Reserved system identity (worker-initiated events only).
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

EVENT_DRAFT_CREATED = "configuration.draft.created"
EVENT_DRAFT_UPDATED = "configuration.draft.updated"
EVENT_VALIDATION_STARTED = "configuration.validation.started"
EVENT_VALIDATION_COMPLETED = "configuration.validation.completed"
EVENT_PUBLISHED = "configuration.published"

# Bounded payload keys — identifiers and status only.
_PAYLOAD_KEYS = (
    "configuration_version_id",
    "configuration_id",
    "tenant_id",
    "venue_id",
    "version",
    "status",
)


def system_actor(tenant_id: uuid.UUID) -> ActorContext:
    """A synthetic system ActorContext for worker-initiated audit events."""
    return ActorContext(
        actor_id=UserId(SYSTEM_ACTOR_ID),
        tenant_id=TenantId(tenant_id),
        role_name=RoleName.ADMIN,
        permissions=frozenset(Permission),
        venue_scope=frozenset(),
        authenticated_at=utc_now(),
        active=True,
    )


async def enqueue_config_audit_event(
    session: AsyncSession,
    *,
    actor: ActorContext,
    event_type: str,
    version: ConfigurationVersionModel,
    reason: str | None = None,
    correlation_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
    action_category: AuditActionCategory = AuditActionCategory.VENUE,
) -> None:
    """Persist an audit row + outbox row for a configuration transition.

    The caller's session owns the commit — the configuration state
    change, the audit row, and the outbox row are atomic.
    """
    now = utc_now()
    payload: dict[str, Any] = {
        "configuration_version_id": str(version.configuration_version_id),
        "configuration_id": str(version.configuration_id),
        "tenant_id": str(version.tenant_id),
        "venue_id": str(version.venue_id),
        "version": version.version,
        "status": version.status,
    }
    if extra_payload:
        payload.update(extra_payload)

    envelope = EventEnvelope[dict[str, Any]](
        event_id=EventId(uuid.uuid4()),
        event_type=event_type,
        event_time=now,
        produced_at=now,
        source="hotelops.configuration",
        correlation_id=correlation_id,
        payload=payload,
    )

    metadata: dict[str, str] = {k: str(payload[k]) for k in _PAYLOAD_KEYS if k in payload}
    if reason:
        metadata["reason"] = reason[:512]

    audit = AuditEventBuilder.from_actor(
        actor=actor,
        action=event_type,
        action_category=action_category,
        correlation_id=correlation_id,
        venue_id=VenueId(version.venue_id),
        metadata=metadata,
    )

    await OutboxService().enqueue_event(
        session,
        actor=actor,
        envelope=envelope,
        audit=audit,
        venue_id=version.venue_id,
    )


__all__ = [
    "EVENT_DRAFT_CREATED",
    "EVENT_DRAFT_UPDATED",
    "EVENT_PUBLISHED",
    "EVENT_VALIDATION_COMPLETED",
    "EVENT_VALIDATION_STARTED",
    "SYSTEM_ACTOR_ID",
    "enqueue_config_audit_event",
    "system_actor",
]
