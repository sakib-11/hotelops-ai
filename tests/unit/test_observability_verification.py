"""Task 8.11 — End-to-end observability verification (unit level).

Verifies the HTTP request boundary of the complete Task 8 flow:

    HTTP request → FastAPI → middleware → (auth) → response

against the real middleware, formatters, and context modules:

  1. request_id exists
  2. correlation_id exists
  3. trace_id exists
  4-6. actor/tenant/venue context represented safely
  9. structured JSON logs are produced
  11. secrets are absent
  12. errors contain useful diagnostic context
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.infrastructure.observability.context import (
    get_request_context,
    request_id,
)
from backend.app.infrastructure.observability.middleware import RequestContextMiddleware

REQUEST_ID_HEADER = "x-request-id"
CORRELATION_ID_HEADER = "x-correlation-id"
TRACEPARENT_HEADER = "traceparent"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)  # type: ignore[arg-type]

    @app.get("/echo")
    async def echo() -> dict[str, Any]:
        # Emit a log line so the structured formatter is exercised.
        logging.getLogger("tests.verification").info("echo handler called")
        return get_request_context()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("diagnostic-boom")

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _capture_log(
    app: FastAPI, method: str, path: str, **headers: str
) -> tuple[dict[str, Any], str]:
    """Run a request while capturing the emitted JSON log line.

    Returns (response_json, emitted_log_line).
    """
    from backend.app.infrastructure.config import Settings
    from backend.app.infrastructure.logging import configure_logging

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings=settings)

    import io

    buf = io.StringIO()
    root = logging.getLogger()
    handler = root.handlers[0]
    original = handler.stream
    handler.stream = buf
    try:
        async with _client(app) as client:
            response = await client.request(method, path, headers=headers)
    finally:
        handler.stream = original
    return response.json() if response.content else {}, buf.getvalue().strip()


class TestRequestIdentifiers:
    """Points 1-3: request_id, correlation_id, trace_id all exist."""

    async def test_successful_request_has_all_identifiers(self) -> None:
        app = _make_app()
        async with _client(app) as client:
            response = await client.get(
                "/echo",
                headers={
                    REQUEST_ID_HEADER: "req-1",
                    CORRELATION_ID_HEADER: "corr-1",
                    TRACEPARENT_HEADER: f"00-{'ab' * 16}-{'cd' * 8}-01",
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["request_id"] == "req-1"  # 1
        assert body["correlation_id"] == "corr-1"  # 2
        assert body["trace_id"] == "ab" * 16  # 3
        assert body["span_id"] == "cd" * 8

    async def test_identifiers_generated_when_absent(self) -> None:
        app = _make_app()
        async with _client(app) as client:
            response = await client.get("/echo")
        assert response.status_code == 200
        body = response.json()
        assert len(body["request_id"]) == 32  # 1 - hex
        assert body["correlation_id"] == body["request_id"]  # 2 - defaults to request id
        assert len(body["trace_id"]) == 32  # 3
        assert len(body["span_id"]) == 16


class TestSafeContextRepresentation:
    """Points 4-6: actor/tenant/venue represented safely."""

    async def test_anonymous_request_has_no_actor_context(self) -> None:
        """Anonymous requests must not invent actor/tenant/venue values."""
        app = _make_app()
        async with _client(app) as client:
            response = await client.get("/echo")
        assert response.status_code == 200
        body = response.json()
        # Safe representation: absent keys, never empty strings or guesses.
        assert "actor_id" not in body
        assert "tenant_id" not in body
        assert "venue_id" not in body

    async def test_context_cleared_after_request(self) -> None:
        """Tenant context must never leak between requests."""
        app = _make_app()
        async with _client(app) as client:
            await client.get("/echo", headers={REQUEST_ID_HEADER: "req-a"})
        # After the request completes, no context may remain in this task.
        assert request_id() is None
        assert get_request_context() == {}


class TestStructuredJsonLogs:
    """Point 9: structured JSON logs are produced."""

    async def test_json_log_emitted_with_context(self) -> None:
        app = _make_app()
        _body, log_line = await _capture_log(
            app,
            "GET",
            "/echo",
            **{REQUEST_ID_HEADER: "req-json", CORRELATION_ID_HEADER: "corr-json"},
        )
        assert log_line, "a structured log line must be emitted"
        record = json.loads(log_line.splitlines()[0])
        assert record["level"] == "INFO"
        assert record["message"]
        assert record["request_id"] == "req-json"
        assert record["correlation_id"] == "corr-json"
        assert record["trace_id"]  # injected by the ContextFilter

    async def test_no_manual_json_dumps_in_output(self) -> None:
        """Log output must be one JSON object per line, not a string blob."""
        app = _make_app()
        _, log_line = await _capture_log(app, "GET", "/echo")
        for line in log_line.splitlines():
            parsed = json.loads(line)  # must be valid JSON
            assert isinstance(parsed, dict)


class TestSecretsAbsent:
    """Point 11: secrets are absent from logs."""

    async def test_password_in_message_is_redacted(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)  # type: ignore[arg-type]

        @app.get("/leak")
        async def leak() -> dict[str, str]:
            logging.getLogger(__name__).info("password = hunter2")
            return {"ok": "yes"}

        import io

        buf = io.StringIO()
        root = logging.getLogger()
        handler = root.handlers[0]
        original = handler.stream
        handler.stream = buf
        try:
            async with _client(app) as client:
                await client.get("/leak")
        finally:
            handler.stream = original
        output = buf.getvalue()
        assert "hunter2" not in output
        assert "[REDACTED]" in output

    async def test_authorization_header_never_logged(self) -> None:
        """The Authorization header value must never appear in any output."""
        app = _make_app()
        _, log_line = await _capture_log(app, "GET", "/echo", Authorization="Bearer SECRETTOK")
        assert "SECRETTOK" not in log_line


class TestErrorDiagnostics:
    """Point 12: errors contain useful diagnostic context."""

    async def test_unhandled_error_is_logged_with_request_context(self) -> None:
        app = _make_app()
        import io

        buf = io.StringIO()
        root = logging.getLogger()
        handler = root.handlers[0]
        original = handler.stream
        handler.stream = buf
        try:
            async with _client(app) as client:
                with pytest.raises(RuntimeError, match="diagnostic-boom"):
                    await client.get("/boom", headers={REQUEST_ID_HEADER: "req-boom"})
        finally:
            handler.stream = original
        output = buf.getvalue()
        # The error log carries the request context for correlation.
        assert "req-boom" in output
        assert "unhandled request error" in output
        assert "traceback" in output.lower() or "exc_type" in output


class TestConcurrentRequests:
    """Concurrent requests: contexts must not leak."""

    async def test_concurrent_requests_isolated(self) -> None:
        app = _make_app()

        async def hit(request_id_value: str) -> str:
            async with _client(app) as client:
                response = await client.get("/echo", headers={REQUEST_ID_HEADER: request_id_value})
            return response.json()["request_id"]

        sent = [f"req-{i}" for i in range(20)]
        echoed = await asyncio.gather(*[hit(v) for v in sent])
        assert echoed == sent, "each request must observe exactly its own context"


class TestMissingTelemetryConfiguration:
    """Missing telemetry configuration must not break requests."""

    def test_tracing_and_metrics_disabled_by_default(self) -> None:
        from backend.app.infrastructure.config import Settings

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.observability_tracing_enabled is False
        assert settings.observability_metrics_enabled is False

    async def test_requests_still_work_with_telemetry_off(self) -> None:
        """Even with all telemetry disabled, requests return context headers."""
        app = _make_app()
        async with _client(app) as client:
            response = await client.get("/echo")
        assert response.status_code == 200
        assert response.headers.get(REQUEST_ID_HEADER)
        assert response.headers.get(TRACEPARENT_HEADER)


class TestMetricsEndpoint:
    """Point 10: metrics are produced when enabled, 404 when disabled."""

    def _build_app(self) -> FastAPI:
        from backend.app.api.routes.metrics import router as metrics_router
        from backend.app.infrastructure.observability.metrics import MetricsMiddleware

        app = FastAPI()
        app.include_router(metrics_router)
        app.add_middleware(RequestContextMiddleware)  # type: ignore[arg-type]
        app.add_middleware(MetricsMiddleware)  # type: ignore[arg-type]

        @app.get("/echo")
        async def echo() -> dict[str, str]:
            return {"ok": "yes"}

        return app

    async def test_metrics_disabled_returns_404(self) -> None:
        from backend.app.infrastructure.observability import metrics as metrics_mod

        metrics_mod._enabled = False  # ensure disabled state
        app = self._build_app()
        async with _client(app) as client:
            response = await client.get("/metrics")
        assert response.status_code == 404

    async def test_metrics_enabled_produces_prometheus_format(self) -> None:
        from backend.app.infrastructure.config import Settings
        from backend.app.infrastructure.observability import metrics as metrics_mod

        settings = Settings(_env_file=None, OBSERVABILITY_METRICS_ENABLED=True)  # type: ignore[call-arg]
        metrics_mod._enabled = False  # reset so configure_metrics can enable
        metrics_mod._registry = None
        assert metrics_mod.configure_metrics(settings) is True
        assert metrics_mod.enabled() is True

        app = self._build_app()
        async with _client(app) as client:
            await client.get("/echo")  # generate a request metric
            response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/plain")
        body = response.text
        # Prometheus exposition format lines.
        assert "http_requests_total" in body
        assert 'method="GET"' in body
        assert 'status="200"' in body
        assert "http_request_duration_seconds" in body

    async def test_record_request_noop_when_disabled(self) -> None:
        from backend.app.infrastructure.observability import metrics as metrics_mod

        metrics_mod._enabled = False
        # Must not raise when disabled.
        metrics_mod.record_request("GET", 200, 0.1)
        assert metrics_mod.render()[0] == b""
