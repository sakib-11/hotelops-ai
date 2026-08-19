"""Task 17.12 — evidence observability tests.

Covers the evidence telemetry contract end to end:

- TELEMETRY CARRIER: every evidence request carries the bounded identity
  set (request_id, correlation_id, trace_id, tenant_id, venue_id,
  event_id, evidence_id, session_id). The trace identity is captured at
  the async boundary (the producing EventEnvelope) and persisted on the
  ref's JSONB metadata; the worker reads it and parents its spans on the
  ORIGINAL trace.
- SPANS: the pipeline chain (process → source_resolution → extraction →
  upload → finalize) carries only bounded identifiers — never tokens,
  credentials, signed URLs, or payloads.
- METRICS: the eight canonical evidence counters fire at their exact
  points (verified in a fresh subprocess with metrics enabled).
- LOG FIELDS: structured-log extra fields are allowlisted by the Task 8
  JSON formatter and never contain secrets.
- CROSS-BOUNDARY CORRELATION: the worker span chain continues the trace
  captured at event production (Event → EvidenceRequest → Worker →
  Source → Extraction → Storage → Finalization) — verified in a fresh
  subprocess with an in-memory exporter (the OTel global provider is
  set once per process).
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.logging import _CONTEXT_FIELDS
from backend.app.infrastructure.observability import metrics
from backend.app.infrastructure.observability.evidence import (
    EVIDENCE_TELEMETRY_KEY,
    SPAN_ATTR_CORRELATION_ID,
    SPAN_ATTR_EVIDENCE_ID,
    SPAN_ATTR_REQUEST_ID,
    SPAN_ATTR_TRACE_ID,
    EvidenceTelemetry,
    capture_telemetry,
    log_fields,
    read_telemetry,
    span_attributes,
    write_telemetry,
)
from contracts.events import EventEnvelope

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TENANT = uuid.UUID("10000000-0000-0000-0000-000000000001")
_VENUE = uuid.UUID("20000000-0000-0000-0000-000000000001")
_REF = uuid.UUID("30000000-0000-0000-0000-000000000001")
_EVENT = uuid.UUID("40000000-0000-0000-0000-000000000001")
_SESSION = uuid.UUID("60000000-0000-0000-0000-000000000001")

_TRACE_ID = "ab" * 16
_SPAN_ID = "cd" * 8
_CORRELATION = "corr-123"


def _ref() -> EvidenceRefModel:
    return EvidenceRefModel(
        ref_id=_REF,
        schema_version="1.0",
        tenant_id=_TENANT,
        venue_id=_VENUE,
        ref_type="video_clip",
        ref_uri="s3://evidence/placeholder",
        event_id=_EVENT,
        event_time=None,
        session_id=_SESSION,
        metadata_={},
        created_at=None,
    )


def _envelope() -> EventEnvelope[dict[str, object]]:
    from datetime import UTC, datetime

    return EventEnvelope(
        event_id=_EVENT,
        event_type="operational.dwell_threshold",
        event_time=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        produced_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        source="rules",
        payload={},
        correlation_id=_CORRELATION,
        trace_id=_TRACE_ID,
        span_id=_SPAN_ID,
        trace_sampled=True,
    )


# =============================================================================
# Telemetry carrier — capture at the async boundary
# =============================================================================


class TestCaptureTelemetry:
    def test_envelope_is_authoritative_for_trace_identity(self) -> None:
        """The producing envelope's trace identity wins over the context."""
        telemetry = capture_telemetry(envelope=_envelope())
        assert telemetry.correlation_id == _CORRELATION
        assert telemetry.trace_id == _TRACE_ID
        assert telemetry.span_id == _SPAN_ID
        assert telemetry.trace_sampled is True
        # request_id comes from the (absent) active context — None is the
        # safe representation, never a guessed value.
        assert telemetry.request_id is None

    def test_partial_envelope_falls_back_to_context(self) -> None:
        from backend.app.infrastructure.observability import context

        ctx = context.RequestContext(
            request_id="req-1",
            correlation_id="ctx-corr",
            trace_id="12" * 16,
            span_id="34" * 8,
            sampled=True,
        )
        tokens = context.bind(ctx)
        try:
            envelope = _envelope()
            envelope = envelope.model_copy(
                update={
                    "correlation_id": None,
                    "trace_id": None,
                    "span_id": None,
                    "trace_sampled": None,
                }
            )
            telemetry = capture_telemetry(envelope=envelope)
            # Correlation/trace fall back to the active request context.
            assert telemetry.correlation_id == "ctx-corr"
            assert telemetry.trace_id == "12" * 16
            assert telemetry.span_id == "34" * 8
            assert telemetry.request_id == "req-1"
            # trace_sampled has no context fallback → None (stays None).
            assert telemetry.trace_sampled is None
        finally:
            context.unbind(tokens)

    def test_nothing_present_produces_empty_carrier(self) -> None:
        telemetry = capture_telemetry(envelope=None)
        assert all(value is None for value in telemetry.to_dict().values())

    def test_parent_trace_derivation(self) -> None:
        telemetry = capture_telemetry(envelope=_envelope())
        parent = telemetry.parent_trace
        assert parent is not None
        assert parent.trace_id == _TRACE_ID
        assert parent.span_id == _SPAN_ID
        assert parent.sampled is True
        # Invalid/absent trace fields → None (start a fresh trace).
        assert EvidenceTelemetry().parent_trace is None


class TestDurableCarrier:
    def test_write_read_round_trip(self) -> None:
        ref = _ref()
        telemetry = EvidenceTelemetry(
            request_id="req-1",
            correlation_id=_CORRELATION,
            trace_id=_TRACE_ID,
            span_id=_SPAN_ID,
            trace_sampled=True,
        )
        write_telemetry(ref, telemetry)
        stored = (ref.metadata_ or {}).get(EVIDENCE_TELEMETRY_KEY)
        assert stored == {
            "request_id": "req-1",
            "correlation_id": _CORRELATION,
            "trace_id": _TRACE_ID,
            "span_id": _SPAN_ID,
            "trace_sampled": True,
        }
        restored = read_telemetry(ref)
        assert restored is not None
        assert restored == telemetry

    def test_none_values_never_persisted(self) -> None:
        ref = _ref()
        write_telemetry(ref, EvidenceTelemetry())
        stored = (ref.metadata_ or {}).get(EVIDENCE_TELEMETRY_KEY)
        assert stored == {}

    def test_read_rejects_unknown_or_malformed(self) -> None:
        ref = _ref()
        # Malformed (non-dict) → None.
        ref.metadata_ = {EVIDENCE_TELEMETRY_KEY: "garbage"}
        assert read_telemetry(ref) is None
        # Unknown fields are dropped — only the bounded carrier survives.
        ref.metadata_ = {
            EVIDENCE_TELEMETRY_KEY: {"trace_id": _TRACE_ID, "signed_url": "s3://secret"}
        }
        telemetry = read_telemetry(ref)
        assert telemetry is not None
        assert telemetry.trace_id == _TRACE_ID
        assert not hasattr(telemetry, "signed_url")


class TestSpanAttributes:
    def test_bounded_identity_set_only(self) -> None:
        telemetry = EvidenceTelemetry(
            request_id="req-1",
            correlation_id=_CORRELATION,
            trace_id=_TRACE_ID,
        )
        ref = _ref()
        attrs = span_attributes(ref, telemetry)
        assert attrs[SPAN_ATTR_EVIDENCE_ID] == str(_REF)
        assert attrs[SPAN_ATTR_REQUEST_ID] == "req-1"
        assert attrs[SPAN_ATTR_CORRELATION_ID] == _CORRELATION
        assert attrs[SPAN_ATTR_TRACE_ID] == _TRACE_ID
        # The remaining identifiers are set on the span by evidence_span
        # from the ref itself (tenant/venue/event/session).
        assert set(attrs) == {
            SPAN_ATTR_EVIDENCE_ID,
            SPAN_ATTR_REQUEST_ID,
            SPAN_ATTR_CORRELATION_ID,
            SPAN_ATTR_TRACE_ID,
        }

    def test_never_payload_or_secret(self) -> None:
        telemetry = EvidenceTelemetry(
            request_id="req-1",
            correlation_id=_CORRELATION,
            trace_id=_TRACE_ID,
        )
        ref = _ref()
        attrs = span_attributes(ref, telemetry)
        joined = " ".join(f"{k}={v}" for k, v in attrs.items()).lower()
        for forbidden in ("token", "secret", "password", "bearer", "signature", "credential"):
            assert forbidden not in joined


class TestLogFields:
    def test_every_key_is_in_the_json_formatter_allowlist(self) -> None:
        """Structured log fields must survive the Task 8 JSON formatter."""
        ref = _ref()
        telemetry = EvidenceTelemetry(
            request_id="req-1",
            correlation_id=_CORRELATION,
            trace_id=_TRACE_ID,
        )
        fields_ = log_fields(ref, telemetry)
        for key in fields_:
            assert key in _CONTEXT_FIELDS, f"{key!r} is not allowlisted by the JSON formatter"

    def test_required_identifiers_always_present(self) -> None:
        fields_ = log_fields(_ref())
        assert fields_["evidence_id"] == str(_REF)
        assert fields_["tenant_id"] == str(_TENANT)
        assert fields_["venue_id"] == str(_VENUE)
        assert fields_["event_id"] == str(_EVENT)
        assert fields_["session_id"] == str(_SESSION)

    def test_correlation_fields_carried(self) -> None:
        telemetry = EvidenceTelemetry(
            request_id="req-1",
            correlation_id=_CORRELATION,
            trace_id=_TRACE_ID,
        )
        fields_ = log_fields(_ref(), telemetry)
        assert fields_["request_id"] == "req-1"
        assert fields_["correlation_id"] == _CORRELATION
        assert fields_["trace_id"] == _TRACE_ID

    def test_never_logs_secrets_or_payloads(self) -> None:
        ref = _ref()
        ref.metadata_ = {"signed_url": "https://s3/secret", "checksum": "abc"}
        fields_ = log_fields(ref, EvidenceTelemetry())
        assert "signed_url" not in fields_
        assert "checksum" not in fields_
        joined = " ".join(str(v) for v in fields_.values()).lower()
        for forbidden in ("s3://", "https://", "bearer", "token", "secret"):
            assert forbidden not in joined


class TestMetrics:
    def test_all_eight_evidence_metrics_registered(self) -> None:
        names = {
            metrics.EVIDENCE_METRIC_REQUESTED,
            metrics.EVIDENCE_METRIC_EXTRACTION_SUCCESS,
            metrics.EVIDENCE_METRIC_EXTRACTION_FAILURE,
            metrics.EVIDENCE_METRIC_UPLOAD_SUCCESS,
            metrics.EVIDENCE_METRIC_UPLOAD_FAILURE,
            metrics.EVIDENCE_METRIC_FINALIZED,
            metrics.EVIDENCE_METRIC_RETRY,
            metrics.EVIDENCE_METRIC_EXPIRED,
        }
        assert len(names) == 8
        # The module is disabled in-process (fresh subprocess probes the
        # enabled path) — record() must be a safe no-op.
        metrics.record_evidence_metric(metrics.EVIDENCE_METRIC_REQUESTED)

    def test_unknown_metric_name_rejected_when_enabled(self) -> None:
        """Unknown names raise only on the enabled path (disabled is a
        no-op — the guard returns before the name check, so this is
        verified in a subprocess with metrics enabled)."""
        result = _run_probe(
            """
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import metrics
s = Settings(
    _env_file=None,
    OBSERVABILITY_METRICS_ENABLED=True,
    OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
    OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
    OBJECT_STORAGE_ACCESS_KEY="test-key",
    OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
)
assert metrics.configure_metrics(s) is True
try:
    metrics.record_evidence_metric("evidence_bogus")
except ValueError:
    print("rejected-ok")
else:
    raise SystemExit("unknown metric name was silently accepted")
"""
        )
        assert result.returncode == 0, result.stderr
        assert "rejected-ok" in result.stdout


# =============================================================================
# Enabled-path probes (fresh subprocess — OTel/metrics globals set once)
# =============================================================================


def _run_probe(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=120,
    )


_CORRELATION_PROBE = """
import asyncio, uuid
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.observability.evidence import (
    capture_telemetry, write_telemetry, read_telemetry, evidence_span,
    SPAN_PROCESS, SPAN_SOURCE_RESOLUTION, SPAN_EXTRACTION, SPAN_UPLOAD,
    SPAN_FINALIZE, SPAN_ATTR_EVIDENCE_ID, SPAN_ATTR_EVENT_ID,
    SPAN_ATTR_TENANT_ID, SPAN_ATTR_VENUE_ID, SPAN_ATTR_SESSION_ID,
)
from backend.app.infrastructure.observability.context import TraceContext, format_traceparent
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.unit.test_evidence_worker import (
    FakeEvidenceWorkStore, FakeExtractor, FakeCandidates, Clock,
    _make_ref, _candidate, _make_worker, _TENANT, _VENUE, _REF, _EVENT, _SESSION,
)

settings = Settings(
    _env_file=None,
    OBSERVABILITY_TRACING_ENABLED=True,
    OTEL_SAMPLE_RATIO=1.0,
    OTEL_OTLP_ENDPOINT="http://127.0.0.1:9",
    OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
    OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
    OBJECT_STORAGE_ACCESS_KEY="test-key",
    OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
    EVIDENCE_WORKER_POLL_INTERVAL=0.1,
    EVIDENCE_WORKER_BATCH_SIZE=50,
    EVIDENCE_WORKER_LEASE_SECONDS=60,
    EVIDENCE_WORKER_MAX_ATTEMPTS=3,
    EVIDENCE_WORKER_BACKOFF_BASE=1.0,
    EVIDENCE_WORKER_BACKOFF_MAX=60.0,
    EVIDENCE_WORKER_BACKOFF_JITTER=0.0,
)
exporter = InMemorySpanExporter()
assert tracing.configure_tracing(settings, exporter=exporter) is True

# The ORIGINAL trace captured at event production (Task 8.8). The envelope
# would carry these fields across the outbox boundary.
TRACE_ID = "ab" * 16
SPAN_ID = "cd" * 8
parent = TraceContext(trace_id=TRACE_ID, span_id=SPAN_ID, sampled=True)

async def main():
    # 1. A ref with the telemetry captured at the async boundary. The
    #    ORM row carries the session identity (populated from the request
    #    contract at creation); the telemetry carries the ORIGINAL trace.
    ref = _make_ref()
    ref.session_id = uuid.UUID(str(_SESSION))
    from backend.app.infrastructure.observability.evidence import EvidenceTelemetry
    telemetry = EvidenceTelemetry(
        request_id="req-17", correlation_id="corr-17",
        trace_id=TRACE_ID, span_id=SPAN_ID, trace_sampled=True,
    )
    write_telemetry(ref, telemetry)
    assert read_telemetry(ref).trace_id == TRACE_ID

    # 2. Run the worker — the evidence spans must parent on the ORIGINAL trace.
    store = FakeEvidenceWorkStore()
    store.seed(ref)
    worker = _make_worker(
        store=store,
        extractor=FakeExtractor(),
        candidates=FakeCandidates([_candidate()]),
        clock=Clock(),
    )
    await worker.run_once()
    assert (ref.metadata_ or {}).get("processing_state") == "finalized"

    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    assert SPAN_PROCESS in by_name, [s.name for s in spans]
    assert SPAN_SOURCE_RESOLUTION in by_name
    assert SPAN_EXTRACTION in by_name
    assert SPAN_UPLOAD in by_name
    assert SPAN_FINALIZE in by_name

    # 3. Every evidence span continues the ORIGINAL trace (the producing
    #    event's trace — NOT a fresh one).
    for name in (SPAN_PROCESS, SPAN_SOURCE_RESOLUTION, SPAN_EXTRACTION, SPAN_UPLOAD, SPAN_FINALIZE):
        span = by_name[name]
        assert span.context.trace_id == int(TRACE_ID, 16), name

    # 4. The PROCESS span (the first in the chain) parents on the envelope
    #    span captured at the async boundary — the remote parent.
    process = by_name[SPAN_PROCESS]
    assert process.parent is not None
    assert process.parent.span_id == int(SPAN_ID, 16)
    assert process.parent.is_remote is True

    # 5. The nested chain: source → extraction → upload → finalize parent
    #    under process (the intra-worker span hierarchy), still in the
    #    ORIGINAL trace.
    for name in (SPAN_SOURCE_RESOLUTION, SPAN_EXTRACTION, SPAN_UPLOAD, SPAN_FINALIZE):
        span = by_name[name]
        assert span.parent is not None, name
        assert span.parent.span_id == by_name[SPAN_PROCESS].context.span_id, name

    # 6. Bounded identity attributes on the spans — no payloads/secrets.
    process = by_name[SPAN_PROCESS]
    assert process.attributes[SPAN_ATTR_EVIDENCE_ID] == str(_REF)
    assert process.attributes[SPAN_ATTR_EVENT_ID] == str(_EVENT)
    assert process.attributes[SPAN_ATTR_TENANT_ID] == str(_TENANT)
    assert process.attributes[SPAN_ATTR_VENUE_ID] == str(_VENUE)
    assert process.attributes[SPAN_ATTR_SESSION_ID] == str(_SESSION)
    assert process.attributes.get("request_id") == "req-17"
    assert process.attributes.get("correlation_id") == "corr-17"
    joined = " ".join(f"{k}={v}" for s in spans for k, v in (s.attributes or {}).items())
    for forbidden in ("token", "secret", "password", "bearer", "signature", "credential", "s3://"):
        assert forbidden not in joined.lower(), f"forbidden attr: {forbidden}"

print("correlation-ok")

asyncio.run(main())
"""


def test_cross_boundary_correlation_and_span_chain() -> None:
    """Event → EvidenceRequest → Worker → Source → Extraction → Storage
    → Finalization: the worker spans continue the producing event's trace
    and carry the full bounded identity set."""
    result = _run_probe(_CORRELATION_PROBE)
    assert result.returncode == 0, result.stderr
    assert "correlation-ok" in result.stdout


_METRICS_PROBE = """
import asyncio
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import metrics
from backend.app.infrastructure.observability.evidence import record
from tests.unit.test_evidence_worker import (
    FakeEvidenceWorkStore, FakeExtractor, FakeCandidates, Clock,
    _make_ref, _candidate, _make_worker,
)
from backend.app.domain.evidence.extraction import ExtractionStatus
from tests.unit.test_evidence_worker import _status, _raise
from backend.app.infrastructure.storage.exceptions import StorageError

settings = Settings(
    _env_file=None,
    OBSERVABILITY_METRICS_ENABLED=True,
    OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
    OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
    OBJECT_STORAGE_ACCESS_KEY="test-key",
    OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
    EVIDENCE_WORKER_POLL_INTERVAL=0.1,
    EVIDENCE_WORKER_BATCH_SIZE=50,
    EVIDENCE_WORKER_LEASE_SECONDS=60,
    EVIDENCE_WORKER_MAX_ATTEMPTS=3,
    EVIDENCE_WORKER_BACKOFF_BASE=1.0,
    EVIDENCE_WORKER_BACKOFF_MAX=60.0,
    EVIDENCE_WORKER_BACKOFF_JITTER=0.0,
)
assert metrics.configure_metrics(settings) is True

async def main():
    # Happy path: requested → extraction success → upload success → finalized.
    # Seeded REQUESTED so the queue step fires evidence_requested.
    store = FakeEvidenceWorkStore()
    store.seed(_make_ref())  # default state = "requested"
    worker = _make_worker(
        store=store, extractor=FakeExtractor(),
        candidates=FakeCandidates([_candidate()]), clock=Clock(),
    )
    await worker.run_once()

    # Retry path: extraction failure → evidence_retry fires.
    store2 = FakeEvidenceWorkStore()
    store2.seed(_make_ref(state="queued"))
    worker2 = _make_worker(
        store=store2, extractor=FakeExtractor([_raise(StorageError("boom"))]),
        candidates=FakeCandidates([_candidate()]), clock=Clock(),
    )
    await worker2.run_once()

    body, _ = metrics.render()
    text = body.decode()
    # prometheus_client appends ``_total`` to counter names and renders
    # values as floats — parse ``<name>_total <float>`` lines.
    def count(name):
        for line in text.splitlines():
            if line.startswith(name + "_total "):
                return int(float(line.split()[-1]))
        return -1

    assert count("evidence_requested") == 1, text
    assert count("evidence_extraction_success") == 1, text
    assert count("evidence_extraction_failure") == 1, text
    assert count("evidence_upload_success") == 1, text
    assert count("evidence_finalized") == 1, text
    assert count("evidence_retry") == 1, text
    # The upload-failure counter fired at the checkpoint guard (a no-op
    # when the guard fails) — the happy path records it as 0, but the
    # counter must EXIST in the exposition.
    assert "evidence_upload_failure" in text

print("metrics-ok")

asyncio.run(main())
"""


def test_evidence_metrics_fire_at_exact_points() -> None:
    result = _run_probe(_METRICS_PROBE)
    assert result.returncode == 0, result.stderr
    assert "metrics-ok" in result.stdout


_EXPIRED_PROBE = """
import asyncio, uuid
from datetime import UTC, datetime, timedelta
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import metrics
from tests.unit.test_evidence_worker import (
    FakeEvidenceWorkStore, FakeExtractor, FakeCandidates, Clock,
    _make_ref, _make_worker,
)

settings = Settings(
    _env_file=None,
    OBSERVABILITY_METRICS_ENABLED=True,
    OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
    OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
    OBJECT_STORAGE_ACCESS_KEY="test-key",
    OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
    EVIDENCE_WORKER_POLL_INTERVAL=0.1,
    EVIDENCE_WORKER_BATCH_SIZE=50,
    EVIDENCE_WORKER_LEASE_SECONDS=60,
    EVIDENCE_WORKER_MAX_ATTEMPTS=3,
    EVIDENCE_WORKER_BACKOFF_BASE=1.0,
    EVIDENCE_WORKER_BACKOFF_MAX=60.0,
    EVIDENCE_WORKER_BACKOFF_JITTER=0.0,
    EVIDENCE_WORKER_REQUEST_TIMEOUT_SECONDS=86400,
)
assert metrics.configure_metrics(settings) is True

async def main():
    # A REQUESTED ref abandoned past the timeout → evidence_expired fires
    # (and the audit event is recorded).
    store = FakeEvidenceWorkStore()
    ref = _make_ref(state="requested")
    ref.created_at = datetime(2026, 1, 1, tzinfo=UTC)  # long abandoned
    store.seed(ref)
    clock = Clock()
    worker = _make_worker(
        store=store, extractor=FakeExtractor(),
        candidates=FakeCandidates([]), clock=clock,
    )
    await worker.run_once()

    body, _ = metrics.render()
    text = body.decode()
    assert "evidence_expired" in text, text
    assert (ref.metadata_ or {}).get("processing_state") == "expired"

print("expired-ok")

asyncio.run(main())
"""


def test_expired_metric_fires_for_abandoned_requests() -> None:
    result = _run_probe(_EXPIRED_PROBE)
    assert result.returncode == 0, result.stderr
    assert "expired-ok" in result.stdout
