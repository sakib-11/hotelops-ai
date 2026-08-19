"""Evidence processing audit events (Task 17.11).

Wires every important evidence worker transition into the existing
transactional outbox: the audit row and the outbox row commit ATOMICALLY
with the evidence state change in the caller's session (OutboxService
contract). No duplicate audit infrastructure is introduced — this
mirrors ``media_audit.py`` for the evidence pipeline.

Event types (project naming convention ``<domain>.<action>``):
  evidence.processing.queued        — REQUESTED → QUEUED (durable enqueue)
  evidence.processing.started       — a worker claimed the ref (EXTRACTING)
  evidence.processing.uploaded      — artifact bytes persisted (UPLOADING)
  evidence.processing.finalized     — package persisted (FINALIZED)
  evidence.processing.retryable_failure — scheduled for bounded retry
  evidence.processing.terminal_failure  — dead-lettered (never retried)
  evidence.processing.recovered     — a crashed claim was reclaimed

Audit identity always comes from the trusted ActorContext; the worker
uses the reserved SYSTEM actor (audit rows store values, so the
synthetic identity is safe — see media_audit.system_actor).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.media_audit import system_actor
from backend.app.application.services.outbox import OutboxService
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from contracts.audit import AuditActionCategory
from contracts.common import EventId, VenueId, utc_now
from contracts.events import EventEnvelope

EVENT_QUEUED = "evidence.processing.queued"
EVENT_STARTED = "evidence.processing.started"
EVENT_UPLOADED = "evidence.processing.uploaded"
EVENT_FINALIZED = "evidence.processing.finalized"
EVENT_RETRYABLE_FAILURE = "evidence.processing.retryable_failure"
EVENT_TERMINAL_FAILURE = "evidence.processing.terminal_failure"
EVENT_RECOVERED = "evidence.processing.recovered"
EVENT_EXPIRED = "evidence.processing.expired"

# Bounded payload — identifiers and processing state only, never content
# or secrets.
_EVIDENCE_PAYLOAD_KEYS = (
    "ref_id",
    "tenant_id",
    "venue_id",
    "event_id",
    "state",
    "attempts",
    "extraction_id",
    "package_id",
    "media_path",
)


async def enqueue_evidence_audit_event(
    session: AsyncSession,
    *,
    ref: EvidenceRefModel,
    event_type: str,
    reason: str | None = None,
    correlation_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Persist an audit row + outbox row for an evidence transition.

    The caller's session owns the commit — the evidence state change,
    the audit row, and the outbox row are atomic (OutboxService
    contract). The worker acts as the reserved system actor for the
    ref's tenant.
    """
    now = utc_now()
    payload: dict[str, Any] = {
        "ref_id": str(ref.ref_id),
        "tenant_id": str(ref.tenant_id),
        "venue_id": str(ref.venue_id),
        "event_id": str(ref.event_id) if ref.event_id else None,
        "state": (ref.metadata_ or {}).get("processing_state"),
        "attempts": (ref.metadata_ or {}).get("processing_attempts"),
        "extraction_id": (ref.metadata_ or {}).get("extraction_id"),
        "package_id": (ref.metadata_ or {}).get("package_id"),
        "media_path": (ref.metadata_ or {}).get("artifact_object_key"),
    }
    if extra_payload:
        payload.update(extra_payload)

    envelope = EventEnvelope[dict[str, Any]](
        event_id=EventId(uuid.uuid4()),
        event_type=event_type,
        event_time=now,
        produced_at=now,
        source="hotelops.evidence",
        correlation_id=correlation_id,
        payload=payload,
    )

    metadata: dict[str, str] = {
        k: (str(payload[k]) if payload.get(k) is not None else "")
        for k in _EVIDENCE_PAYLOAD_KEYS
        if payload.get(k) is not None
    }
    if reason:
        metadata["reason"] = reason[:512]

    actor = system_actor(ref.tenant_id)
    audit = AuditEventBuilder.from_actor(
        actor=actor,
        action=event_type,
        action_category=AuditActionCategory.EVIDENCE,
        correlation_id=correlation_id,
        venue_id=VenueId(ref.venue_id),
        metadata=metadata,
    )

    await OutboxService().enqueue_event(
        session,
        actor=actor,
        envelope=envelope,
        audit=audit,
        venue_id=ref.venue_id,
    )


__all__ = [
    "EVENT_EXPIRED",
    "EVENT_FINALIZED",
    "EVENT_QUEUED",
    "EVENT_RECOVERED",
    "EVENT_RETRYABLE_FAILURE",
    "EVENT_STARTED",
    "EVENT_TERMINAL_FAILURE",
    "EVENT_UPLOADED",
    "enqueue_evidence_audit_event",
]
