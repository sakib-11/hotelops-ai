"""Media lifecycle audit events (Task 9.15).

Wires every important media lifecycle transition into the existing
transactional outbox: the audit row and the outbox row commit ATOMICALLY
with the media state change in the caller's session (OutboxService
contract). No duplicate audit infrastructure is introduced.

Event types (project naming convention ``<domain>.<action>``):
  media.upload.completed      — bytes verified present in object storage
  media.upload.aborted        — client/operator aborted an in-flight upload
  media.validation.failed     — content or checksum verification failed
  media.available             — media promoted to AVAILABLE
  media.access.requested      — a signed download URL was issued
  media.deletion.requested    — deletion initiated (two-phase start)
  media.deleted               — object deleted, record terminal
  media.cleanup.failed        — a cleanup worker operation failed

Audit identity always comes from the trusted ActorContext; the cleanup
worker uses a reserved SYSTEM actor (audit rows store values, so the
synthetic identity is safe).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.outbox import OutboxService
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.models.media import MediaAssetModel
from contracts.audit import AuditActionCategory
from contracts.common import EventId, TenantId, UserId, VenueId, utc_now
from contracts.events import EventEnvelope
from contracts.identity import ActorContext, Permission, RoleName

# Reserved system identity for worker-initiated events (no human actor).
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

EVENT_UPLOAD_INITIATED = "media.upload.initiated"
EVENT_UPLOAD_COMPLETED = "media.upload.completed"
EVENT_UPLOAD_ABORTED = "media.upload.aborted"
EVENT_VALIDATION_FAILED = "media.validation.failed"
EVENT_AVAILABLE = "media.available"
EVENT_ACCESS_REQUESTED = "media.access.requested"
EVENT_DELETION_REQUESTED = "media.deletion.requested"
EVENT_DELETED = "media.deleted"
EVENT_CLEANUP_FAILED = "media.cleanup.failed"

# Bounded payload — identifiers and state only, never content or secrets.
_MEDIA_PAYLOAD_KEYS = ("media_id", "tenant_id", "venue_id", "category", "object_key", "state")


def system_actor(tenant_id: uuid.UUID) -> ActorContext:
    """A synthetic system ActorContext for worker-initiated audit events.

    ``venue_scope`` is empty (tenant-wide) so ``require_venue_access``
    in the outbox service permits worker-scoped writes; the audit row
    still records the exact tenant/venue of the affected record.
    """
    return ActorContext(
        actor_id=UserId(SYSTEM_ACTOR_ID),
        tenant_id=TenantId(tenant_id),
        role_name=RoleName.ADMIN,
        permissions=frozenset(Permission),
        venue_scope=frozenset(),
        authenticated_at=utc_now(),
        active=True,
    )


def _category_for_media(media: MediaAssetModel) -> AuditActionCategory:
    if media.category == "recordings":
        return AuditActionCategory.VIDEO
    if media.category == "evidence":
        return AuditActionCategory.EVIDENCE
    if media.category == "reports":
        return AuditActionCategory.RECOMMENDATION
    if media.category == "analytics":
        return AuditActionCategory.ANALYTICS
    return AuditActionCategory.SYSTEM


async def enqueue_media_audit_event(
    session: AsyncSession,
    *,
    actor: ActorContext,
    event_type: str,
    media: MediaAssetModel,
    reason: str | None = None,
    correlation_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Persist an audit row + outbox row for a media lifecycle transition.

    The caller's session owns the commit — the media state change, the
    audit row, and the outbox row are atomic (OutboxService contract).
    """
    now = utc_now()
    payload: dict[str, Any] = {
        "media_id": str(media.media_id),
        "tenant_id": str(media.tenant_id),
        "venue_id": str(media.venue_id),
        "category": media.category,
        "object_key": media.object_key,
        "state": media.lifecycle_state,
    }
    if extra_payload:
        payload.update(extra_payload)

    envelope = EventEnvelope[dict[str, Any]](
        event_id=EventId(uuid.uuid4()),
        event_type=event_type,
        event_time=now,
        produced_at=now,
        source="hotelops.media",
        correlation_id=correlation_id,
        payload=payload,
    )

    metadata: dict[str, str] = {k: str(payload[k]) for k in _MEDIA_PAYLOAD_KEYS if k in payload}
    if reason:
        metadata["reason"] = reason[:512]

    audit = AuditEventBuilder.from_actor(
        actor=actor,
        action=event_type,
        action_category=_category_for_media(media),
        correlation_id=correlation_id,
        venue_id=VenueId(media.venue_id),
        metadata=metadata,
    )

    await OutboxService().enqueue_event(
        session,
        actor=actor,
        envelope=envelope,
        audit=audit,
        venue_id=media.venue_id,
    )


__all__ = [
    "EVENT_ACCESS_REQUESTED",
    "EVENT_AVAILABLE",
    "EVENT_CLEANUP_FAILED",
    "EVENT_DELETED",
    "EVENT_DELETION_REQUESTED",
    "EVENT_UPLOAD_ABORTED",
    "EVENT_UPLOAD_COMPLETED",
    "EVENT_UPLOAD_INITIATED",
    "EVENT_VALIDATION_FAILED",
    "SYSTEM_ACTOR_ID",
    "enqueue_media_audit_event",
    "system_actor",
]
