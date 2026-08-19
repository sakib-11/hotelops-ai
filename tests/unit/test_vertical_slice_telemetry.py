"""Task 18.18 — END-TO-END TELEMETRY VERIFICATION.

Traces ONE full vertical-slice run through every boundary of the 18.14
E2E driver:

    fixture → ingestion → detection → tracking → spatial → FSM → rule
        → DB → outbox → worker → evidence → API

and verifies, with the REAL production components (the same seams the
18.14/18.15/18.16 suites use):

1. CORRELATION SURVIVAL — the correlation id bound at event-production
   time is captured onto the envelope by the REAL
   ``OutboxService._inject_trace_context``, then survives the real
   Task 7 outbox payload → worker rebuild → API response (the boundaries
   the Task 8.8 architecture requires the carrier to cross).
2. TRACE SURVIVAL — when a recording span is active, ``_inject_trace_context``
   attaches trace_id/span_id/trace_sampled to the envelope; the outbox
   payload preserves them verbatim, and the worker's continuation seam
   (``trace_context_from_event_attrs`` — the exact function the inbox
   consumer/ingress and outbox publisher use) reconstructs the same parent.
3. LOG FIELDS — the slice's logs contain the full correlation scope:
   tenant_id, venue_id, session_id, source_id, event_id, evidence_id,
   rule_id, rule_version, configuration_version_id, plus the formatter's
   build/service version base schema.
4. SECRETS ABSENT — the allowlist drops non-allowlisted ``extra=`` fields
   and the formatter redacts credential-shaped message content; the slice
   logs contain no token/credential strings.
5. METRICS — the seven pipeline stage counters (frames, detections,
   tracks, occupancy events, persistence, outbox, worker) fire at their
   real firing points with the exact expected values, and the API metric
   (http_requests_total) is registered in the exposition. The outbox
   metric is verified through the REAL Task 7 outbox repository enqueue
   (the E2E's outbox port is a documented fake that never inserts a real
   outbox row).
6. RECONSTRUCTION (STOP condition) — from telemetry alone (logs + metrics
   + the envelope carrier), the vertical slice is fully reconstructable:
   the one logical event, its scope, its evidence, and every stage that
   produced it.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.trace import SpanContext, TraceFlags

from backend.app.api.routes.operational import get_operational_event
from backend.app.application.services.operational_persistence import (
    OperationalPersistenceService,
)
from backend.app.application.services.outbox import OutboxService, _inject_trace_context
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.logging import JsonFormatter
from backend.app.infrastructure.observability import context as obs_context
from backend.app.infrastructure.observability import metrics
from backend.app.infrastructure.observability.tracing import trace_context_from_event_attrs
from contracts.common import EventId
from contracts.events import EventEnvelope
from tests.unit.test_vertical_slice_api import FakeSession as ApiSession
from tests.unit.test_vertical_slice_e2e import (
    IDS,
    _install_deterministic_seams,
    _run_e2e,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeSession as PersistenceSession,
)
from tests.unit.test_vertical_slice_persistence import FakeStore

pytestmark = pytest.mark.e2e

# The W3C trace identity the fake recording span serves (the OTel SDK is
# never installed in this process — the seam mirrors the 18.14 lazy-SDK
# convention: the REAL _inject_trace_context runs, only the span source is
# deterministic).
TRACE_ID = "ab" * 16
SPAN_ID = "cd" * 8


@pytest.fixture(autouse=True)
def _deterministic_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the same deterministic seams as the 18.14 E2E driver."""
    _install_deterministic_seams(monkeypatch)


def _record(
    msg: str = "hello world",
    *,
    level: int = logging.INFO,
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tests.t18.18",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _captured(records: list[logging.LogRecord]) -> str:
    """Flatten captured records into one searchable text: the message plus
    every allowlisted context field attached via ``extra=`` (the same
    fields the JSON formatter emits)."""
    lines: list[str] = []
    for record in records:
        parts = [record.getMessage()]
        for key in (
            "request_id",
            "correlation_id",
            "trace_id",
            "span_id",
            "actor_id",
            "tenant_id",
            "venue_id",
            "job_id",
            "session_id",
            "event_id",
            "evidence_id",
            "camera_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                parts.append(f"{key}={value}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _enable_metrics_fresh() -> None:
    """Reset the metrics module to a fresh enabled registry (the module is
    process-global; the evidence probe resets the same way)."""
    metrics._enabled = False
    metrics._registry = None
    settings = Settings(_env_file=None, OBSERVABILITY_METRICS_ENABLED=True)  # type: ignore[call-arg]
    assert metrics.configure_metrics(settings) is True


@pytest.fixture
def _metrics_restore() -> None:
    """Disable metrics after the test so no module-global state leaks."""
    yield
    metrics._enabled = False
    metrics._registry = None


def _metric_count(exposition: str, name: str) -> int:
    """Parse ``<name>_total <value>`` from the Prometheus exposition."""
    for line in exposition.splitlines():
        if line.startswith(name + "_total "):
            return int(float(line.split()[-1]))
    return -1


class _RecordingSpan:
    """A fake recording span served to ``trace.get_current_span`` — the
    REAL ``OutboxService._inject_trace_context`` then runs unchanged and
    captures the trace identity onto the envelope (the production →
    outbox hop of Task 8.8)."""

    def is_recording(self) -> bool:
        return True

    def get_span_context(self) -> SpanContext:
        return SpanContext(
            trace_id=int(TRACE_ID, 16),
            span_id=int(SPAN_ID, 16),
            is_remote=False,
            trace_flags=TraceFlags(0x01),
        )


class _RealOutboxPath:
    """The production outbox composition over the in-memory transaction
    fake: the idempotency PRE-CHECK answers against the durable store
    (mirroring the real SQL query's semantics — the unique event_id
    arbiter still runs), while the enqueue runs the REAL
    ``OutboxService`` (trace/correlation injection) and the REAL
    ``OutboxRepository`` (durable row + the outbox metric)."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._service = OutboxService()

    async def find_by_event_id(
        self,
        session: Any,
        event_id: uuid.UUID | str,
    ) -> bool:
        return uuid.UUID(str(event_id)) in self._store.outbox_by_event

    async def enqueue_event(
        self,
        session: Any,
        *,
        actor: Any,
        envelope: EventEnvelope[Any],
        audit: Any,
        venue_id: uuid.UUID | None = None,
    ) -> Any:
        return await self._service.enqueue_event(
            session,
            actor=actor,
            envelope=envelope,
            audit=audit,
            venue_id=venue_id,
        )


async def _persist_and_query(
    run: Any,
    envelope: EventEnvelope[Any],
) -> tuple[FakeStore, Any]:
    """Persist the fact + envelope through ``OperationalPersistenceService``
    with the REAL ``OutboxService``/``OutboxRepository`` (the production
    outbox boundary the E2E driver's fake outbox port deliberately
    bypasses) into a fresh store, then answer the API route from it."""
    store = FakeStore()
    session = PersistenceSession(store)
    service = OperationalPersistenceService(outbox=_RealOutboxPath(store))
    persisted = await service.persist(
        session, fact=run.snapshots[0], event=envelope, actor=run.actor
    )
    assert persisted.created is True
    await session.commit()
    api_session = ApiSession(
        events={row.event_id: row for row in store.events.values()},
        facts={row.fact_id: row for row in store.facts.values()},
        evidence={},
    )
    api_event = await get_operational_event(
        event_id=EventId(envelope.event_id), actor=run.actor, _perm=None, session=api_session
    )
    return store, api_event


# =============================================================================
# 1 + 2. Correlation / trace context survival across every async boundary
# =============================================================================


class TestCorrelationSurvival:
    async def test_correlation_context_survives_every_async_boundary(self) -> None:
        """The correlation id bound at production time (as the Task 8.4
        middleware binds it) is captured onto the envelope by the REAL
        ``_inject_trace_context`` at the outbox boundary and survives the
        real outbox payload → worker rebuild → API response."""
        correlation = "corr-18-18-slice"
        tokens = obs_context.bind(
            obs_context.RequestContext(
                request_id="req-18-18-slice",
                correlation_id=correlation,
                trace_id="12" * 16,
                span_id="34" * 8,
                sampled=True,
            )
        )
        try:
            run = await _run_e2e()
            assert run.event is not None
            # Production → outbox: the REAL capture step (OutboxService
            # runs this at enqueue time) stamps the correlation id.
            envelope = _inject_trace_context(run.event)
            assert envelope.correlation_id == correlation
            # Persist through the REAL Task 7 outbox into a fresh store.
            store, api_event = await _persist_and_query(run, envelope)
            # Outbox → worker: the outbox payload IS the serialized
            # envelope — the worker rebuilds exactly this carrier.
            outbox_row = store.outbox_by_event[uuid.UUID(str(envelope.event_id))]
            restored = EventEnvelope[Any].model_validate(outbox_row.payload)
            assert restored.correlation_id == correlation
            # API: the response DTO carries the same correlation id.
            assert api_event.correlation_id == correlation
        finally:
            obs_context.unbind(tokens)

    async def test_trace_context_is_captured_and_survives_to_the_worker_seam(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With a recording span active, the REAL _inject_trace_context
        attaches trace_id/span_id/trace_sampled at production; the real
        outbox payload preserves them verbatim; and the worker's
        continuation seam reconstructs the same parent trace."""
        monkeypatch.setattr(otel_trace, "get_current_span", lambda: _RecordingSpan())
        run = await _run_e2e()
        assert run.event is not None

        # Production → outbox: trace identity captured by the real seam.
        envelope = _inject_trace_context(run.event)
        assert envelope.trace_id == TRACE_ID
        assert envelope.span_id == SPAN_ID
        assert envelope.trace_sampled is True
        # Outbox → worker: the carrier survives byte-for-byte through the
        # real Task 7 outbox repository insert.
        store, _api_event = await _persist_and_query(run, envelope)
        outbox_row = store.outbox_by_event[uuid.UUID(str(envelope.event_id))]
        restored = EventEnvelope[Any].model_validate(outbox_row.payload)
        assert restored.trace_id == TRACE_ID
        assert restored.span_id == SPAN_ID
        assert restored.trace_sampled is True
        # Worker continuation seam: the exact function the inbox
        # consumer/ingress and outbox publisher use to parent their spans
        # reconstructs the ORIGINAL trace identity.
        parent = trace_context_from_event_attrs(
            restored.trace_id,
            restored.span_id,
            restored.trace_sampled,
        )
        assert parent is not None
        assert parent.trace_id == TRACE_ID
        assert parent.span_id == SPAN_ID
        assert parent.sampled is True


# =============================================================================
# 3. Log fields — the slice's logs reconstruct the full correlation scope
# =============================================================================


class TestLogFields:
    async def test_logs_contain_the_full_slice_scope(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Across the slice's real log records (rule evaluation, persistence,
        worker effect), every required correlation identity is present:
        tenant_id, venue_id, session_id, source_id, event_id, evidence_id,
        rule_id, rule_version, configuration_version_id."""
        with caplog.at_level(logging.INFO):
            run = await _run_e2e()
        assert run.event is not None
        text = _captured(caplog.records)

        # Scope + source + event + evidence identities.
        assert str(IDS["tenant_id"]) in text
        assert str(IDS["venue_id"]) in text
        assert str(IDS["session_id"]) in text
        assert str(IDS["camera_id"]) in text
        assert "source_id=rule:occupancy_session:v1" in text  # worker log
        assert f"event_id={run.event.event_id}" in text
        # Evidence id is present where applicable — the worker effect log.
        (ref_row,) = run.evidence.rows.values()
        assert f"evidence_id={ref_row.ref_id}" in text
        # Rule + configuration provenance.
        assert "rule_id=occupancy_session" in text
        assert "rule_version=v1" in text
        assert str(IDS["configuration_version_id"]) in text

    def test_formatter_base_schema_carries_build_and_service_version(self) -> None:
        """Every structured record carries service, environment, version and
        build metadata (the build/service version requirement)."""
        formatter = JsonFormatter(
            service="HotelOps AI",
            environment="development",
            version="0.1.0",
            build_commit="abc123",
            build_timestamp="2026-08-17T00:00:00Z",
        )
        payload = json.loads(formatter.format(_record()))
        assert payload["service"] == "HotelOps AI"
        assert payload["environment"] == "development"
        assert payload["version"] == "0.1.0"
        assert payload["build_commit"] == "abc123"
        assert payload["build_timestamp"] == "2026-08-17T00:00:00Z"
        # The correlation/event ids the slice attaches flow through as
        # structured fields, not buried in free text.
        payload2 = json.loads(
            formatter.format(
                _record(
                    extra={"tenant_id": str(IDS["tenant_id"]), "event_id": str(IDS["tenant_id"])}
                )
            )
        )
        assert payload2["tenant_id"] == str(IDS["tenant_id"])


# =============================================================================
# 4. Secrets absent — allowlist + redaction at the output boundary
# =============================================================================


class TestSecretsAbsent:
    def test_allowlist_and_redaction_keep_secrets_out(self) -> None:
        """A credential in the message body is redacted; a credential in an
        un-allowlisted extra field is dropped; the task's evidence_id and
        job_id fields (where applicable) flow through."""
        formatter = JsonFormatter(service="s", environment="e", version="v")

        # Message-body redaction (single controlled mechanism).
        payload = json.loads(
            formatter.format(_record("connecting with password=hunter2 and api_key=abc123def456"))
        )
        assert "hunter2" not in payload["message"]
        assert "abc123def456" not in payload["message"]
        assert "[REDACTED]" in payload["message"]

        # Allowlist: secrets via extra= never appear.
        payload2 = json.loads(
            formatter.format(
                _record(extra={"password": "s3cr3t", "secret_key": "k", "tenant_id": "ok"})
            )
        )
        assert "password" not in payload2
        assert "secret_key" not in payload2
        assert "s3cr3t" not in json.dumps(payload2)
        assert payload2["tenant_id"] == "ok"

        # evidence_id / job_id are allowlisted (the task's "where
        # applicable" fields).
        payload3 = json.loads(
            formatter.format(_record(extra={"evidence_id": "ev-1", "job_id": "job-1"}))
        )
        assert payload3["evidence_id"] == "ev-1"
        assert payload3["job_id"] == "job-1"

    async def test_slice_logs_contain_no_secret_shaped_strings(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The full slice run emits no token/credential-shaped content."""
        with caplog.at_level(logging.INFO):
            await _run_e2e()
        text = _captured(caplog.records)
        assert "Bearer " not in text
        assert "eyJ" not in text  # JWT signature
        assert "password=" not in text
        assert "secret" not in text.lower().replace("[REDACTED]", "")


# =============================================================================
# 5. Metrics — every stage has a real counter firing at its real point
# =============================================================================


class TestMetrics:
    async def test_pipeline_metrics_fire_at_every_stage(self, _metrics_restore: None) -> None:
        """One full slice run with metrics enabled: the pipeline stage
        counters that the E2E's real components run fire with the exact
        expected values, and the outbox + API metrics are registered."""
        _enable_metrics_fresh()
        run = await _run_e2e()
        assert run.event is not None

        body, _ = metrics.render()
        text = body.decode()

        # frames: every fixture frame crossed the ingestion boundary.
        assert _metric_count(text, "pipeline_frames") >= 30
        # detections/tracks: the person was detected and tracked.
        assert _metric_count(text, "pipeline_detections") >= 1
        assert _metric_count(text, "pipeline_tracks") >= 1
        # occupancy events: exactly one logical event produced.
        assert _metric_count(text, "pipeline_occupancy_events") == 1
        # persistence: exactly one authoritative commit (the E2E's real
        # OperationalPersistenceService, fake outbox port).
        assert _metric_count(text, "pipeline_persistence") == 1
        # worker: exactly one effect applied by the real handler.
        assert _metric_count(text, "pipeline_worker") == 1
        # API: the HTTP request metric is registered (fired by the metrics
        # middleware — exercised by test_observability_verification).
        assert "http_requests_total" in text
        # outbox: registered; its firing needs the REAL outbox repository
        # insert, which the E2E's fake outbox port bypasses — verified in
        # test_outbox_metric_fires_at_the_real_enqueue_point.
        assert "pipeline_outbox" in text

    async def test_outbox_metric_fires_at_the_real_enqueue_point(
        self,
        _metrics_restore: None,
    ) -> None:
        """The outbox counter fires exactly once per durable outbox row
        inserted by the REAL OutboxRepository.enqueue."""
        _enable_metrics_fresh()
        run = await _run_e2e()
        assert run.event is not None
        envelope = _inject_trace_context(run.event)
        await _persist_and_query(run, envelope)

        body, _ = metrics.render()
        text = body.decode()
        assert _metric_count(text, "pipeline_outbox") == 1
        # Two authoritative commits ran: the E2E's own persist and the real
        # Task 7 persist above.
        assert _metric_count(text, "pipeline_persistence") == 2


# =============================================================================
# 6. Reconstruction — STOP condition: telemetry alone rebuilds the slice
# =============================================================================


class TestReconstruction:
    async def test_telemetry_alone_reconstructs_the_vertical_slice(
        self,
        caplog: pytest.LogCaptureFixture,
        _metrics_restore: None,
    ) -> None:
        """STOP condition: from logs + metrics + the envelope carrier alone,
        the vertical slice is fully reconstructable — one logical event,
        its full scope, its evidence, and every stage that produced it."""
        _enable_metrics_fresh()
        correlation = "corr-18-18-reconstruct"
        tokens = obs_context.bind(
            obs_context.RequestContext(
                request_id="req-18-18-reconstruct",
                correlation_id=correlation,
                trace_id="56" * 16,
                span_id="78" * 8,
                sampled=True,
            )
        )
        try:
            with caplog.at_level(logging.INFO):
                run = await _run_e2e()
            assert run.event is not None
            envelope = _inject_trace_context(run.event)
            # The real Task 7 outbox boundary (fires the outbox counter).
            store, api_event = await _persist_and_query(run, envelope)
        finally:
            obs_context.unbind(tokens)

        event_id = str(run.event.event_id)
        (ref_row,) = run.evidence.rows.values()

        # --- From LOGS alone ---
        text = _captured(caplog.records)
        assert f"event_id={event_id}" in text  # the one logical event
        assert f"evidence_id={ref_row.ref_id}" in text  # its evidence
        assert f"correlation_id={correlation}" in text  # the trace
        assert "rule_id=occupancy_session" in text
        assert "rule_version=v1" in text
        assert str(IDS["configuration_version_id"]) in text
        assert str(IDS["tenant_id"]) in text
        assert str(IDS["venue_id"]) in text
        assert str(IDS["session_id"]) in text
        assert "source_id=rule:occupancy_session:v1" in text

        # --- From METRICS alone ---
        body, _ = metrics.render()
        exposition = body.decode()
        assert _metric_count(exposition, "pipeline_frames") >= 30
        assert _metric_count(exposition, "pipeline_detections") >= 1
        assert _metric_count(exposition, "pipeline_tracks") >= 1
        assert _metric_count(exposition, "pipeline_occupancy_events") == 1
        # Two persistence commits ran: the E2E's authoritative persist and
        # the real Task 7 persist above.
        assert _metric_count(exposition, "pipeline_persistence") == 2
        assert _metric_count(exposition, "pipeline_outbox") == 1
        assert _metric_count(exposition, "pipeline_worker") == 1

        # --- From the CARRIER (envelope → outbox → API) alone ---
        # The outbox payload round-trips to the exact produced envelope.
        outbox_row = store.outbox_by_event[uuid.UUID(str(event_id))]
        restored = EventEnvelope[Any].model_validate(outbox_row.payload)
        assert restored.correlation_id == correlation
        assert restored.model_dump(mode="json") == envelope.model_dump(mode="json")
        # The API answers the same logical event + correlation.
        assert api_event.event_id == run.event.event_id
        assert api_event.correlation_id == correlation
        assert api_event.payload.rule_version == "v1"
        assert run.api_evidence.event_id == run.event.event_id
        assert str(run.api_evidence.evidence_ref_id) == str(ref_row.ref_id)
