"""Request context middleware (Task 8.4).

Pure-ASGI middleware (no BaseHTTPMiddleware task-spawning overhead):
the request context is bound in the SAME task that runs the endpoint,
so contextvars propagation is guaranteed async-safe, and the context is
always unbound in a ``finally`` — even when the handler raises — so it
never leaks into the next request or into a concurrent one.

Behavior:

  - ``X-Request-ID``: accepted when it passes sanitization (printable
    ASCII, length-bounded); otherwise a fresh id is generated. The
    final id is always echoed on the response.
  - ``X-Correlation-ID``: accepted when valid; defaults to the request
    id when absent. Always echoed on the response.
  - ``traceparent`` (W3C Trace Context): an inbound valid header is
    propagated (trace id, parent span id, sampled flag); otherwise a
    fresh trace context is generated. A ``traceparent`` response header
    is always stamped so callers can continue the trace.

    When OpenTelemetry tracing is enabled (Task 8.7), the inbound
    ``traceparent`` becomes the parent of a real SERVER span for the
    request and the response ``traceparent`` is produced by that span
    (the server's own span id). When tracing is disabled, the
    correlation trace context is echoed as before.

Authentication/authorization are untouched: this middleware only reads
headers and mutates response headers, running before the auth
dependencies without intercepting them.
"""

from __future__ import annotations

import logging

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.observability.context import (
    RequestContext,
    bind,
    format_traceparent,
    new_request_id,
    new_trace_context,
    parse_traceparent,
    sanitize_identifier,
    unbind,
)

logger = logging.getLogger(__name__)

_REQUEST_ID_HEADER = "x-request-id"
_CORRELATION_ID_HEADER = "x-correlation-id"
_TRACEPARENT_HEADER = "traceparent"


def _header_value(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Read a header by lowercase name (ASGI normalizes keys)."""
    for key, value in headers:
        if key == name.encode("latin-1"):
            return value.decode("latin-1")
    return None


class RequestContextMiddleware:
    """Stamps request/correlation/trace context onto every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Websockets/lifespan pass through untouched.
            await self._app(scope, receive, send)
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers") or []

        # Safely accept (or generate) the identifiers.
        request_id = sanitize_identifier(_header_value(headers, _REQUEST_ID_HEADER))
        if request_id is None:
            request_id = new_request_id()
        correlation_id = sanitize_identifier(_header_value(headers, _CORRELATION_ID_HEADER))
        if correlation_id is None:
            correlation_id = request_id

        trace = parse_traceparent(_header_value(headers, _TRACEPARENT_HEADER))
        if trace is None:
            trace = new_trace_context()

        ctx = RequestContext(
            request_id=request_id,
            correlation_id=correlation_id,
            trace_id=trace.trace_id,
            span_id=trace.span_id,
            sampled=trace.sampled,
        )
        tokens = bind(ctx)

        async with tracing.http_request_span(
            scope,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            parent_trace=trace,
        ) as span:

            async def send_with_headers(message: Message) -> None:
                if message["type"] == "http.response.start":
                    response_headers = MutableHeaders(scope=message)
                    response_headers[_REQUEST_ID_HEADER] = ctx.request_id
                    response_headers[_CORRELATION_ID_HEADER] = ctx.correlation_id
                    # With a live server span, the response traceparent is
                    # produced by the span itself (the server's own span
                    # id); otherwise echo the correlation trace context.
                    response_headers[_TRACEPARENT_HEADER] = tracing.traceparent_from_span(
                        span
                    ) or format_traceparent(trace)
                    if message.get("status") is not None:
                        tracing.record_response_status(span, int(message["status"]))
                await send(message)

            try:
                await self._app(scope, receive, send_with_headers)
            except AuthenticationError, AuthorizationError:
                # Expected client errors — the registered auth exception
                # handlers turn these into 4xx JSON responses. Re-raise
                # without an ERROR+traceback log and without marking the
                # span as an error (record_response_status records 4xx
                # neutrally).
                raise
            except Exception as exc:
                # Log genuine failures with the request context so they
                # are traceable and record them on the span; re-raise so
                # the framework's error handling stays intact.
                tracing.record_exception(span, exc)
                logger.exception(
                    "unhandled request error",
                    extra={
                        "request_id": ctx.request_id,
                        "correlation_id": ctx.correlation_id,
                    },
                )
                raise
            finally:
                # Always clear the context — even on exceptions — so no
                # state leaks into the next request.
                unbind(tokens)
