"""Evidence observability (Task 17.12).

One module owns EVERY evidence telemetry concern — spans, metrics, and
structured log fields — so the evidence layer never scatters observability:

- TELEMETRY CARRIER: every evidence request carries the bounded identity
  set (request_id, correlation_id, trace_id, tenant_id, venue_id,
  event_id, evidence_id, session_id). The trace/correlation identity is
  captured at the async boundary (the producing ``EventEnvelope``, Task
  8.8) and persisted on the evidence ref's JSONB metadata; the worker
  reads it and parents its spans on the ORIGINAL trace — the async
  boundary is correlated end-to-end:

      Event → EvidenceRequest → Worker → Source → Extraction
            → Storage → Finalization

- SPANS: ``evidence_span`` attaches the full bounded identifier set as
  span attributes (never secrets, never payloads). When the telemetry
  carries a trace (from the producing event), the span continues that
  trace instead of starting a fresh one.

- METRICS: the eight canonical evidence counters (see ``metrics``) are
  recorded at their exact firing points through ``record``.

- LOG FIELDS: ``log_fields`` returns the allowlisted identifiers for
  ``logger.info(..., extra=...)`` — the project's JSON formatter emits
  structured JSON with redaction (Task 8.9); only the allowlisted
  ``_CONTEXT_FIELDS`` are serialized.

SECURITY: only bounded identifiers ever leave this module. Tokens,
credentials, signed URLs, raw video, and payloads are never logged, set
as span attributes, or persisted on the telemetry carrier.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from typing import Any

from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.observability import context, metrics, tracing
from backend.app.infrastructure.observability.context import TraceContext
from contracts.events import EventEnvelope

# The JSONB metadata key on an evidence ref that carries the telemetry
# identity captured when the request crossed the async boundary.
EVIDENCE_TELEMETRY_KEY = "_observability"

# Bounded span attribute names (the ONLY span attributes evidence spans
# may carry — never payloads or secrets).
SPAN_ATTR_REQUEST_ID = "request_id"
SPAN_ATTR_CORRELATION_ID = "correlation_id"
SPAN_ATTR_TRACE_ID = "trace_id"
SPAN_ATTR_EVENT_ID = "event_id"
SPAN_ATTR_EVIDENCE_ID = "evidence_id"
SPAN_ATTR_TENANT_ID = "tenant_id"
SPAN_ATTR_VENUE_ID = "venue_id"
SPAN_ATTR_SESSION_ID = "session_id"

# Canonical evidence pipeline span names (the trace chain).
SPAN_PROCESS = "evidence.process"
SPAN_SOURCE_RESOLUTION = "evidence.source_resolution"
SPAN_EXTRACTION = "evidence.extraction"
SPAN_UPLOAD = "evidence.upload"
SPAN_FINALIZE = "evidence.finalize"


@dataclass(frozen=True)
class EvidenceTelemetry:
    """The bounded observability identity of one evidence request.

    ``request_id``/``correlation_id``/``trace_id``/``span_id`` are the
    transport-level correlation identity; the evidence identifiers
    (tenant/venue/event/evidence/session) are derived from the ref itself
    at span/log time. Absence of a field is the safe representation —
    never a guessed value.
    """

    request_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    trace_sampled: bool | None = None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def parent_trace(self) -> TraceContext | None:
        """The W3C trace to continue (None = start a fresh trace).

        Derived from the trace identity captured at the async boundary —
        the evidence spans become children of the producing event's
        trace, correlating the full Event → ... → Finalization chain.
        """
        return tracing.trace_context_from_event_attrs(
            self.trace_id,
            self.span_id,
            self.trace_sampled,
        )


def capture_telemetry(*, envelope: EventEnvelope[Any] | None = None) -> EvidenceTelemetry:
    """Capture the evidence telemetry at the async boundary.

    The envelope is authoritative for the TRACE identity (Task 8.8 — the
    trace captured at event production survives the outbox → worker
    boundary); the active request context supplies request_id when
    present. Missing telemetry never breaks processing — it simply means
    a fresh trace starts downstream (Task 8 requirement 10).
    """
    active = context.get_request_context()
    return EvidenceTelemetry(
        request_id=context.request_id(),
        correlation_id=(
            envelope.correlation_id
            if envelope is not None and envelope.correlation_id
            else active.get("correlation_id")
        ),
        trace_id=(
            envelope.trace_id
            if envelope is not None and envelope.trace_id
            else active.get("trace_id")
        ),
        span_id=(
            envelope.span_id if envelope is not None and envelope.span_id else active.get("span_id")
        ),
        trace_sampled=envelope.trace_sampled if envelope is not None else None,
    )


# =============================================================================
# Durable carrier (JSONB policy — the ref's variable metadata)
# =============================================================================


def read_telemetry(ref: EvidenceRefModel) -> EvidenceTelemetry | None:
    """The telemetry persisted on the ref (None = none was captured)."""
    raw = (ref.metadata_ or {}).get(EVIDENCE_TELEMETRY_KEY)
    if not isinstance(raw, dict):
        return None
    known = {field.name for field in fields(EvidenceTelemetry)}
    return EvidenceTelemetry(**{key: value for key, value in raw.items() if key in known})


def write_telemetry(ref: EvidenceRefModel, telemetry: EvidenceTelemetry) -> None:
    """Persist the telemetry identity onto the ref's metadata (in memory).

    The caller's transaction persists it with the state change. Only the
    bounded identity is stored — never payloads or secrets.
    """
    metadata = dict(ref.metadata_ or {})
    metadata[EVIDENCE_TELEMETRY_KEY] = {
        key: value for key, value in telemetry.to_dict().items() if value is not None
    }
    ref.metadata_ = metadata


# =============================================================================
# Spans (the trace chain)
# =============================================================================


@asynccontextmanager
async def evidence_span(
    name: str,
    *,
    ref: EvidenceRefModel,
    telemetry: EvidenceTelemetry | None = None,
    parent_trace: TraceContext | None = None,
) -> AsyncIterator[Any]:
    """A business span carrying the full bounded evidence identity.

    ``parent_trace`` (from ``telemetry.parent_trace``) continues the
    producing event's trace across the async boundary; nested spans
    parent on the current span (Source → Extraction → Storage →
    Finalization). A safe no-op when tracing is disabled.
    """
    async with tracing.event_span(
        name,
        event_id=str(ref.event_id) if ref.event_id else None,
        event_type="evidence",
        tenant_id=str(ref.tenant_id),
        venue_id=str(ref.venue_id),
        session_id=str(ref.session_id) if ref.session_id else None,
        correlation_id=telemetry.correlation_id if telemetry else None,
        parent_trace=parent_trace,
    ) as span:
        tracing.set_current_span_attributes(
            **span_attributes(ref, telemetry),
        )
        yield span


def span_attributes(ref: EvidenceRefModel, telemetry: EvidenceTelemetry | None) -> dict[str, Any]:
    """The bounded evidence span attributes (never payloads or secrets)."""
    attrs: dict[str, Any] = {
        SPAN_ATTR_EVIDENCE_ID: str(ref.ref_id),
    }
    if telemetry is not None:
        if telemetry.request_id:
            attrs[SPAN_ATTR_REQUEST_ID] = telemetry.request_id
        if telemetry.correlation_id:
            attrs[SPAN_ATTR_CORRELATION_ID] = telemetry.correlation_id
        if telemetry.trace_id:
            attrs[SPAN_ATTR_TRACE_ID] = telemetry.trace_id
    return attrs


# =============================================================================
# Metrics
# =============================================================================


def record(name: str) -> None:
    """Record one evidence pipeline counter (no-op while metrics disabled)."""
    metrics.record_evidence_metric(name)


# =============================================================================
# Structured log fields (JSON only, allowlisted + redacted by Task 8.9)
# =============================================================================


def log_fields(ref: EvidenceRefModel, telemetry: EvidenceTelemetry | None = None) -> dict[str, str]:
    """The allowlisted identifier fields for ``logger.info(..., extra=...)``.

    Every key is in the JSON formatter's ``_CONTEXT_FIELDS`` allowlist
    (and passes the redaction filter) — the message/fields are emitted as
    one structured JSON object per line, and nothing outside the allowlist
    is serialized.
    """
    fields_: dict[str, str] = {}
    if telemetry is not None:
        if telemetry.request_id:
            fields_["request_id"] = telemetry.request_id
        if telemetry.correlation_id:
            fields_["correlation_id"] = telemetry.correlation_id
        if telemetry.trace_id:
            fields_["trace_id"] = telemetry.trace_id
    if ref.event_id:
        fields_["event_id"] = str(ref.event_id)
    fields_["evidence_id"] = str(ref.ref_id)
    fields_["tenant_id"] = str(ref.tenant_id)
    fields_["venue_id"] = str(ref.venue_id)
    if ref.session_id:
        fields_["session_id"] = str(ref.session_id)
    return fields_


__all__ = [
    "EVIDENCE_TELEMETRY_KEY",
    "SPAN_ATTR_CORRELATION_ID",
    "SPAN_ATTR_EVENT_ID",
    "SPAN_ATTR_EVIDENCE_ID",
    "SPAN_ATTR_REQUEST_ID",
    "SPAN_ATTR_SESSION_ID",
    "SPAN_ATTR_TENANT_ID",
    "SPAN_ATTR_TRACE_ID",
    "SPAN_ATTR_VENUE_ID",
    "SPAN_EXTRACTION",
    "SPAN_FINALIZE",
    "SPAN_PROCESS",
    "SPAN_SOURCE_RESOLUTION",
    "SPAN_UPLOAD",
    "EvidenceTelemetry",
    "capture_telemetry",
    "evidence_span",
    "log_fields",
    "read_telemetry",
    "record",
    "span_attributes",
    "write_telemetry",
]
