"""Transactional outbox publisher (Task 7 Phase 6/7/8/9).

Polls the durable outbox and transports pending events to the Redis
stream (ADR-004: Redis is transport, the outbox is the source of truth):

    PENDING ──claim──▶ PROCESSING ──publish──▶ PUBLISHED
                              │
                              └─fail─▶ failed (available_at = now + backoff)
                                       │
                                       └─attempts >= max / non-retryable──▶ DEAD_LETTER

Durability/crash safety:
  - CLAIM is a short transaction (FOR UPDATE SKIP LOCKED) that commits
    BEFORE the external publish — no DB transaction is held open during
    network I/O.
  - If the publisher crashes after claiming, the lease expires and the
    row is reclaimable (crash recovery).
  - If the publisher crashes AFTER publishing but before marking
    published, the event is re-published — at-least-once delivery. The
    inbox deduplication key (source, event_id) prevents duplicate
    business effects downstream.
  - Redis being unavailable only schedules a retry; the outbox row is
    already durable.

Retry policy: bounded exponential backoff + jitter persisted as the
row's available_at (migration 016). Permanently failing events move to
DEAD_LETTER and are NEVER deleted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import Any

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.database.models.audit_outbox_inbox import OutboxEventModel
from backend.app.infrastructure.database.repositories.outbox import OutboxRepository
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.reliability import (
    NonRetryableError,
    compute_backoff_delay,
)
from backend.app.infrastructure.transport import RedisStreamTransport
from backend.app.workers.base import PollingWorker
from contracts.common import utc_now

logger = logging.getLogger(__name__)


class OutboxPublisherWorker(PollingWorker):
    """Polls and publishes outbox events to the Redis stream."""

    def __init__(
        self,
        database: DatabaseClient,
        transport: RedisStreamTransport,
        settings: Settings,
        *,
        worker_id: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(
            poll_interval=settings.outbox_poll_interval,
            worker_id=worker_id or f"outbox:{uuid.uuid4().hex[:8]}",
        )
        self._database = database
        self._transport = transport
        self._settings = settings
        self._rng = rng or random.Random()
        self._batch_size = 20
        self._max_error_length = 2000

    async def run_once(self) -> int:
        """One claim → publish → transition cycle.

        Returns:
            The number of events successfully published.
        """
        now = utc_now()
        async with self._database.session() as session:
            rows = await OutboxRepository(session).claim_next(
                worker_id=self.worker_id,
                batch_size=self._batch_size,
                lease_seconds=self._settings.outbox_lease_seconds,
                now=now,
            )

        published = 0
        for row in rows:
            if self._stop_event.is_set():
                # Graceful shutdown — leave the rest for the next cycle
                # (the lease will expire and the rows get re-claimed).
                break
            try:
                await self._publish(row)
            except NonRetryableError as exc:
                await self._dead_letter(row, exc)
                continue
            except Exception as exc:  # retryable (transport, serialization, ...)
                await self._schedule_retry(row, exc)
                continue
            if await self._mark_published(row):
                published += 1
        return published

    # =========================================================================
    # Publish + transitions
    # =========================================================================

    async def _publish(self, row: OutboxEventModel) -> None:
        """Publish the envelope JSON to the Redis stream (external I/O).

        Task 8.8: the trace context captured at enqueue time is
        extracted from the envelope payload and used as the parent of
        this span, so the original trace continues through the worker.
        """
        envelope_json = json.dumps(row.payload, separators=(",", ":"), ensure_ascii=False)
        # Reconstruct the parent trace context from the envelope's
        # telemetry fields (captured at enqueue time).
        parent_trace = tracing.trace_context_from_event_attrs(
            trace_id=row.payload.get("trace_id"),
            span_id=row.payload.get("span_id"),
            trace_sampled=row.payload.get("trace_sampled"),
        )
        correlation_id = self._correlation_id(row.payload)
        # Event-scoped span carrying bounded identifiers only — the
        # envelope body is never an attribute (Task 8.7). When no
        # parent trace is available (missing telemetry context), a
        # fresh trace is started (requirement 10).
        async with tracing.event_span(
            "outbox.publish",
            event_id=str(row.event_id),
            event_type=row.event_type,
            tenant_id=str(row.tenant_id),
            venue_id=str(row.venue_id) if row.venue_id else None,
            parent_trace=parent_trace,
        ) as _:
            await self._transport.publish(
                self._settings.redis_stream_events,
                event_id=str(row.event_id),
                event_type=row.event_type,
                envelope_json=envelope_json,
                tenant_id=str(row.tenant_id),
                venue_id=str(row.venue_id) if row.venue_id else None,
                schema_version=row.schema_version,
                correlation_id=correlation_id,
            )

    async def _mark_published(self, row: OutboxEventModel) -> bool:
        async with self._database.session() as session:
            ok = await OutboxRepository(session).mark_published(row.outbox_id, self.worker_id)
        if not ok:
            # The lease was lost (another worker reclaimed after expiry)
            # — never override the new owner's state.
            logger.warning(
                "outbox claim lost before publish-marking; skipping",
                extra={"event_id": str(row.event_id), "outbox_id": str(row.outbox_id)},
            )
        return ok

    async def _schedule_retry(self, row: OutboxEventModel, error: Exception) -> None:
        """Persist failed with bounded exponential backoff + jitter."""
        attempts = row.attempts
        if attempts >= self._settings.outbox_max_attempts:
            await self._dead_letter(row, error)
            return
        delay = compute_backoff_delay(
            attempts,
            base_seconds=self._settings.outbox_backoff_base,
            max_seconds=self._settings.outbox_backoff_max,
            jitter=self._settings.outbox_backoff_jitter,
            rng=self._rng,
        )
        retry_at = utc_now() + delay
        async with self._database.session() as session:
            ok = await OutboxRepository(session).mark_failed(
                row.outbox_id,
                self.worker_id,
                error=str(error)[: self._max_error_length],
                retry_at=retry_at,
            )
        if ok:
            logger.warning(
                "outbox publish failed; retry scheduled",
                extra={
                    "event_id": str(row.event_id),
                    "event_type": row.event_type,
                    "attempts": attempts,
                    "retry_in_seconds": delay.total_seconds(),
                    "error_category": type(error).__name__,
                },
            )

    async def _dead_letter(self, row: OutboxEventModel, error: Exception) -> None:
        """Move the event to DEAD_LETTER (terminal — never deleted)."""
        async with self._database.session() as session:
            ok = await OutboxRepository(session).mark_dead_letter(
                row.outbox_id,
                self.worker_id,
                error=str(error)[: self._max_error_length],
            )
        if ok:
            logger.error(
                "outbox event dead-lettered",
                extra={
                    "event_id": str(row.event_id),
                    "event_type": row.event_type,
                    "tenant_id": str(row.tenant_id),
                    "venue_id": str(row.venue_id) if row.venue_id else None,
                    "attempts": row.attempts,
                    "error_category": type(error).__name__,
                },
            )

    @staticmethod
    def _correlation_id(payload: dict[str, Any]) -> str | None:
        """The envelope's correlation_id, when present (never secrets)."""
        correlation_id = payload.get("correlation_id")
        return correlation_id if isinstance(correlation_id, str) else None


async def _main() -> None:
    from backend.app.infrastructure.logging import configure_logging
    from backend.app.infrastructure.observability import tracing
    from backend.app.infrastructure.redis.client import RedisClient

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings=settings)
    tracing.configure_tracing(settings)
    database = DatabaseClient(settings)
    await database.initialize()
    redis = RedisClient(settings)
    await redis.initialize()
    transport = RedisStreamTransport(redis)
    try:
        worker = OutboxPublisherWorker(database, transport, settings)
        await worker.run_forever()
    finally:
        await redis.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
