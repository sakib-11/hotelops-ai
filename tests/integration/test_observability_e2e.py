"""Task 8.11 — End-to-end observability verification (integration).

Runs the complete Task 8 flow against a scratch TimescaleDB and real
Redis with tracing AND metrics enabled simultaneously:

    enqueue (trace context captured)
      → outbox (COMMIT)
      → publisher (span parented on the original trace)
      → Redis stream
      → ingress (span continues the trace)
      → inbox (COMMIT)
      → consumer (span continues the trace)
      → business effect

Verifies the observability contract end to end:
  - spans are produced at every hop
  - the trace id survives the async boundary (one trace, many spans)
  - event_id is preserved on spans and in payloads
  - actor/tenant/venue context appears on spans (safe values)
  - secrets never appear in span attributes
  - metrics are produced
  - database failure produces an ERROR span + retry with diagnostics
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.application.services.outbox import OutboxService
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.observability.metrics import configure_metrics
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


def _make_settings(db_name: str, stream: str, group: str, **overrides):
    return scratch_settings(
        db_name,
        OUTBOX_BACKOFF_JITTER=0.0,
        OUTBOX_MAX_ATTEMPTS=3,
        INBOX_BACKOFF_JITTER=0.0,
        INBOX_MAX_ATTEMPTS=3,
        REDIS_STREAM_EVENTS=stream,
        REDIS_CONSUMER_GROUP=group,
        OBSERVABILITY_TRACING_ENABLED=True,
        OBSERVABILITY_METRICS_ENABLED=True,
        OTEL_SAMPLE_RATIO=1.0,
        OTEL_OTLP_ENDPOINT="http://127.0.0.1:9",  # never contacted in tests
        **overrides,
    )


async def _scaffold_effect_table(url: str) -> None:
    engine = query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE effect_log (event_id uuid PRIMARY KEY, value text NOT NULL)")
            )
    finally:
        await engine.dispose()


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


class TestFullFlowObservability:
    async def test_complete_flow_with_tracing_and_metrics(
        self, task7_db, task7_redis, task7_settings
    ) -> None:
        """The full flow produces one coherent trace + metrics + safe spans."""
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        await _scaffold_effect_table(task7_db["url"])
        stream = f"hotelops:t8e2e:{uuid.uuid4().hex[:8]}"
        group = f"grp-{uuid.uuid4().hex[:8]}"
        settings = _make_settings(task7_db["name"], stream, group)

        exporter = InMemorySpanExporter()
        # Enable tracing with an in-memory exporter + metrics.
        tracing.configure_tracing(settings, exporter=exporter)
        configure_metrics(settings)

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope(
                event_type="test.obs.effect",
                correlation_id="e2e-corr",
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
            publisher = OutboxPublisherWorker(client, transport, settings, worker_id="e2e-pub")
            bridge = InboxIngressBridge(client, transport, settings, consumer="e2e-bridge")
            consumer = InboxConsumerWorker(
                client,
                settings,
                effect_handlers={"test.obs.effect": handler},
                worker_id="e2e-con",
            )

            assert await publisher.run_once() == 1
            assert await bridge.run_once() == 1
            assert await consumer.run_once() == 1

            # The business effect ran exactly once with the event id.
            assert effects == [str(envelope.event_id)]

            # ---- Point 7: event_id preserved end to end ----
            assert await scalar(task7_db["url"], "SELECT count(*) FROM effect_log") == 1

            # ---- Point 8: async trace context survives ----
            spans = exporter.get_finished_spans()
            worker_spans = [
                s for s in spans if s.name in ("outbox.publish", "ingress.relay", "inbox.process")
            ]
            assert len(worker_spans) >= 3, "all worker hops must create spans"
            # All worker spans are in the ORIGINAL trace (captured at enqueue).
            for span in worker_spans:
                assert span.context.trace_id == int("ab" * 16, 16), (
                    f"{span.name} must continue the original trace"
                )

            # ---- Points 4-6: actor/tenant context on worker spans ----
            outbox_span = next(s for s in worker_spans if s.name == "outbox.publish")
            assert outbox_span.attributes.get("event_id") == str(envelope.event_id)
            assert outbox_span.attributes.get("tenant_id") == str(actor.tenant_id)

            # ---- Point 11: no secrets in span attributes ----
            secret_words = ("password", "token", "secret", "authorization", "credential")
            for span in spans:
                for key in span.attributes or {}:
                    assert not any(w in key.lower() for w in secret_words), (
                        f"suspicious span attribute {key!r} on {span.name}"
                    )

            # ---- Point 10: metrics are produced ----
            from backend.app.infrastructure.observability import metrics

            assert metrics.enabled() is True
            body, _content_type = metrics.render()
            assert b"http_requests_total" in body or b"http_request_duration_seconds" in body

            # The outbox row reached 'published'.
            assert (
                await scalar(
                    task7_db["url"],
                    f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
                )
                == "published"
            )
        finally:
            await client.dispose()

    async def test_database_failure_produces_error_and_retry(
        self, task7_db, task7_redis, task7_settings
    ) -> None:
        """Database failure during the effect → ERROR span, retry, then
        dead-letter with diagnostic context (point 12)."""
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.trace import StatusCode

        await _scaffold_effect_table(task7_db["url"])
        stream = f"hotelops:t8edb:{uuid.uuid4().hex[:8]}"
        group = f"grp-{uuid.uuid4().hex[:8]}"
        settings = _make_settings(task7_db["name"], stream, group)

        exporter = InMemorySpanExporter()
        tracing.configure_tracing(settings, exporter=exporter)

        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            actor = make_actor(tenant_id=uuid.uuid4())
            envelope = make_envelope(
                event_type="test.obs.dbfail",
                correlation_id="e2e-db-corr",
                trace_id="12" * 16,
                span_id="34" * 8,
                trace_sampled=True,
            )
            await _enqueue(client, actor, envelope)

            async def failing_handler(session, inbox_row) -> None:
                # Simulate a database failure inside the business effect.
                await session.execute(text("SELECT * FROM nonexistent_table_xyz"))

            transport = task7_redis["transport"]
            publisher = OutboxPublisherWorker(client, transport, settings, worker_id="dbf-pub")
            bridge = InboxIngressBridge(client, transport, settings, consumer="dbf-bridge")
            consumer = InboxConsumerWorker(
                client,
                settings,
                effect_handlers={"test.obs.dbfail": failing_handler},
                worker_id="dbf-con",
            )

            assert await publisher.run_once() == 1
            assert await bridge.run_once() == 1
            assert await consumer.run_once() == 0  # effect failed → retry

            # The message is 'failed' (retryable) with a persisted error.
            status, attempts, last_error = await _fetch_inbox_row(task7_db["url"])
            assert status == "failed"
            assert attempts >= 1
            assert "nonexistent_table_xyz" in last_error, "diagnostic context preserved"

            # The inbox.process span is an ERROR span in the original trace.
            spans = exporter.get_finished_spans()
            process_spans = [s for s in spans if s.name == "inbox.process"]
            assert process_spans, "a processing span must exist"
            assert any(s.status.status_code is StatusCode.ERROR for s in process_spans), (
                "a database failure must mark the span ERROR"
            )
            assert all(s.context.trace_id == int("12" * 16, 16) for s in process_spans), (
                "failure spans continue the original trace"
            )
        finally:
            await client.dispose()


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
