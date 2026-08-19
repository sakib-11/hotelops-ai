"""Task 8.7 — OpenTelemetry tracing tests.

Covers configuration behavior (opt-in, safe defaults, no collector
dependency, no exporter import while disabled) and trace creation:
the FastAPI request boundary (server spans parented on the inbound W3C
traceparent, response traceparent produced by the server span),
actor/tenant span attributes from the server-validated AuthContext, and
the database/Redis/event span helpers (bounded attributes only).

Tracing is enabled module-wide with sample_ratio=1.0 and an in-memory
exporter so spans are deterministic; "disabled" behavior is verified in
fresh subprocesses (the OpenTelemetry global provider is set only once
per process, so a disabled configuration must be observed in a process
that never enabled it).
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from backend.app.infrastructure.auth.deps import get_actor_context
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.observability.context import TraceContext, format_traceparent
from backend.app.infrastructure.observability.middleware import RequestContextMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# B008: the repo allows the Depends() pattern only in app modules, so
# tests use a module-level dependency marker.
_actor_context_dep = Depends(get_actor_context)


def _settings(*, tracing_enabled: bool = True, sample_ratio: float = 1.0) -> object:
    from backend.app.infrastructure.config import Settings

    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        OBSERVABILITY_TRACING_ENABLED=tracing_enabled,
        OTEL_SAMPLE_RATIO=sample_ratio,
        OTEL_OTLP_ENDPOINT="http://127.0.0.1:9",  # never contacted in tests
    )


def _auth_settings() -> object:
    from backend.app.infrastructure.config import Settings

    return Settings(  # type: ignore[call-arg]
        _env_file=None, SECRET_KEY="t", JWT_ALGORITHM="HS256", JWT_EXPIRATION_MINUTES=60
    )


# =========================================================================
# Disabled semantics — verified in fresh subprocesses
# =========================================================================


def _run_disabled_script(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=60,
    )


_DISABLED_PROBE = """
import sys
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing
from opentelemetry import trace

s = Settings(_env_file=None)  # OBSERVABILITY_TRACING_ENABLED defaults to false
assert tracing.configure_tracing(s) is False
assert tracing.enabled() is False
# The exporter packages must never be imported while disabled.
assert "opentelemetry.exporter" not in sys.modules
# The API stays its safe no-op: spans are never recorded.
span = trace.get_tracer("probe").start_span("x")
assert span.is_recording() is False
span.end()
print("disabled-ok")
"""


def test_disabled_never_imports_exporter() -> None:
    result = _run_disabled_script(_DISABLED_PROBE)
    assert result.returncode == 0, result.stderr
    assert "disabled-ok" in result.stdout


_DISABLED_HELPERS_PROBE = """
import asyncio
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.observability.context import TraceContext

s = Settings(_env_file=None)
assert tracing.configure_tracing(s) is False

async def main():
    # Every span helper is a safe no-op yielding None.
    async with tracing.db_span("db.session") as span:
        assert span is None
    async with tracing.redis_span("redis.xadd", event_id="e1") as span:
        assert span is None
    async with tracing.event_span("inbox.process", event_id="e1") as span:
        assert span is None
    async with tracing.http_request_span(
        {"type": "http", "method": "GET", "path": "/"}, request_id="r", correlation_id="c",
        parent_trace=TraceContext(trace_id="a" * 32, span_id="b" * 16, sampled=True),
    ) as span:
        assert span is None
    # Attribute/status recording are no-ops too.
    tracing.set_current_span_attributes(tenant_id="t1")
    tracing.record_response_status(None, 500)
    tracing.record_exception(None, RuntimeError("x"))

asyncio.run(main())
print("helpers-ok")
"""


def test_span_helpers_noop_when_disabled() -> None:
    result = _run_disabled_script(_DISABLED_HELPERS_PROBE)
    assert result.returncode == 0, result.stderr
    assert "helpers-ok" in result.stdout


_SHUTDOWN_RECONFIGURE_PROBE = """
import asyncio
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

s = Settings(_env_file=None, OBSERVABILITY_TRACING_ENABLED=True, OTEL_SAMPLE_RATIO=1.0)
exp1 = InMemorySpanExporter()
assert tracing.configure_tracing(s, exporter=exp1) is True

async def main():
    async with tracing.event_span("first"):
        pass

asyncio.run(main())
assert len(exp1.get_finished_spans()) == 1

tracing.shutdown_tracing()
assert tracing.enabled() is False
# The OTel global provider is installed once per process — a later
# configure must NOT claim enabled with a provider that cannot export.
assert tracing.configure_tracing(s, exporter=InMemorySpanExporter()) is False
assert tracing.enabled() is False

async def main2():
    async with tracing.event_span("second"):
        pass

asyncio.run(main2())
print("shutdown-reconfigure-ok")
"""


def test_configure_after_shutdown_stays_disabled() -> None:
    """Re-configuring after shutdown returns False (never a dead provider)."""
    result = _run_disabled_script(_SHUTDOWN_RECONFIGURE_PROBE)
    assert result.returncode == 0, result.stderr
    assert "shutdown-reconfigure-ok" in result.stdout


# =========================================================================
# Enabled semantics — one provider per process, in-memory exporter
# =========================================================================


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def tracing_enabled(exporter: InMemorySpanExporter) -> None:
    assert tracing.configure_tracing(_settings(), exporter=exporter) is True
    yield
    tracing.shutdown_tracing()


@pytest.fixture(autouse=True)
def clear_spans(exporter: InMemorySpanExporter) -> None:
    exporter.clear()


class TestConfigureBehavior:
    def test_configure_is_idempotent(self, exporter: InMemorySpanExporter) -> None:
        assert tracing.enabled() is True
        # A second configure (different exporter) must be refused — the
        # global provider is configured once per process.
        assert tracing.configure_tracing(_settings(), exporter=InMemorySpanExporter()) is True


class TestTraceHelpers:
    def test_tracer_records_spans(self, exporter: InMemorySpanExporter) -> None:
        with tracing.tracer().start_as_current_span("manual"):
            tracing.set_current_span_attributes(tenant_id="t-1", actor_id="a-1", venue_id=None)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "manual"
        assert spans[0].attributes["tenant_id"] == "t-1"
        assert spans[0].attributes["actor_id"] == "a-1"
        assert "venue_id" not in spans[0].attributes  # None values dropped

    def test_resource_metadata_on_spans(self, exporter: InMemorySpanExporter) -> None:
        """Every span carries the service/environment/version resource."""
        with tracing.tracer().start_as_current_span("meta"):
            pass
        span = exporter.get_finished_spans()[0]
        resource = span.resource.attributes
        assert resource["service.name"] == "hotelops-ai"  # otel_service_name default
        assert resource["service.version"] == "0.1.0"
        assert resource["deployment.environment"] == "development"

    def test_span_context_from_trace_context_round_trip(self) -> None:
        tc = TraceContext(trace_id="ab" * 16, span_id="cd" * 8, sampled=True)
        context = tracing.span_context_from_trace_context(tc)
        assert context is not None
        # A span parented on this context continues the same trace under
        # the inbound (remote) span.
        with tracing.tracer().start_as_current_span("child", context=context) as span:
            assert span.get_span_context().trace_id == int("ab" * 16, 16)
            assert span.parent is not None
            assert span.parent.span_id == int("cd" * 8, 16)
        assert tracing.span_context_from_trace_context(None) is None

    def test_unsampled_parent_not_recorded(self) -> None:
        """ParentBased honors an inbound sampled=false traceparent."""
        tc = TraceContext(trace_id="12" * 16, span_id="34" * 8, sampled=False)
        context = tracing.span_context_from_trace_context(tc)
        span = tracing.tracer().start_span("dropped", context=context)
        assert span.is_recording() is False
        span.end()

    def test_traceparent_from_span(self) -> None:
        with tracing.tracer().start_as_current_span("tp") as span:
            value = tracing.traceparent_from_span(span)
        assert value is not None
        parts = value.split("-")
        assert len(parts) == 4 and parts[0] == "00"
        assert tracing.traceparent_from_span(None) is None

    def test_db_redis_event_span_helpers(self, exporter: InMemorySpanExporter) -> None:
        async def run() -> None:
            # Combined contexts enter left-to-right, so each span parents
            # under the one before it (event -> db -> redis).
            async with (
                tracing.event_span(
                    "inbox.process",
                    event_id=str(uuid.uuid4()),
                    event_type="room.cleaned",
                    tenant_id=str(uuid.uuid4()),
                ),
                tracing.db_span("db.session"),
                tracing.redis_span("redis.xadd", event_id="e-1"),
            ):
                pass

        import asyncio

        asyncio.run(run())
        spans = exporter.get_finished_spans()
        by_name = {span.name: span for span in spans}
        assert set(by_name) == {"inbox.process", "db.session", "redis.xadd"}
        assert by_name["db.session"].attributes["db.system"] == "postgresql"
        assert by_name["db.session"].attributes["db.operation"] == "db.session"
        assert by_name["redis.xadd"].attributes["db.system"] == "redis"
        assert by_name["redis.xadd"].attributes["event_id"] == "e-1"
        assert by_name["inbox.process"].attributes["event_type"] == "room.cleaned"
        # The event span is the parent of the db/redis spans.
        parent = by_name["db.session"].parent
        assert parent is not None
        assert parent.span_id == by_name["inbox.process"].context.span_id

    def test_trace_context_from_event_attrs(self) -> None:
        """Task 8.8: Reconstruct a TraceContext from envelope fields."""
        # Valid fields produce a proper TraceContext.
        tc = tracing.trace_context_from_event_attrs(
            trace_id="ab" * 16,
            span_id="cd" * 8,
            trace_sampled=True,
        )
        assert tc is not None
        assert tc.trace_id == "ab" * 16
        assert tc.span_id == "cd" * 8
        assert tc.sampled is True

        # Missing trace_id -> None (fresh trace).
        assert tracing.trace_context_from_event_attrs(None, "cd" * 8, True) is None
        # Missing span_id -> None.
        assert tracing.trace_context_from_event_attrs("ab" * 16, None, True) is None
        # Short trace_id -> None.
        assert tracing.trace_context_from_event_attrs("ab", "cd" * 8, True) is None
        # Short span_id -> None.
        assert tracing.trace_context_from_event_attrs("ab" * 16, "cd", True) is None
        # Non-hex characters -> None.
        assert tracing.trace_context_from_event_attrs("gg" * 16, "cd" * 8, True) is None
        # Missing trace_sampled defaults to True (continue recording).
        tc = tracing.trace_context_from_event_attrs("ab" * 16, "cd" * 8, None)
        assert tc is not None
        assert tc.sampled is True

    def test_event_span_with_parent_trace(self, exporter: InMemorySpanExporter) -> None:
        """Task 8.8: event_span with parent_trace continues the parent trace."""
        parent_tc = TraceContext(trace_id="12" * 16, span_id="34" * 8, sampled=True)

        async def run() -> None:
            async with tracing.event_span(
                "worker.effect",
                event_id="e-1",
                event_type="task.process",
                correlation_id="corr-1",
                parent_trace=parent_tc,
            ):
                pass

        import asyncio

        asyncio.run(run())
        spans = exporter.get_finished_spans()
        assert len(spans) >= 1
        span = spans[0]
        assert span.name == "worker.effect"
        # The span is in the parent's trace.
        assert span.context.trace_id == int("12" * 16, 16)
        # The parent is the NonRecordingSpan from the envelope fields.
        assert span.parent is not None
        assert span.parent.span_id == int("34" * 8, 16)
        assert span.parent.is_remote is True
        # Correlation id is recorded as an attribute.
        assert span.attributes.get("correlation_id") == "corr-1"

    def test_event_span_without_parent_starts_fresh(self, exporter: InMemorySpanExporter) -> None:
        """Missing parent_trace starts a fresh trace (no parent)."""

        async def run() -> None:
            async with tracing.event_span(
                "orphan",
                event_id="e-2",
                event_type="task.stray",
            ):
                pass

        import asyncio

        asyncio.run(run())
        spans = exporter.get_finished_spans()
        span = spans[0]
        assert span.name == "orphan"
        # No parent -> span is a root.
        assert span.parent is None


# =========================================================================
# FastAPI request boundary
# =========================================================================


def _make_app(*, auth: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    if auth:
        from backend.app.dependencies import get_settings
        from backend.app.infrastructure.auth.exceptions import (
            AuthenticationError,
            AuthorizationError,
        )
        from backend.app.infrastructure.auth.handler import (
            authentication_error_handler,
            authorization_error_handler,
        )

        app.dependency_overrides[get_settings] = lambda: _auth_settings()
        app.add_exception_handler(AuthenticationError, authentication_error_handler)
        app.add_exception_handler(AuthorizationError, authorization_error_handler)

        @app.get("/whoami")
        async def whoami(actor=_actor_context_dep) -> dict[str, str]:
            return {"actor_id": str(actor.actor_id)}

    else:

        @app.get("/ping")
        async def ping() -> dict[str, str]:
            return {"ok": "pong"}

    return app


class TestMiddlewareSpans:
    async def test_request_span_and_response_traceparent(
        self, exporter: InMemorySpanExporter
    ) -> None:
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/ping", headers={"X-Request-ID": "req-abc"})
        assert response.status_code == 200

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "HTTP GET"
        assert span.attributes["http.request.method"] == "GET"
        assert span.attributes["request_id"] == "req-abc"
        assert span.attributes["correlation_id"] == "req-abc"  # defaults to request id
        assert span.attributes["http.response.status_code"] == 200

        # The response traceparent is produced by the server span itself.
        response_tp = response.headers["traceparent"]
        assert response_tp == format_traceparent(
            TraceContext(
                trace_id=f"{span.context.trace_id:032x}",
                span_id=f"{span.context.span_id:016x}",
                sampled=True,
            )
        )

    async def test_inbound_traceparent_parents_server_span(
        self, exporter: InMemorySpanExporter
    ) -> None:
        app = _make_app()
        inbound = format_traceparent(
            TraceContext(trace_id="ab" * 16, span_id="cd" * 8, sampled=True)
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/ping", headers={"traceparent": inbound})
        assert response.status_code == 200

        span = exporter.get_finished_spans()[0]
        # The server span continues the inbound trace under the inbound span.
        assert span.context.trace_id == int("ab" * 16, 16)
        assert span.parent is not None
        assert span.parent.span_id == int("cd" * 8, 16)
        # The response traceparent continues the SAME trace but carries the
        # server's own span id (the inbound span id is NOT echoed).
        response_tp = response.headers["traceparent"]
        parts = response_tp.split("-")
        assert parts[1] == "ab" * 16
        assert parts[2] != "cd" * 8
        assert parts[2] == f"{span.context.span_id:016x}"

    async def test_expected_401_not_marked_error(self, exporter: InMemorySpanExporter) -> None:
        """Client auth errors are recorded neutrally, not as span errors."""
        app = _make_app(auth=True)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/whoami")
        assert response.status_code == 401

        span = exporter.get_finished_spans()[0]
        assert span.status.status_code is not StatusCode.ERROR
        assert span.attributes["http.response.status_code"] == 401
        # No actor context was ever bound for the anonymous request.
        assert "actor_id" not in span.attributes

    async def test_unhandled_500_marked_error(self, exporter: InMemorySpanExporter) -> None:
        """A genuine handler failure becomes an ERROR span with an exception event."""
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/boom")
        async def boom() -> None:
            msg = "handler exploded"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="handler exploded"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
                await client.get("/boom")

        span = exporter.get_finished_spans()[0]
        assert span.name == "HTTP GET"
        assert span.status.status_code is StatusCode.ERROR
        assert any(event.name == "exception" for event in span.events)

    async def test_actor_attributes_on_authenticated_request(
        self, exporter: InMemorySpanExporter
    ) -> None:
        from backend.app.infrastructure.auth.service import AuthService

        app = _make_app(auth=True)
        token = AuthService(_auth_settings()).create_token(str(uuid.uuid4()))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        span = exporter.get_finished_spans()[0]
        # Server-validated actor identity lands on the span.
        assert "actor_id" in span.attributes
        assert "tenant_id" in span.attributes
        # The default no-lookup builder derives an unambiguous ALL_VENUES
        # scope, so no venue_id is guessed.
        assert "venue_id" not in span.attributes


class TestShutdown:
    """Shutdown semantics — kept LAST: after shutdown the process cannot
    install a new provider (OTel sets the global provider once per
    process), so nothing after this test may need tracing enabled."""

    def test_shutdown_disables_and_flushes(self) -> None:
        assert tracing.enabled() is True
        tracing.shutdown_tracing()
        assert tracing.enabled() is False
        # Span helpers become safe no-ops after shutdown.
        import asyncio

        async def run() -> None:
            async with tracing.event_span("x") as span:
                assert span is None

        asyncio.run(run())
        tracing.set_current_span_attributes(tenant_id="t")
        # Idempotent — a second shutdown is harmless.
        tracing.shutdown_tracing()
