"""Authoritative vertical-slice persistence boundary (Task 18.10).

The ONE business transaction that makes the slice's business state and
its events impossible to diverge. Every material fact+event pair is
persisted atomically:

    BEGIN
      1. canonical business fact  → temporal_facts
      2. domain event             → operational_events
      3. audit identity/context   → audit_events
      4. outbox message           → outbox_events (Task 7)
    COMMIT

The caller's session owns the commit — if ANY step fails, the whole
transaction rolls back and NONE of the four partially commit (the STOP
condition: business state and event can never become inconsistent).
Nothing is published to Redis before the database commit: the outbox
row is the durability boundary (Task 7 ADR-004 — Redis is transport,
not truth; PostgreSQL remains the source of truth).

Idempotency (Task 7): the outbox's unique ``event_id`` is the arbiter.
A duplicate delivery (the same material event re-persisted) returns a
``replayed`` result and writes NOTHING; under a concurrent race the
unique constraints reject the loser, whose savepoint is rolled back —
exactly one fact row, one event row, one audit row, and one outbox row
per logical event.

The outbox port is injectable (the production adapter is ``Task7Outbox``
wrapping ``OutboxService``); the fact/event row builders are the single
extension point for future canonical fact types.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.outbox import OutboxService
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.models.audit_outbox_inbox import OutboxEventModel
from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.database.models.temporal import TemporalFactModel
from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_PERSISTENCE,
    record_pipeline_metric,
)
from backend.app.infrastructure.reliability.exceptions import DuplicateEventError
from contracts.audit import AuditActionCategory, AuditEvent
from contracts.common import EventId
from contracts.events import EventEnvelope
from contracts.identity import ActorContext
from contracts.temporal import OccupancySnapshot

logger = logging.getLogger(__name__)

__all__ = [
    "FACT_TYPE_OCCUPANCY_SNAPSHOT",
    "EventOutboxPort",
    "OperationalPersistenceService",
    "PersistenceResult",
    "Task7Outbox",
]

# The controlled fact_type vocabulary entry for the occupancy slice fact.
FACT_TYPE_OCCUPANCY_SNAPSHOT = "occupancy_snapshot"

# Unique/PK constraint names that identify a DUPLICATE insert (replay) —
# as opposed to a genuine integrity failure (e.g. an FK violation) which
# must propagate. Mirrors the constraint-name inspection used by the
# outbox repository.
_DUPLICATE_CONSTRAINTS = frozenset({
    "uq_outbox_events_event_id",
    "pk_temporal_facts",
    "pk_operational_events",
})


def _is_duplicate(exc: IntegrityError) -> bool:
    return any(name in str(exc.orig) for name in _DUPLICATE_CONSTRAINTS)


class EventOutboxPort(Protocol):
    """The Task 7 outbox port the boundary writes through (injectable)."""

    async def find_by_event_id(
        self,
        session: AsyncSession,
        event_id: uuid.UUID | str,
    ) -> bool:
        """Whether an outbox row already exists for the event (dedup)."""

    async def enqueue_event(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        envelope: EventEnvelope[Any],
        audit: AuditEvent,
        venue_id: uuid.UUID | None = None,
    ) -> OutboxEventModel:
        """Persist audit + outbox rows (Task 7; the caller commits)."""


class Task7Outbox:
    """Production outbox adapter: ``OutboxService`` + repository lookup.

    ``find_by_event_id`` is the idempotency pre-check; ``enqueue_event``
    delegates to the Task 7 ``OutboxService`` (audit row + outbox row in
    the caller's transaction, unique event_id arbiter).
    """

    def __init__(self, service: OutboxService | None = None) -> None:
        self._service = service or OutboxService()

    async def find_by_event_id(
        self,
        session: AsyncSession,
        event_id: uuid.UUID | str,
    ) -> bool:
        stmt = select(OutboxEventModel.event_id).where(OutboxEventModel.event_id == event_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def enqueue_event(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        envelope: EventEnvelope[Any],
        audit: AuditEvent,
        venue_id: uuid.UUID | None = None,
    ) -> OutboxEventModel:
        return await self._service.enqueue_event(
            session,
            actor=actor,
            envelope=envelope,
            audit=audit,
            venue_id=venue_id,
        )


@dataclass(frozen=True)
class PersistenceResult:
    """The deterministic outcome of one authoritative persist.

    ``created=True``  — the fact/event/audit/outbox rows were written
                        into the caller's (uncommitted) transaction.
    ``replayed=True`` — the event was already persisted (duplicate
                        delivery / replay); NOTHING was written.
    """

    created: bool
    event_id: EventId
    fact_id: uuid.UUID | None = None
    outbox_id: uuid.UUID | None = None

    @property
    def replayed(self) -> bool:
        return not self.created


class OperationalPersistenceService:
    """The vertical-slice authoritative persistence boundary (Task 18.10)."""

    def __init__(self, outbox: EventOutboxPort | None = None) -> None:
        self._outbox = outbox or Task7Outbox()

    async def persist(
        self,
        session: AsyncSession,
        *,
        fact: OccupancySnapshot,
        event: EventEnvelope[Any],
        actor: ActorContext,
        correlation_id: str | None = None,
        processing_time: datetime | None = None,
        action: str = "operational.event.persisted",
        action_category: AuditActionCategory = AuditActionCategory.ANALYTICS,
    ) -> PersistenceResult:
        """Atomically persist fact + event + audit + outbox (no commit).

        Args:
            session: The caller's transaction-scoped session (the caller
                owns the commit — the four rows commit or roll back
                together).
            fact: The canonical Task 15 business fact (the material
                input that produced the event).
            event: The canonical Task 16 domain event (the envelope).
            actor: The trusted server-side ActorContext — the audit
                identity derives ONLY from it (Task 5 boundary).
            correlation_id: Optional request correlation for the audit
                and the envelope.
            processing_time: Optional processing metadata for the event
                row (never the event's event_time).
            action: The audit action label (default
                ``operational.event.persisted``).
            action_category: The audit action category.

        Returns:
            ``PersistenceResult`` — ``created`` (rows written, awaiting
            the caller's commit) or ``replayed`` (the event was already
            persisted; nothing was written).
        """
        # 0. Idempotency pre-check (Task 7 — the outbox unique event_id
        #    is the arbiter; the common duplicate path writes nothing).
        if await self._outbox.find_by_event_id(session, event.event_id):
            logger.info("operational persist replayed: event_id=%s", event.event_id)
            return PersistenceResult(created=False, event_id=event.event_id)

        # 1. canonical business fact
        fact_row = _fact_row(fact)
        # 2. domain event (scope from the fact — the engine already
        #    proved the event payload agrees with the fact scope)
        event_row = _event_row(fact, event, processing_time=processing_time)
        # 3. audit identity/context (trusted ActorContext only)
        audit = AuditEventBuilder.from_actor(
            actor=actor,
            action=action,
            action_category=action_category,
            correlation_id=correlation_id,
            venue_id=fact.key.venue_id,
            metadata={
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "fact_id": str(fact.snapshot_id),
                "fact_type": FACT_TYPE_OCCUPANCY_SNAPSHOT,
                "configuration_version_id": str(fact.key.configuration_version_id),
                "source": event.source,
            },
        )

        # The savepoint makes the duplicate RACE atomic: if another
        # transaction won with the same event, the unique constraints
        # reject this write and the savepoint discards the partial
        # rows — business state and event can never diverge.
        savepoint = await session.begin_nested()
        try:
            session.add(fact_row)
            session.add(event_row)
            # 4. outbox message (Task 7 — audit + outbox rows)
            outbox_row = await self._outbox.enqueue_event(
                session,
                actor=actor,
                envelope=event,
                audit=audit,
                venue_id=fact.key.venue_id,
            )
        except DuplicateEventError:
            await savepoint.rollback()
            logger.info("operational persist replayed: event_id=%s", event.event_id)
            return PersistenceResult(created=False, event_id=event.event_id)
        except IntegrityError as exc:
            if _is_duplicate(exc):
                await savepoint.rollback()
                logger.info("operational persist replayed: event_id=%s", event.event_id)
                return PersistenceResult(created=False, event_id=event.event_id)
            raise
        await savepoint.commit()
        # Task 18.18 — one authoritative persistence boundary commit.
        record_pipeline_metric(PIPELINE_METRIC_PERSISTENCE)

        logger.info(
            "operational persist: fact_id=%s event_id=%s outbox_id=%s",
            fact.snapshot_id,
            event.event_id,
            outbox_row.outbox_id,
        )
        return PersistenceResult(
            created=True,
            event_id=event.event_id,
            fact_id=uuid.UUID(str(fact.snapshot_id)),
            outbox_id=outbox_row.outbox_id,
        )


# =============================================================================
# Row builders — the single extension point for future canonical fact types
# =============================================================================


def _fact_row(fact: OccupancySnapshot) -> TemporalFactModel:
    """Map the canonical business fact to its durable temporal_facts row.

    The payload is the canonical fact contract serialized verbatim —
    the fact is never re-derived at persistence time.
    """
    key = fact.key
    return TemporalFactModel(
        fact_id=uuid.UUID(str(fact.snapshot_id)),
        fact_type=FACT_TYPE_OCCUPANCY_SNAPSHOT,
        fsm_kind=fact.fsm_kind,
        tenant_id=uuid.UUID(str(key.tenant_id)),
        venue_id=uuid.UUID(str(key.venue_id)),
        session_id=uuid.UUID(str(key.session_id)),
        camera_id=uuid.UUID(str(key.camera_id)),
        configuration_version_id=uuid.UUID(str(key.configuration_version_id)),
        event_time=fact.event_time,
        source_transition_id=uuid.UUID(str(fact.source_transition_id)),
        fsm_version=fact.fsm_version,
        policy_revision=fact.policy_revision,
        payload=fact.model_dump(mode="json"),
    )


def _event_row(
    fact: OccupancySnapshot,
    event: EventEnvelope[Any],
    *,
    processing_time: datetime | None,
) -> OperationalEventModel:
    """Map the canonical domain event to its durable operational_events row.

    Envelope metadata becomes typed columns (Task 6.6); the envelope
    payload (the generic PayloadT) is the JSONB payload. Tenant/venue/
    session/camera scope comes from the canonical FACT (the engine
    already proved the event payload agrees with the fact scope), so
    the event row can never disagree with the fact that produced it.
    """
    key = fact.key
    payload = event.payload
    if hasattr(payload, "model_dump"):
        payload_json = payload.model_dump(mode="json")
    else:
        payload_json = dict(payload)
    return OperationalEventModel(
        event_id=uuid.UUID(str(event.event_id)),
        event_type=event.event_type,
        schema_version=event.schema_version,
        tenant_id=uuid.UUID(str(key.tenant_id)),
        venue_id=uuid.UUID(str(key.venue_id)),
        session_id=uuid.UUID(str(key.session_id)),
        camera_id=uuid.UUID(str(key.camera_id)),
        event_time=event.event_time,
        produced_at=event.produced_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        source=event.source,
        payload=payload_json,
        processing_time=processing_time,
    )
