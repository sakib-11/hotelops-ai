"""Integration tests for the Task 7 outbox publisher (Phases 4-9).

Runs the real publisher + repositories + migration 016 schema against a
scratch TimescaleDB and real Redis:

  - atomic enqueue (audit + outbox commit/roll back together)
  - publish success + Redis delivery
  - retry with persisted bounded exponential backoff
  - dead-letter after the retry budget is exhausted (row preserved)
  - non-retryable failure dead-letters immediately
  - leasing: one active claim; expired leases are reclaimable
  - concurrency: N workers claim disjoint rows (single active lease)
  - Redis unavailable: the outbox row remains retryable, never lost
  - crash-after-publish: duplicate delivery, deduped downstream
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.database.repositories.outbox import OutboxRepository
from backend.app.infrastructure.reliability import NonRetryableError, PublishError
from backend.app.workers.outbox_publisher import OutboxPublisherWorker
from contracts.audit import AuditActionCategory
from contracts.common import utc_now
from tests.integration._task7_helpers import (
    make_actor,
    make_database_client,
    make_envelope,
    query_engine,
    scalar,
    scratch_settings,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not __import__("os").environ.get("INTEGRATION_TESTS"),
        reason="Set INTEGRATION_TESTS=1 and start PostgreSQL/Redis",
    ),
]


class FlakyTransport:
    """Wraps the real Redis transport; publish failures are toggleable."""

    def __init__(self, real, *, fail: bool = False, non_retryable: bool = False) -> None:
        self._real = real
        self.fail = fail
        self.non_retryable = non_retryable
        self.published: list[dict] = []

    async def publish(self, stream: str, **kwargs) -> str:
        if self.fail:
            if self.non_retryable:
                raise NonRetryableError("contract-invalid payload")
            raise PublishError("Redis is unavailable")
        message_id = await self._real.publish(stream, **kwargs)
        self.published.append(kwargs)
        return message_id


# =============================================================================
# Helpers
# =============================================================================


async def _enqueue(client: DatabaseClient, actor, envelope, venue_id=None):
    """Enqueue via the real OutboxService (audit + outbox atomic)."""
    from backend.app.application.services.outbox import OutboxService

    audit = AuditEventBuilder.from_actor(
        actor=actor,
        action="event.enqueue",
        action_category=AuditActionCategory.SYSTEM,
        venue_id=venue_id,
    )
    async with client.session() as session:
        row = await OutboxService().enqueue_event(
            session,
            actor=actor,
            envelope=envelope,
            audit=audit,
            venue_id=venue_id,
        )
        return row.outbox_id, row.event_id


async def _advance_available_at(url: str, outbox_id: uuid.UUID, to_past: bool = True) -> None:
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            if to_past:
                await conn.execute(
                    text(
                        "UPDATE outbox_events SET available_at = now() - interval '1 second' "
                        "WHERE outbox_id = :oid"
                    ),
                    {"oid": outbox_id},
                )
            else:
                await conn.execute(
                    text(
                        "UPDATE outbox_events SET available_at = now() + interval '1 hour' "
                        "WHERE outbox_id = :oid"
                    ),
                    {"oid": outbox_id},
                )
    finally:
        await engine.dispose()


async def _expire_lease(url: str, outbox_id: uuid.UUID) -> None:
    """Rewind the lease into the past so the row becomes reclaimable.

    Uses GREATEST(created_at, ...) so the simulated expiry can never
    violate ck_outbox_events_lease_not_before_created when the row was
    created less than a second ago.
    """
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE outbox_events "
                    "SET claimed_until = GREATEST(created_at, now() - interval '1 second') "
                    "WHERE outbox_id = :oid"
                ),
                {"oid": outbox_id},
            )
    finally:
        await engine.dispose()


def _worker_settings(db_name: str, stream: str, **overrides):
    return scratch_settings(
        db_name,
        OUTBOX_BACKOFF_JITTER=0.0,  # deterministic backoff for tests
        OUTBOX_BACKOFF_BASE=1.0,
        OUTBOX_BACKOFF_MAX=60.0,
        OUTBOX_MAX_ATTEMPTS=3,
        OUTBOX_LEASE_SECONDS=60,
        REDIS_STREAM_EVENTS=stream,
        **overrides,
    )


# =============================================================================
# Atomicity of the enqueue boundary (Phase 4 / 13)
# =============================================================================


class TestEnqueueAtomicity:
    async def test_enqueue_commits_audit_and_outbox_together(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope()
            outbox_id, _ = await _enqueue(client, actor, envelope)
            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
                )
                == "pending"
            )
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT count(*) FROM audit_events WHERE action = 'event.enqueue'",
                )
                == 1
            )
        finally:
            await client.dispose()

    async def test_rollback_discards_audit_and_outbox(self, task7_db) -> None:
        """Crash before commit → no business change, no outbox, no audit."""
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            from backend.app.application.services.outbox import OutboxService

            actor = make_actor(tenant_id=uuid.uuid4())
            audit = AuditEventBuilder.from_actor(
                actor=actor,
                action="event.enqueue",
                action_category=AuditActionCategory.SYSTEM,
            )
            with pytest.raises(RuntimeError, match="boom"):
                async with client.session() as session:
                    await OutboxService().enqueue_event(
                        session,
                        actor=actor,
                        envelope=make_envelope(),
                        audit=audit,
                    )
                    raise RuntimeError("boom")
            assert await scalar(task7_db["url"], "SELECT count(*) FROM outbox_events") == 0, (
                "rollback must remove the outbox row"
            )
            assert await scalar(task7_db["url"], "SELECT count(*) FROM audit_events") == 0, (
                "rollback must remove the audit row"
            )
        finally:
            await client.dispose()

    async def test_duplicate_event_id_enqueue_is_idempotent(self, task7_db) -> None:
        from backend.app.application.services.outbox import OutboxService
        from backend.app.infrastructure.reliability import DuplicateEventError

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope()
            audit = AuditEventBuilder.from_actor(
                actor=actor,
                action="event.enqueue",
                action_category=AuditActionCategory.SYSTEM,
            )
            async with client.session() as session:
                await OutboxService().enqueue_event(
                    session, actor=actor, envelope=envelope, audit=audit
                )
            with pytest.raises(DuplicateEventError):
                async with client.session() as session:
                    await OutboxService().enqueue_event(
                        session, actor=actor, envelope=envelope, audit=audit
                    )
            assert await scalar(task7_db["url"], "SELECT count(*) FROM outbox_events") == 1
        finally:
            await client.dispose()


# =============================================================================
# Publish lifecycle
# =============================================================================


class TestPublishLifecycle:
    async def test_publish_success_delivers_to_redis(self, task7_db, task7_redis) -> None:
        stream = f"hotelops:t7:pub:{uuid.uuid4().hex[:8]}"
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope()
            outbox_id, event_id = await _enqueue(client, actor, envelope)

            settings = _worker_settings(task7_db["name"], stream)
            transport = FlakyTransport(task7_redis["transport"])
            worker = OutboxPublisherWorker(client, transport, settings, worker_id="pub-1")
            assert await worker.run_once() == 1

            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
                )
                == "published"
            )
            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT published_at IS NOT NULL FROM outbox_events "
                    f"WHERE outbox_id = '{outbox_id}'::uuid",
                )
                is True
            )
            assert len(transport.published) == 1
            assert transport.published[0]["event_id"] == str(event_id)
            assert await task7_redis["transport"].stream_length(stream) == 1
        finally:
            await client.dispose()

    async def test_retry_schedules_persisted_backoff(self, task7_db, task7_redis) -> None:
        """Failure → failed with available_at ≈ now + backoff(1) and last_error."""
        stream = f"hotelops:t7:retry:{uuid.uuid4().hex[:8]}"
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            outbox_id, _ = await _enqueue(client, actor, make_envelope())

            settings = _worker_settings(task7_db["name"], stream)
            transport = FlakyTransport(task7_redis["transport"], fail=True)
            worker = OutboxPublisherWorker(client, transport, settings, worker_id="pub-2")
            assert await worker.run_once() == 0

            row = await scalar(
                task7_db["url"],
                f"SELECT status || '|' || attempts || '|' || COALESCE(last_error, '') "
                f"FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            assert str(row).startswith("failed|1|")
            assert "Redis is unavailable" in str(row)

            # available_at ≈ now + 1s (base backoff, jitter=0)
            available_at = await scalar(
                task7_db["url"],
                f"SELECT available_at FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            assert available_at is not None
            delta = (available_at - datetime.now(UTC)).total_seconds()
            assert 0.8 <= delta <= 1.5, f"expected ~1s backoff, got {delta}s"

            # Redis recovers → the row publishes on the retry cycle
            transport.fail = False
            await _advance_available_at(task7_db["url"], outbox_id)
            assert await worker.run_once() == 1
            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
                )
                == "published"
            )
        finally:
            await client.dispose()

    async def test_dead_letter_after_max_attempts(self, task7_db, task7_redis) -> None:
        """Retry budget exhausted → dead_letter; the row is preserved."""
        stream = f"hotelops:t7:dl:{uuid.uuid4().hex[:8]}"
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            outbox_id, event_id = await _enqueue(client, actor, make_envelope())

            settings = _worker_settings(task7_db["name"], stream)  # max_attempts=3
            transport = FlakyTransport(task7_redis["transport"], fail=True)
            worker = OutboxPublisherWorker(client, transport, settings, worker_id="pub-3")

            assert await worker.run_once() == 0  # attempt 1 → failed
            await _advance_available_at(task7_db["url"], outbox_id)
            assert await worker.run_once() == 0  # attempt 2 → failed
            await _advance_available_at(task7_db["url"], outbox_id)
            assert await worker.run_once() == 0  # attempt 3 → DEAD_LETTER

            status, attempts, last_error, payload = await _fetch_outbox_row(
                task7_db["url"], outbox_id
            )
            assert status == "dead_letter"
            assert attempts == 3
            assert "Redis is unavailable" in last_error
            assert payload["event_id"] == str(event_id), "payload must be preserved"
        finally:
            await client.dispose()

    async def test_non_retryable_error_dead_letters_immediately(
        self, task7_db, task7_redis
    ) -> None:
        stream = f"hotelops:t7:nr:{uuid.uuid4().hex[:8]}"
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            outbox_id, _ = await _enqueue(client, actor, make_envelope())

            settings = _worker_settings(task7_db["name"], stream)
            transport = FlakyTransport(task7_redis["transport"], fail=True, non_retryable=True)
            worker = OutboxPublisherWorker(client, transport, settings, worker_id="pub-4")
            assert await worker.run_once() == 0
            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
                )
                == "dead_letter"
            )
        finally:
            await client.dispose()


# =============================================================================
# Leasing + concurrency (Phase 7 / 16)
# =============================================================================


class TestLeasingAndConcurrency:
    async def test_lease_prevents_double_claim_then_expires(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            outbox_id, _ = await _enqueue(client, actor, make_envelope())
            now = utc_now()

            # Worker A claims → lease valid
            async with client.session() as session:
                claimed = await OutboxRepository(session).claim_next(
                    worker_id="worker-a", batch_size=10, lease_seconds=60, now=now
                )
            assert [r.outbox_id for r in claimed] == [outbox_id]

            # Worker B cannot claim while the lease is live
            async with client.session() as session:
                again = await OutboxRepository(session).claim_next(
                    worker_id="worker-b", batch_size=10, lease_seconds=60, now=now
                )
            assert again == [], "a live lease must block re-claiming"

            # Worker A "crashes"; the lease expires; B reclaims
            await _expire_lease(task7_db["url"], outbox_id)
            async with client.session() as session:
                reclaimed = await OutboxRepository(session).claim_next(
                    worker_id="worker-b",
                    batch_size=10,
                    lease_seconds=60,
                    now=utc_now(),
                )
            assert [r.outbox_id for r in reclaimed] == [outbox_id]
            assert reclaimed[0].claimed_by == "worker-b"
            assert reclaimed[0].attempts == 2, "attempts advance on every claim"
        finally:
            await client.dispose()

    async def test_concurrent_workers_claim_disjoint_rows(self, task7_db) -> None:
        """N workers racing: each row is claimed by exactly one worker."""
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            ids = []
            for _ in range(12):
                outbox_id, _ = await _enqueue(client, actor, make_envelope())
                ids.append(outbox_id)

            async def claim(worker_id: str) -> list[uuid.UUID]:
                async with client.session() as session:
                    rows = await OutboxRepository(session).claim_next(
                        worker_id=worker_id, batch_size=10, lease_seconds=60, now=utc_now()
                    )
                    return [r.outbox_id for r in rows]

            claims = await asyncio.gather(*[claim(f"worker-{i}") for i in range(5)])
            claimed_ids = [oid for batch in claims for oid in batch]
            assert len(claimed_ids) == 12, "all rows must be claimed exactly once"
            assert len(set(claimed_ids)) == 12, "no row may be claimed twice"

            owners = await scalar(
                task7_db["url"],
                "SELECT count(DISTINCT claimed_by) FROM outbox_events WHERE status = 'processing'",
            )
            assert owners >= 1
        finally:
            await client.dispose()

    async def test_redis_unavailable_keeps_row_retryable(self, task7_db, task7_redis) -> None:
        """Redis down → the durable outbox row stays retryable, never lost."""
        stream = f"hotelops:t7:down:{uuid.uuid4().hex[:8]}"
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            outbox_id, _ = await _enqueue(client, actor, make_envelope())

            settings = _worker_settings(task7_db["name"], stream)
            # A transport that always fails = Redis permanently unavailable
            transport = FlakyTransport(task7_redis["transport"], fail=True)
            worker = OutboxPublisherWorker(client, transport, settings, worker_id="pub-5")
            for _ in range(2):
                await worker.run_once()
                await _advance_available_at(task7_db["url"], outbox_id)

            status = await scalar(
                task7_db["url"],
                f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            assert status in ("failed", "dead_letter"), "event must remain durable"
            count = await scalar(task7_db["url"], "SELECT count(*) FROM outbox_events")
            assert count == 1, "events are never deleted"
        finally:
            await client.dispose()

    async def test_crash_after_publish_produces_duplicate_delivery(
        self, task7_db, task7_redis
    ) -> None:
        """Publisher crash between publish and mark → re-publish (at-least-once)."""
        stream = f"hotelops:t7:crash:{uuid.uuid4().hex[:8]}"
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope()
            outbox_id, event_id = await _enqueue(client, actor, envelope)

            settings = _worker_settings(task7_db["name"], stream)
            transport = FlakyTransport(task7_redis["transport"])
            worker = OutboxPublisherWorker(client, transport, settings, worker_id="pub-6")

            # Simulate: claim + publish OK, then crash BEFORE mark_published
            async with client.session() as session:
                rows = await OutboxRepository(session).claim_next(
                    worker_id="pub-6", batch_size=10, lease_seconds=60, now=utc_now()
                )
            assert len(rows) == 1
            await worker._publish(rows[0])  # published to Redis, row still processing
            assert await task7_redis["transport"].stream_length(stream) == 1

            # Lease expires (crash), another cycle re-publishes
            await _expire_lease(task7_db["url"], outbox_id)
            assert await worker.run_once() == 1
            assert await task7_redis["transport"].stream_length(stream) == 2

            # Both stream messages carry the SAME event_id — downstream
            # dedup (inbox (source, source_message_id)) collapses them.
            entries = await _read_stream(task7_redis["transport"], stream)
            assert all(e["event_id"] == str(event_id) for e in entries)
        finally:
            await client.dispose()


# =============================================================================
# Raw helpers
# =============================================================================


async def _fetch_outbox_row(url: str, outbox_id: uuid.UUID) -> tuple:
    engine = query_engine(url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, attempts, COALESCE(last_error, '') AS last_error, "
                        "payload FROM outbox_events WHERE outbox_id = :oid"
                    ),
                    {"oid": outbox_id},
                )
            ).one()
            return row.status, row.attempts, row.last_error, row.payload
    finally:
        await engine.dispose()


async def _read_stream(transport, stream: str) -> list[dict]:
    """Read all messages from a stream (bypassing consumer groups)."""
    entries = await transport._redis.client.xrange(stream)  # type: ignore[attr-defined]
    messages = []
    for message_id, fields in entries:
        messages.append({"id": message_id, **fields})
    return messages
