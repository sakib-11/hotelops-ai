"""Integration tests for the Task 7 inbox consumer (Phase 10).

Runs the real InboxService + InboxConsumerWorker + migration 016 schema
against a scratch TimescaleDB:

  - receive dedup: (source, source_message_id) rejects duplicates
  - duplicate delivery produces exactly ONE business effect
  - effect failure → persisted backoff retry → dead-letter on exhaustion
  - an effect with no registered handler dead-letters immediately
  - a crashing effect rolls back its partial writes (savepoint)
  - concurrent consumers: one logical effect per message
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.application.services.inbox import InboxService
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.workers.inbox_consumer import InboxConsumerWorker
from tests.integration._task7_helpers import (
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
        reason="Set INTEGRATION_TESTS=1 and start PostgreSQL",
    ),
]


async def _scaffold_effect_table(url: str) -> None:
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE effect_log (event_id uuid PRIMARY KEY, "
                    "value text NOT NULL, processed_by text NOT NULL)"
                )
            )
    finally:
        await engine.dispose()


def _effect_counter() -> tuple[list[str], object]:
    """Returns (events, handler) — the handler records every effect run.

    The inbox row carries the event identity as ``source_message_id``
    (the canonical EventEnvelope event_id string); the payload envelope
    is available under ``row.payload``.
    """
    events: list[str] = []

    async def handler(session, row) -> None:
        events.append(row.source_message_id)
        await session.execute(
            text(
                "INSERT INTO effect_log (event_id, value, processed_by) "
                "VALUES (:eid, :value, 'consumer')"
            ),
            {"eid": row.source_message_id, "value": "processed"},
        )

    return events, handler


def _failing_handler(error: Exception | None = None):
    async def handler(session, row) -> None:
        await session.execute(
            text(
                "INSERT INTO effect_log (event_id, value, processed_by) "
                "VALUES (:eid, :value, 'partial')"
            ),
            {"eid": row.source_message_id, "value": "partial-write"},
        )
        raise error or RuntimeError("effect failed")

    return handler


def _consumer_settings(db_name: str, **overrides):
    return scratch_settings(
        db_name,
        INBOX_BACKOFF_JITTER=0.0,
        INBOX_BACKOFF_BASE=1.0,
        INBOX_BACKOFF_MAX=60.0,
        INBOX_MAX_ATTEMPTS=3,
        INBOX_LEASE_SECONDS=60,
        **overrides,
    )


async def _receive(client: DatabaseClient, tenant_id: uuid.UUID, envelope, source="outbox"):
    async with client.session() as session:
        return await InboxService().receive(
            session,
            source=source,
            envelope=envelope,
            tenant_id=tenant_id,
        )


class TestReceiveDeduplication:
    async def test_duplicate_receive_returns_none(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            envelope = make_envelope()
            first = await _receive(client, uuid.uuid4(), envelope)
            second = await _receive(client, uuid.uuid4(), envelope)
            assert first is not None
            assert second is None, "duplicate delivery must be rejected"
            assert await scalar(task7_db["url"], "SELECT count(*) FROM inbox_messages") == 1
        finally:
            await client.dispose()

    async def test_distinct_consumers_get_distinct_rows(self, task7_db) -> None:
        """Consumer A + Event X is a different unit from Consumer B + Event X."""
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            envelope = make_envelope()
            await _receive(client, uuid.uuid4(), envelope, source="consumer-a")
            await _receive(client, uuid.uuid4(), envelope, source="consumer-b")
            assert await scalar(task7_db["url"], "SELECT count(*) FROM inbox_messages") == 2
        finally:
            await client.dispose()


class TestConsumerLifecycle:
    async def test_process_success(self, task7_db) -> None:
        await _scaffold_effect_table(task7_db["url"])
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            envelope = make_envelope(event_type="test.effect")
            await _receive(client, uuid.uuid4(), envelope)

            events, handler = _effect_counter()
            settings = _consumer_settings(task7_db["name"])
            worker = InboxConsumerWorker(
                client, settings, effect_handlers={"test.effect": handler}, worker_id="c1"
            )
            assert await worker.run_once() == 1
            assert events == [str(envelope.event_id)]
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT count(*) FROM inbox_messages WHERE status = 'processed'",
                )
                == 1
            )
        finally:
            await client.dispose()

    async def test_duplicate_delivery_produces_one_effect(self, task7_db) -> None:
        """Redelivery (at-least-once) → one logical business effect."""
        await _scaffold_effect_table(task7_db["url"])
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            tenant = uuid.uuid4()
            envelope = make_envelope(event_type="test.effect")
            await _receive(client, tenant, envelope)
            assert await _receive(client, tenant, envelope) is None  # duplicate delivery

            events, handler = _effect_counter()
            worker = InboxConsumerWorker(
                client,
                _consumer_settings(task7_db["name"]),
                effect_handlers={"test.effect": handler},
                worker_id="c2",
            )
            await worker.run_once()
            await worker.run_once()  # a second cycle must find nothing to do
            assert events == [str(envelope.event_id)], "exactly one effect"
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 1
        finally:
            await client.dispose()

    async def test_failing_effect_retries_then_dead_letters(self, task7_db) -> None:
        await _scaffold_effect_table(task7_db["url"])
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            envelope = make_envelope(event_type="test.bad")
            await _receive(client, uuid.uuid4(), envelope)

            worker = InboxConsumerWorker(
                client,
                _consumer_settings(task7_db["name"]),  # max_attempts=3
                effect_handlers={"test.bad": _failing_handler()},
                worker_id="c3",
            )
            await worker.run_once()  # attempt 1 → failed
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 0, (
                "partial effect writes must be rolled back"
            )
            for _ in range(2):
                await _advance_available_at(task7_db["url"])
                await worker.run_once()

            status, attempts, last_error = await _fetch_inbox_row(task7_db["url"])
            assert status == "dead_letter"
            assert attempts == 3
            assert "effect failed" in last_error
            # No partial effect row survives, but the failed/dead-letter
            # state itself is durable (message preserved).
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 0
        finally:
            await client.dispose()

    async def test_no_handler_dead_letters_immediately(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            envelope = make_envelope(event_type="test.unhandled")
            await _receive(client, uuid.uuid4(), envelope)

            worker = InboxConsumerWorker(
                client,
                _consumer_settings(task7_db["name"]),
                effect_handlers={},  # no handler for this event_type
                worker_id="c4",
            )
            await worker.run_once()
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT status FROM inbox_messages WHERE event_type = 'test.unhandled'",
                )
                == "dead_letter"
            )
        finally:
            await client.dispose()

    async def test_concurrent_consumers_one_effect(self, task7_db) -> None:
        await _scaffold_effect_table(task7_db["url"])
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            import asyncio

            envelope = make_envelope(event_type="test.effect")
            await _receive(client, uuid.uuid4(), envelope)

            events, handler = _effect_counter()

            async def run_worker(worker_id: str) -> int:
                worker = InboxConsumerWorker(
                    client,
                    _consumer_settings(task7_db["name"]),
                    effect_handlers={"test.effect": handler},
                    worker_id=worker_id,
                )
                return await worker.run_once()

            # Multiple consumers racing on one message: SKIP LOCKED lets
            # exactly one claim it; the others claim nothing.
            outcomes = await asyncio.gather(*[run_worker(f"cx-{i}") for i in range(4)])
            assert sum(outcomes) == 1, "exactly one consumer may process the message"
            assert len(events) == 1
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 1
        finally:
            await client.dispose()

    async def test_consumer_crash_after_claim_recovers_once(self, task7_db) -> None:
        """Phase 14 #7: a consumer crash after claiming leaves the message
        reclaimable after lease expiry; a fresh consumer processes it once."""
        await _scaffold_effect_table(task7_db["url"])
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            from backend.app.infrastructure.database.repositories.inbox import InboxRepository
            from contracts.common import utc_now

            envelope = make_envelope(event_type="test.effect")
            await _receive(client, uuid.uuid4(), envelope)

            # Consumer "crashed" after claiming: the row is 'processing'
            # with a live lease and no effect was committed.
            async with client.session() as session:
                rows = await InboxRepository(session).claim_next(
                    worker_id="crashed-consumer",
                    batch_size=10,
                    lease_seconds=60,
                    now=utc_now(),
                )
            assert len(rows) == 1
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT count(*) FROM effect_log",
                )
                == 0
            )

            # Lease expires (time passes) — the row is reclaimable.
            await _expire_inbox_lease(task7_db["url"])

            events, handler = _effect_counter()
            worker = InboxConsumerWorker(
                client,
                _consumer_settings(task7_db["name"]),
                effect_handlers={"test.effect": handler},
                worker_id="recovering-consumer",
            )
            assert await worker.run_once() == 1, "the fresh consumer recovers the message"
            assert events == [str(envelope.event_id)], "exactly one effect after recovery"
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 1
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT count(*) FROM inbox_messages WHERE status = 'processed'",
                )
                == 1
            )
        finally:
            await client.dispose()


async def _advance_available_at(url: str) -> None:
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE inbox_messages SET available_at = now() - interval '1 second'")
            )
    finally:
        await engine.dispose()


async def _expire_inbox_lease(url: str) -> None:
    """Rewind every inbox lease into the past (never before received_at)."""
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE inbox_messages "
                    "SET claimed_until = GREATEST(received_at, now() - interval '1 second')"
                )
            )
    finally:
        await engine.dispose()


async def _fetch_inbox_row(url: str) -> tuple:
    engine = query_engine(url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, attempts, COALESCE(last_error, '') AS last_error "
                        "FROM inbox_messages ORDER BY inbox_id LIMIT 1"
                    )
                )
            ).one()
            return row.status, row.attempts, row.last_error
    finally:
        await engine.dispose()
