"""Outbox service (Task 7 Phase 4/5).

The transactional boundary for outbound events. ``enqueue_event``:

  BEGIN (caller's session)
    validate EventEnvelope (Task 4 contract — frozen, extra=forbid)
    validate tenant/venue scope against the ActorContext
    persist audit row (AuditEvent — trusted ActorContext identity)
    persist outbox row (validated envelope payload)
  COMMIT (caller's session)

Business state (written by the caller through the same session), the
domain event, the audit row, and the outbox row commit ATOMICALLY. If
the transaction rolls back, all four roll back. If it commits, the
outbox row is durable even if Redis is down — the outbox is the
durability boundary, and a publisher transports the row AFTER the
commit. Nothing is published to Redis before the database commit.

The outbox payload is the deterministic JSON serialization of the
canonical EventEnvelope (contracts/events/envelope.py); a separate
second envelope is never introduced.
"""

from __future__ import annotations

import uuid
from typing import Any

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.auth.scope import require_venue_access
from backend.app.infrastructure.database.models.audit_outbox_inbox import (
    AuditEventModel,
    OutboxEventModel,
)
from backend.app.infrastructure.database.repositories.outbox import OutboxRepository
from backend.app.infrastructure.observability.context import correlation_id
from contracts.audit import AuditEvent
from contracts.common import VenueId, utc_now
from contracts.events import EventEnvelope
from contracts.identity import ActorContext


def serialize_envelope(envelope: EventEnvelope[Any]) -> dict[str, Any]:
    """Deterministic JSON-safe serialization of the canonical envelope.

    The envelope is already validated by construction (frozen Pydantic
    model, extra=forbid, UTC timestamps, non-empty event_type/source).
    model_dump(mode="json") yields primitives only, so the dict is
    directly JSONB-serializable and round-trips through
    EventEnvelope.model_validate.
    """
    return envelope.model_dump(mode="json")


def validate_envelope(envelope: EventEnvelope[Any]) -> None:
    """Contract validation before the envelope may enter the outbox.

    The frozen Pydantic model validates event_id, event_type,
    schema_version, UTC event_time/produced_at, source, and payload on
    construction. This explicit re-validation documents the invariant
    and rejects structurally invalid envelopes (e.g. a dict that never
    went through the model) before any persistence happens.
    """
    # model_validate re-runs every field validator — invalid UUIDs,
    # naive timestamps, bad schema versions, and extra fields are all
    # rejected here.
    EventEnvelope[Any].model_validate(serialize_envelope(envelope))


def _inject_trace_context(envelope: EventEnvelope[Any]) -> EventEnvelope[Any]:
    """Attach the current trace context + correlation id to an envelope.

    Task 8.8: the envelope crosses three async boundaries (outbox →
    publisher → Redis → ingress → inbox → consumer), so the trace
    context active at production time is captured onto the envelope
    here. The TracerProvider is a safe no-op when tracing is disabled,
    so this is always safe to call and never fails.

    Missing telemetry context (no active span / tracing disabled)
    leaves the envelope unchanged — downstream workers start a fresh
    trace (requirement 10).
    """
    updates: dict[str, Any] = {}
    span = trace.get_current_span()
    if span.is_recording():
        sc = span.get_span_context()
        updates["trace_id"] = f"{sc.trace_id:032x}"
        updates["span_id"] = f"{sc.span_id:016x}"
        updates["trace_sampled"] = bool(sc.trace_flags.sampled)
    if envelope.correlation_id is None:
        cid = correlation_id()
        if cid:
            updates["correlation_id"] = cid
    if not updates:
        return envelope
    return envelope.model_copy(update=updates)


class OutboxService:
    """Writes validated events + audit rows into the caller's transaction."""

    async def enqueue_event(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        envelope: EventEnvelope[Any],
        audit: AuditEvent,
        venue_id: uuid.UUID | None = None,
    ) -> OutboxEventModel:
        """Persist audit + outbox for a validated event (no commit).

        The caller's session transaction owns the commit — the audit
        row, the outbox row, and the caller's own business writes are
        atomic.

        Args:
            session: The caller's transaction-scoped session.
            actor: The trusted server-side ActorContext (the envelope
                and audit tenant/venue identity derive from it — never
                from client payloads).
            envelope: The canonical EventEnvelope to publish.
            audit: A pre-built AuditEvent (AuditEventBuilder.from_actor)
                recording the same operation.
            venue_id: Optional venue scope of the event.

        Returns:
            The persisted outbox row.

        Raises:
            DuplicateEventError: If an outbox row with the same event_id
                already exists (idempotent enqueue — treat as a no-op).
        """
        # Task 8.8 — inject the current trace context into the envelope
        # so it survives the async boundary (outbox → publisher → Redis
        # → ingress → inbox → consumer). The envelope is frozen, so we
        # produce a copy with the telemetry fields attached.
        envelope = _inject_trace_context(envelope)

        validate_envelope(envelope)

        tenant_id = uuid.UUID(str(actor.tenant_id))
        if venue_id is not None:
            require_venue_access(actor, VenueId(venue_id))

        # Audit row — trusted ActorContext identity (no client-supplied
        # actor/tenant fields; the contract's secret-key validator
        # already ran when the AuditEvent was built).
        session.add(
            AuditEventModel(
                actor_id=uuid.UUID(str(audit.actor_id)),
                tenant_id=uuid.UUID(str(audit.tenant_id)),
                membership_id=(
                    uuid.UUID(str(audit.membership_id)) if audit.membership_id else None
                ),
                venue_id=uuid.UUID(str(audit.venue_id)) if audit.venue_id else None,
                action=audit.action,
                action_category=audit.action_category.value,
                correlation_id=audit.correlation_id,
                timestamp=audit.timestamp,
                metadata_=dict(audit.metadata) if audit.metadata else None,
            )
        )

        # Outbox row — the durable publication unit.
        return await OutboxRepository(session).enqueue(
            event_id=uuid.UUID(str(envelope.event_id)),
            tenant_id=tenant_id,
            venue_id=venue_id,
            event_type=envelope.event_type,
            payload=serialize_envelope(envelope),
            available_at=utc_now(),
        )


__all__ = [
    "OutboxService",
    "serialize_envelope",
    "validate_envelope",
]
