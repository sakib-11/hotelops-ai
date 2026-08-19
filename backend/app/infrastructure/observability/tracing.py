"""OpenTelemetry tracing (Task 8.7).

One tracing configuration, owned by this module, wired through the W3C
trace context already parsed by the Task 8.4 middleware:

  - :func:`configure_tracing` is OPT-IN: with
    ``OBSERVABILITY_TRACING_ENABLED=false`` (the default) nothing is
    configured, the OpenTelemetry API stays its safe no-op, and the
    exporter packages are never even imported — the application starts
    and runs with no external collector (requirement 8).
  - When enabled, a ``TracerProvider`` is built with a
    ``ParentBased(TraceIdRatioBased(ratio))`` sampler and an
    OTLP/HTTP exporter pointed at ``OTEL_OTLP_ENDPOINT``. The exporter
    only contacts the endpoint when spans are actually exported, so
    startup never depends on a collector being reachable.
  - The FastAPI request boundary is instrumented by
    :func:`http_request_span`, which parents the server span on the
    inbound ``traceparent`` (so existing traces continue) and yields the
    real server span so the response ``traceparent`` reflects the
    server's own span id (closing the 8.4 note that this was a
    correlation-only layer).
  - Database/Redis/event operations attach only bounded,
    low-cardinality attributes (``db.system``, ``db.operation``, and
    event/job/session identifiers as UUIDs). Credentials, tokens and
    payload bodies are NEVER added to spans (requirement 9), and no
    uncontrolled high-cardinality attributes are emitted (requirement
    10; request paths are truncated to a fixed bound).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.context import attach as context_attach
from opentelemetry.context import detach as context_detach
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)

from backend.app.infrastructure.observability.context import TraceContext, format_traceparent

if TYPE_CHECKING:
    from backend.app.infrastructure.config import Settings

logger = logging.getLogger(__name__)

_TRACER_NAME = "hotelops-ai"
# Request paths are bounded so a hostile/parametrized path cannot grow
# span cardinality without limit (requirement 10).
_MAX_PATH_LENGTH = 128

_DB_SYSTEM_POSTGRESQL = "postgresql"
_DB_SYSTEM_REDIS = "redis"

# Module state — the single tracing configuration for this process.
_enabled = False
_provider: Any = None


# =========================================================================
# Configuration (opt-in; safe defaults)
# =========================================================================


def configure_tracing(settings: Settings, *, exporter: Any | None = None) -> bool:
    """Configure (or leave untouched) the OpenTelemetry tracing SDK.

    The OTel API allows the global TracerProvider to be installed only
    ONCE per process (later assignments are silently ignored), so this
    function is designed for exactly-once configuration at startup:

      - A second call while already configured is an idempotent no-op
        returning the current enabled state.
      - After :func:`shutdown_tracing` the process stays disabled: a
        later call returns False and never installs a broken
        "enabled-but-dead" provider (the OTel global cannot be
        replaced). Callers should configure once at startup and shut
        down at exit (the ``enabled()`` guard keeps all span helpers
        no-ops while shut down).

    Args:
        settings: Application settings — tracing is enabled only when
            ``settings.observability_tracing_enabled`` is true.
        exporter: Optional span exporter override (tests inject an
            ``InMemorySpanExporter``). Defaults to the OTLP/HTTP
            exporter — which is imported lazily here and never
            contacted during configuration.

    Returns:
        True when tracing is enabled after this call.
    """
    global _provider, _enabled
    if _provider is not None:
        # Already configured — do not reconfigure the global provider.
        return _enabled

    if not settings.observability_tracing_enabled:
        _enabled = False
        return False

    # SDK imports are lazy: while disabled these packages are never
    # imported, and the exporter below is only constructed (not
    # contacted) here.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource = Resource.create(resource_attributes(settings))
    # ParentBased: honor an inbound sampled traceparent; otherwise apply
    # the configured sampling ratio. TraceIdRatioBased keeps the choice
    # deterministic per trace id.
    sampler = ParentBased(TraceIdRatioBased(settings.otel_sample_ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)
    if exporter is None:
        # Batch: export asynchronously, flushed by shutdown_tracing().
        processor: Any = BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint))
    else:
        # Tests inject an exporter — export synchronously so finished
        # spans are observable immediately.
        processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    _provider = provider
    _enabled = True
    logger.info(
        "OpenTelemetry tracing enabled (service=%s, endpoint=%s, sample_ratio=%.2f)",
        settings.otel_service_name,
        settings.otel_otlp_endpoint,
        settings.otel_sample_ratio,
    )
    return True


def shutdown_tracing() -> None:
    """Flush and shut down the tracing SDK.

    Safe to call when tracing was never enabled. After shutdown the
    module is disabled (``enabled()`` is False and all span helpers are
    no-ops) for the rest of the process — the OTel API allows the
    global provider to be installed only once per process, so a later
    :func:`configure_tracing` honestly returns False instead of
    installing a provider that could never export. Repeated shutdown
    calls are safe (the SDK shuts down once).
    """
    global _enabled
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            logger.exception("OpenTelemetry provider shutdown failed")
    _enabled = False


def resource_attributes(settings: Settings) -> dict[str, Any]:
    """OTel resource attributes for the service (Task 8.10).

    Single source for the service/build metadata attached to every
    exported span: service identity, environment, version, and build
    info when available. Values are plain strings only — never
    credentials. Used by :func:`configure_tracing` so the production
    path and tests exercise the same construction.
    """
    from opentelemetry.sdk.resources import (
        DEPLOYMENT_ENVIRONMENT,
        SERVICE_NAME,
        SERVICE_VERSION,
    )

    resource_attrs: dict[str, Any] = {
        SERVICE_NAME: settings.otel_service_name,
        SERVICE_VERSION: settings.app_version,
        DEPLOYMENT_ENVIRONMENT: settings.app_env,
    }
    if settings.build_commit:
        resource_attrs["build.commit"] = settings.build_commit
    if settings.build_timestamp:
        resource_attrs["build.timestamp"] = settings.build_timestamp
    return resource_attrs


def enabled() -> bool:
    """True when the tracing SDK is configured and recording."""
    return _enabled


def tracer() -> trace.Tracer:
    """The application tracer (no-op API tracer when disabled)."""
    return trace.get_tracer(_TRACER_NAME)


# =========================================================================
# Trace context helpers
# =========================================================================


def span_context_from_trace_context(tc: TraceContext | None) -> Any | None:
    """Build an OTel parent ``Context`` from a W3C ``TraceContext``.

    Lets the request span continue an inbound trace: the parsed
    ``traceparent`` becomes the remote parent. ``None`` in, ``None``
    out (a fresh trace is then generated by the SDK).
    """
    if tc is None:
        return None
    span_context = SpanContext(
        trace_id=int(tc.trace_id, 16),
        span_id=int(tc.span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(0x01 if tc.sampled else 0x00),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


def traceparent_from_span(span: Span | None) -> str | None:
    """The W3C ``traceparent`` value of a live server span.

    Returns None when there is no recording span — the caller then
    falls back to the correlation trace context (disabled mode).
    """
    if span is None or not span.is_recording():
        return None
    sc = span.get_span_context()
    return format_traceparent(
        TraceContext(
            trace_id=f"{sc.trace_id:032x}",
            span_id=f"{sc.span_id:016x}",
            sampled=bool(sc.trace_flags.sampled),
        )
    )


def trace_context_from_event_attrs(
    trace_id: str | None,
    span_id: str | None,
    trace_sampled: bool | None,
) -> TraceContext | None:
    """Build a TraceContext from the optional trace fields on an event.

    Returns ``None`` when the fields are incomplete or absent — the
    caller then starts a fresh trace rather than continuing a broken
    one (requirement 10: missing telemetry context never breaks event
    processing).
    """
    if not trace_id or not span_id:
        return None
    try:
        # Validate hex format; accept only 32-char trace_id and
        # 16-char span_id (W3C standard lengths).
        if len(trace_id) != 32 or len(span_id) != 16:
            return None
        int(trace_id, 16)
        int(span_id, 16)
    except ValueError, TypeError:
        return None
    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        # When the flag is absent but the trace identity is present,
        # default to sampled so the trace continues being recorded.
        sampled=bool(trace_sampled) if trace_sampled is not None else True,
    )


def set_current_span_attributes(**attributes: Any) -> None:
    """Attach bounded attributes to the current span, if any.

    A safe no-op when tracing is disabled or no span is active. None
    values are skipped so callers can pass optional fields directly.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    attrs = {key: value for key, value in attributes.items() if value is not None}
    if attrs:
        span.set_attributes(attrs)


def record_response_status(span: Span | None, status_code: int) -> None:
    """Record the response status code; mark the span ERROR on 5xx only.

    Expected 4xx client errors (e.g. auth failures) are recorded but NOT
    marked as span errors — they are not infrastructure failures.
    """
    if span is None or not span.is_recording():
        return
    span.set_attribute("http.response.status_code", int(status_code))
    if int(status_code) >= 500:
        span.set_status(Status(StatusCode.ERROR))


def record_exception(span: Span | None, exc: BaseException) -> None:
    """Record a genuine failure on the span (exception event + ERROR).

    Note: the exception message is forwarded verbatim into the span's
    exception event (standard OTel behavior, matching the logging
    module's documented caller-responsibility contract) — application
    code must keep credentials out of exception messages.
    """
    if span is None or not span.is_recording():
        return
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR))


# =========================================================================
# Spans
# =========================================================================


@asynccontextmanager
async def http_request_span(
    scope: Mapping[str, Any],
    *,
    request_id: str,
    correlation_id: str,
    parent_trace: TraceContext,
) -> AsyncIterator[Span | None]:
    """The server span for one HTTP request (no-op when disabled).

    The span is made current for the whole request so database/Redis
    spans created while handling the request become its children, and
    it is detached/ended when the request finishes. The yielded span
    (or None when disabled) drives the response ``traceparent`` and
    status/exception recording in the middleware.
    """
    if not _enabled:
        yield None
        return
    method = str(scope.get("method") or "UNKNOWN")
    path = str(scope.get("path") or "")[:_MAX_PATH_LENGTH]
    span = tracer().start_span(
        f"HTTP {method}",
        context=span_context_from_trace_context(parent_trace),
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": method,
            "url.path": path,
            "request_id": request_id,
            "correlation_id": correlation_id,
        },
    )
    token = context_attach(set_span_in_context(span))
    try:
        yield span
    finally:
        context_detach(token)
        span.end()


@asynccontextmanager
async def _span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
    parent_trace: TraceContext | None = None,
) -> AsyncIterator[Span | None]:
    """A child span of the current span (no-op when disabled).

    None-valued attributes are dropped before setting. When
    ``parent_trace`` is provided, the span is parented on the
    given trace context rather than the current span — this lets
    workers continue a trace that was captured at enqueue time.
    """
    if not _enabled:
        yield None
        return
    attrs = {key: value for key, value in (attributes or {}).items() if value is not None}
    context = span_context_from_trace_context(parent_trace) if parent_trace else None
    if context:
        with tracer().start_as_current_span(
            name, context=context, kind=kind, attributes=attrs
        ) as span:
            yield span
    else:
        with tracer().start_as_current_span(name, kind=kind, attributes=attrs) as span:
            yield span


@asynccontextmanager
async def db_span(operation: str) -> AsyncIterator[Span | None]:
    """A PostgreSQL operation span (db.system=postgresql)."""
    async with _span(
        operation,
        attributes={"db.system": _DB_SYSTEM_POSTGRESQL, "db.operation": operation},
    ) as span:
        yield span


@asynccontextmanager
async def redis_span(
    operation: str,
    *,
    event_id: str | None = None,
    event_type: str | None = None,
    tenant_id: str | None = None,
    venue_id: str | None = None,
) -> AsyncIterator[Span | None]:
    """A Redis operation span (db.system=redis) with bounded event context."""
    async with _span(
        operation,
        attributes={
            "db.system": _DB_SYSTEM_REDIS,
            "db.operation": operation,
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "venue_id": venue_id,
        },
    ) as span:
        yield span


@asynccontextmanager
async def event_span(
    name: str,
    *,
    event_id: str | None = None,
    event_type: str | None = None,
    tenant_id: str | None = None,
    venue_id: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    parent_trace: TraceContext | None = None,
) -> AsyncIterator[Span | None]:
    """A business-operation span carrying event/job/session identifiers.

    ``parent_trace`` continues a trace captured at event-production time
    (Task 8.8): the worker span becomes a child of the original span
    instead of a root of a new trace.
    """
    async with _span(
        name,
        attributes={
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "venue_id": venue_id,
            "job_id": job_id,
            "session_id": session_id,
            "correlation_id": correlation_id,
        },
        parent_trace=parent_trace,
    ) as span:
        yield span


__all__ = [
    "configure_tracing",
    "db_span",
    "enabled",
    "event_span",
    "http_request_span",
    "record_exception",
    "record_response_status",
    "redis_span",
    "resource_attributes",
    "set_current_span_attributes",
    "shutdown_tracing",
    "span_context_from_trace_context",
    "trace_context_from_event_attrs",
    "traceparent_from_span",
    "tracer",
]
