"""End-to-end Task 7 flow (Phase 22).

API-independent full pipeline against a scratch TimescaleDB and real
Redis:

  enqueue (validated envelope + audit)
    → outbox (COMMIT)
    → publisher (lease) → Redis stream
    → ingress bridge (consumer group, dedup insert + ack)
    → inbox (COMMIT)
    → consumer (effect + processed atomically)
    → COMMIT

Also verifies at-least-once + dedup end to end: re-publishing the same
event produces a second stream message but NOT a second business effect.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.application.services.outbox import OutboxService
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.workers.inbox_consumer import InboxConsumerWorker
from backend.app.workers.inbox_ingress import InboxIngressBridge
from backend.app.workers.outbox_publisher import OutboxPublisherWorker
from contracts.audit import AuditActionCategory
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


async def _scaffold_effect_table(url: str) -> None:
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE effect_log (event_id uuid PRIMARY KEY, value text NOT NULL)")
            )
    finally:
        await engine.dispose()


def _make_settings(db_name: str, stream: str, group: str):
    return scratch_settings(
        db_name,
        OUTBOX_BACKOFF_JITTER=0.0,
        OUTBOX_MAX_ATTEMPTS=3,
        INBOX_BACKOFF_JITTER=0.0,
        INBOX_MAX_ATTEMPTS=3,
        REDIS_STREAM_EVENTS=stream,
        REDIS_CONSUMER_GROUP=group,
    )


async def _enqueue(client: DatabaseClient, actor, envelope):
    audit = AuditEventBuilder.from_actor(
        actor=actor,
        action="event.enqueue",
        action_category=AuditActionCategory.SYSTEM,
    )
    async with client.session() as session:
        return await OutboxService().enqueue_event(
            session, actor=actor, envelope=envelope, audit=audit
        )


class TestFullPipeline:
    async def test_complete_flow(self, task7_db, task7_redis) -> None:
        await _scaffold_effect_table(task7_db["url"])
        stream = f"hotelops:t7:e2e:{uuid.uuid4().hex[:8]}"
        group = f"grp-{uuid.uuid4().hex[:8]}"
        settings = _make_settings(task7_db["name"], stream, group)

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope(event_type="test.e2e.effect")
            row = await _enqueue(client, actor, envelope)
            outbox_id = row.outbox_id

            effects: list[str] = []

            async def handler(session, inbox_row) -> None:
                effects.append(str(inbox_row.source_message_id))
                await session.execute(
                    text("INSERT INTO effect_log (event_id, value) VALUES (:eid, :value)"),
                    {"eid": inbox_row.source_message_id, "value": "done"},
                )

            transport = task7_redis["transport"]
            publisher = OutboxPublisherWorker(client, transport, settings, worker_id="e2e-pub")
            bridge = InboxIngressBridge(client, transport, settings, consumer="e2e-bridge")
            consumer = InboxConsumerWorker(
                client,
                settings,
                effect_handlers={"test.e2e.effect": handler},
                worker_id="e2e-con",
            )

            # 1. outbox → Redis
            assert await publisher.run_once() == 1
            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
                )
                == "published"
            )
            assert await transport.stream_length(stream) == 1

            # 2. Redis → inbox (dedup insert + ack)
            assert await bridge.run_once() == 1
            assert await transport.pending_count(stream, group) == 0
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT count(*) FROM inbox_messages WHERE status = 'pending'",
                )
                == 1
            )

            # 3. inbox → business effect (atomic)
            assert await consumer.run_once() == 1
            assert effects == [str(envelope.event_id)]
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT count(*) FROM inbox_messages WHERE status = 'processed'",
                )
                == 1
            )
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 1

            # 4. audit row committed with the outbox
            assert await scalar(task7_db["url"], "SELECT count(*) FROM audit_events") == 1
        finally:
            await client.dispose()

    async def test_observability_context_survives_async_boundary(
        self, task7_db, task7_redis
    ) -> None:
        """Task 8.8: event_id, correlation_id and trace context survive the
        full outbox → publisher → Redis → ingress → inbox → consumer flow."""
        await _scaffold_effect_table(task7_db["url"])
        stream = f"hotelops:t8:flow:{uuid.uuid4().hex[:8]}"
        group = f"grp-{uuid.uuid4().hex[:8]}"
        settings = _make_settings(task7_db["name"], stream, group)

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            # Envelope carrying event_id + correlation_id + trace context.
            envelope = make_envelope(
                event_type="test.telemetry.flow",
                correlation_id="corr-8-8",
                trace_id="ab" * 16,
                span_id="cd" * 8,
                trace_sampled=True,
            )
            row = await _enqueue(client, actor, envelope)
            outbox_id = row.outbox_id

            effects: list[str] = []

            async def handler(session, inbox_row) -> None:
                effects.append(str(inbox_row.source_message_id))
                await session.execute(
                    text("INSERT INTO effect_log (event_id, value) VALUES (:eid, :value)"),
                    {"eid": inbox_row.source_message_id, "value": "done"},
                )

            transport = task7_redis["transport"]
            publisher = OutboxPublisherWorker(client, transport, settings, worker_id="t8-pub")
            bridge = InboxIngressBridge(client, transport, settings, consumer="t8-bridge")
            consumer = InboxConsumerWorker(
                client,
                settings,
                effect_handlers={"test.telemetry.flow": handler},
                worker_id="t8-con",
            )

            # Full pipeline: enqueue -> outbox -> publisher -> Redis -> ingress -> inbox -> consumer.
            assert await publisher.run_once() == 1
            assert await bridge.run_once() == 1
            assert await consumer.run_once() == 1

            # event_id reached the business effect (source_message_id).
            assert effects == [str(envelope.event_id)]

            # The inbox row payload preserved event_id + correlation_id + trace context.
            payload = await scalar(
                task7_db["url"],
                f"SELECT payload FROM inbox_messages WHERE source_message_id = "
                f"'{envelope.event_id}'::text",
            )
            assert payload["event_id"] == str(envelope.event_id)
            assert payload["correlation_id"] == "corr-8-8"
            assert payload["trace_id"] == "ab" * 16
            assert payload["span_id"] == "cd" * 8
            assert payload.get("trace_sampled") is True

            # The outbox row payload also carried the trace context.
            outbox_payload = await scalar(
                task7_db["url"],
                f"SELECT payload FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            assert outbox_payload["trace_id"] == "ab" * 16
            assert outbox_payload["span_id"] == "cd" * 8
        finally:
            await client.dispose()

    async def test_missing_telemetry_context_does_not_break_processing(
        self, task7_db, task7_redis
    ) -> None:
        """Task 8.8 req 10: an envelope without trace context still flows."""
        await _scaffold_effect_table(task7_db["url"])
        stream = f"hotelops:t8:noctx:{uuid.uuid4().hex[:8]}"
        group = f"grp-{uuid.uuid4().hex[:8]}"
        settings = _make_settings(task7_db["name"], stream, group)

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            # No correlation_id, no trace context.
            envelope = make_envelope(event_type="test.telemetry.nocontext")
            assert envelope.trace_id is None
            assert envelope.correlation_id is None
            await _enqueue(client, actor, envelope)

            effects: list[str] = []

            async def handler(session, inbox_row) -> None:
                effects.append(str(inbox_row.source_message_id))
                await session.execute(
                    text("INSERT INTO effect_log (event_id, value) VALUES (:eid, :value)"),
                    {"eid": inbox_row.source_message_id, "value": "done"},
                )

            transport = task7_redis["transport"]
            publisher = OutboxPublisherWorker(client, transport, settings, worker_id="nc-pub")
            bridge = InboxIngressBridge(client, transport, settings, consumer="nc-bridge")
            consumer = InboxConsumerWorker(
                client,
                settings,
                effect_handlers={"test.telemetry.nocontext": handler},
                worker_id="nc-con",
            )

            assert await publisher.run_once() == 1
            assert await bridge.run_once() == 1
            assert await consumer.run_once() == 1

            # The event was still processed end to end.
            assert effects == [str(envelope.event_id)]
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

    async def test_duplicate_delivery_end_to_end_one_effect(self, task7_db, task7_redis) -> None:
        """Re-publish (publisher crash) → second message, still ONE effect."""
        await _scaffold_effect_table(task7_db["url"])
        stream = f"hotelops:t7:dup:{uuid.uuid4().hex[:8]}"
        group = f"grp-{uuid.uuid4().hex[:8]}"
        settings = _make_settings(task7_db["name"], stream, group)

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope(event_type="test.dup.effect")
            await _enqueue(client, actor, envelope)

            effects: list[str] = []

            async def handler(session, inbox_row) -> None:
                effects.append(str(inbox_row.source_message_id))
                await session.execute(
                    text("INSERT INTO effect_log (event_id, value) VALUES (:eid, :value)"),
                    {"eid": inbox_row.source_message_id, "value": "done"},
                )

            transport = task7_redis["transport"]
            publisher = OutboxPublisherWorker(client, transport, settings, worker_id="dup-pub")
            bridge = InboxIngressBridge(client, transport, settings, consumer="dup-bridge")
            consumer = InboxConsumerWorker(
                client,
                settings,
                effect_handlers={"test.dup.effect": handler},
                worker_id="dup-con",
            )

            await publisher.run_once()
            await bridge.run_once()
            await consumer.run_once()
            assert len(effects) == 1

            # Publisher "crashed" before marking published → re-publish
            await transport.publish(
                stream,
                event_id=str(envelope.event_id),
                event_type=envelope.event_type,
                envelope_json=envelope.model_dump_json(),
                tenant_id=str(actor.tenant_id),
                venue_id=None,
                schema_version=envelope.schema_version,
                correlation_id=envelope.correlation_id,
            )
            assert await transport.stream_length(stream) == 2

            # Bridge dedups; consumer has nothing new to process
            await bridge.run_once()
            await consumer.run_once()
            assert await scalar(task7_db["url"], "SELECT count(*) FROM inbox_messages") == 1, (
                "duplicate delivery must not create a second inbox row"
            )
            assert len(effects) == 1, "one logical business effect despite redelivery"
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 1
        finally:
            await client.dispose()
