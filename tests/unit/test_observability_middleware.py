"""Task 8.4 — request/correlation context propagation tests.

Exercises the real RequestContextMiddleware + contextvars module through
an ASGI transport: generated vs propagated identifiers, sanitization,
W3C traceparent handling, concurrency isolation, cleanup on success and
on exceptions, and the logger ContextFilter.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.infrastructure.observability.context import (
    RequestContext,
    bind,
    correlation_id,
    format_traceparent,
    get_request_context,
    new_request_id,
    parse_traceparent,
    request_id,
    sanitize_identifier,
    trace_id,
    unbind,
)
from backend.app.infrastructure.observability.middleware import RequestContextMiddleware

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")

REQUEST_ID_HEADER = "x-request-id"
CORRELATION_ID_HEADER = "x-correlation-id"
TRACEPARENT_HEADER = "traceparent"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)  # type: ignore[arg-type]

    @app.get("/echo")
    async def echo() -> dict[str, Any]:
        return get_request_context()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# =========================================================================
# Generated identifiers
# =========================================================================


async def test_generated_request_id_is_echoed() -> None:
    app = _make_app()
    async with _client(app) as client:
        response = await client.get("/echo")
    assert response.status_code == 200
    generated = response.headers.get(REQUEST_ID_HEADER)
    assert generated is not None and _HEX32.match(generated)
    body = response.json()
    assert body["request_id"] == generated
    # correlation defaults to the request id when not supplied
    assert body["correlation_id"] == generated


async def test_generated_traceparent_is_stamped() -> None:
    app = _make_app()
    async with _client(app) as client:
        response = await client.get("/echo")
    traceparent = response.headers.get(TRACEPARENT_HEADER)
    assert traceparent is not None and _TRACEPARENT.match(traceparent)
    body = response.json()
    trace = parse_traceparent(traceparent)
    assert trace is not None
    assert body["trace_id"] == trace.trace_id
    assert body["span_id"] == trace.span_id


# =========================================================================
# Propagated identifiers
# =========================================================================


async def test_propagated_request_id_is_preserved() -> None:
    app = _make_app()
    async with _client(app) as client:
        response = await client.get("/echo", headers={REQUEST_ID_HEADER: "my-request-42"})
    assert response.headers.get(REQUEST_ID_HEADER) == "my-request-42"
    assert response.json()["request_id"] == "my-request-42"


async def test_propagated_correlation_id() -> None:
    app = _make_app()
    async with _client(app) as client:
        response = await client.get(
            "/echo",
            headers={
                REQUEST_ID_HEADER: "req-a",
                CORRELATION_ID_HEADER: "corr-99",
            },
        )
    body = response.json()
    assert body["request_id"] == "req-a"
    assert body["correlation_id"] == "corr-99"
    assert response.headers.get(CORRELATION_ID_HEADER) == "corr-99"


async def test_propagated_traceparent_is_continued() -> None:
    app = _make_app()
    inbound = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    async with _client(app) as client:
        response = await client.get("/echo", headers={TRACEPARENT_HEADER: inbound})
    body = response.json()
    assert body["trace_id"] == "ab" * 16
    assert body["span_id"] == "cd" * 8
    # The response stamps the same trace context so callers can continue it.
    assert response.headers.get(TRACEPARENT_HEADER) == inbound


# =========================================================================
# Sanitization / invalid input
# =========================================================================


async def test_invalid_request_id_is_rejected_and_replaced() -> None:
    app = _make_app()
    async with _client(app) as client:
        response = await client.get("/echo", headers={REQUEST_ID_HEADER: "bad\ninjection"})
    generated = response.headers.get(REQUEST_ID_HEADER)
    assert generated is not None and _HEX32.match(generated), "control chars must be rejected"
    assert generated != "bad\ninjection"


async def test_overlong_request_id_is_truncated() -> None:
    app = _make_app()
    long_id = "x" * 500
    async with _client(app) as client:
        response = await client.get("/echo", headers={REQUEST_ID_HEADER: long_id})
    echoed = response.headers.get(REQUEST_ID_HEADER)
    assert echoed is not None and len(echoed) == 128 and echoed == "x" * 128


async def test_malformed_traceparent_is_ignored() -> None:
    app = _make_app()
    async with _client(app) as client:
        response = await client.get(
            "/echo",
            headers={TRACEPARENT_HEADER: "99-not-valid-traceparent"},
        )
    body = response.json()
    assert _HEX32.match(body["trace_id"]), "invalid traceparent must be replaced"
    assert _HEX16.match(body["span_id"])


# =========================================================================
# Concurrency isolation
# =========================================================================


async def test_concurrent_requests_do_not_leak_context() -> None:
    app = _make_app()

    async def hit(request_id_value: str) -> str:
        async with _client(app) as client:
            response = await client.get("/echo", headers={REQUEST_ID_HEADER: request_id_value})
        return response.json()["request_id"]

    sent = [f"req-{i}" for i in range(20)]
    echoed = await asyncio.gather(*[hit(value) for value in sent])
    assert echoed == sent, "each request must observe exactly its own context"


# =========================================================================
# Cleanup
# =========================================================================


async def test_context_is_cleared_after_request() -> None:
    app = _make_app()
    async with _client(app) as client:
        await client.get("/echo")
    # The middleware's finally has run — no context may remain.
    assert request_id() is None
    assert correlation_id() is None
    assert trace_id() is None
    assert get_request_context() == {}


async def test_context_is_cleared_on_exception() -> None:
    app = _make_app()
    async with _client(app) as client:
        # The framework's error handling is preserved: the unhandled
        # exception still propagates (this stack raises out of the
        # transport instead of synthesizing a 500).
        with pytest.raises(RuntimeError, match="kaboom"):
            await client.get("/boom")
    # Even though the handler raised, context must be unbound.
    assert request_id() is None
    assert get_request_context() == {}


# =========================================================================
# Context module primitives
# =========================================================================


async def test_bind_and_unbind_round_trip() -> None:
    ctx = RequestContext(
        request_id="r1",
        correlation_id="c1",
        trace_id="t1",
        span_id="s1",
        sampled=True,
    )
    assert request_id() is None
    tokens = bind(ctx)
    assert request_id() == "r1"
    assert correlation_id() == "c1"
    assert trace_id() == "t1"
    assert get_request_context()["request_id"] == "r1"
    unbind(tokens)
    assert request_id() is None
    assert get_request_context() == {}


def test_sanitize_identifier() -> None:
    assert sanitize_identifier("  abc-123  ") == "abc-123"
    assert sanitize_identifier("a\nb") is None
    assert sanitize_identifier("") is None
    assert sanitize_identifier(None) is None
    assert sanitize_identifier("x" * 500) == "x" * 128


def test_parse_and_format_traceparent() -> None:
    ctx = parse_traceparent("00-00112233445566778899aabbccddeeff-0011223344556677-01")
    assert ctx is not None
    assert ctx.trace_id == "00112233445566778899aabbccddeeff"
    assert ctx.span_id == "0011223344556677"
    assert ctx.sampled is True
    assert format_traceparent(ctx) == "00-00112233445566778899aabbccddeeff-0011223344556677-01"

    # Invalid: wrong version, wrong lengths, all-zero ids, garbage.
    assert parse_traceparent("01-00112233445566778899aabbccddeeff-0011223344556677-01") is None
    assert parse_traceparent("00-0011-0011223344556677-01") is None
    assert parse_traceparent("00-" + "0" * 32 + "-0011223344556677-01") is None
    assert parse_traceparent("garbage") is None
    assert parse_traceparent(None) is None
    assert parse_traceparent("") is None


async def test_new_identifiers_are_random_and_formatted() -> None:
    a, b = new_request_id(), new_request_id()
    assert a != b and _HEX32.match(a) and _HEX32.match(b)


async def test_context_filter_injects_request_context_into_log_records() -> None:
    from backend.app.infrastructure.logging import ContextFilter

    filter_ = ContextFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )
    ctx = RequestContext(
        request_id="r-inject",
        correlation_id="c-inject",
        trace_id="t-inject",
        span_id="s-inject",
    )
    tokens = bind(ctx)
    try:
        assert filter_.filter(record) is True
    finally:
        unbind(tokens)
    assert record.request_id == "r-inject"
    assert record.correlation_id == "c-inject"
    assert record.trace_id == "t-inject"


async def test_bind_in_one_task_is_invisible_to_sibling_task() -> None:
    """Our bind()/unbind() must never leak into a task created before binding.

    asyncio copies the current context into a task AT CREATION TIME, so
    a sibling created before ``bind()`` holds a snapshot with no
    request context — reading it must yield None while the bound task
    sees its own values.
    """
    seen: dict[str, str | None] = {}

    async def binder() -> None:
        tokens = bind(
            RequestContext(
                request_id="only-in-binder",
                correlation_id="only-in-binder",
                trace_id="t",
                span_id="s",
            )
        )
        try:
            await asyncio.sleep(0.02)
        finally:
            unbind(tokens)

    async def sibling() -> None:
        await asyncio.sleep(0.01)  # runs while binder holds its bind
        seen["sibling"] = request_id()

    async def self_check() -> None:
        # bind in the CURRENT task and observe it directly
        tokens = bind(
            RequestContext(
                request_id="self-visible",
                correlation_id="c",
                trace_id="t",
                span_id="s",
            )
        )
        try:
            seen["self"] = request_id()
            await asyncio.sleep(0.03)
        finally:
            unbind(tokens)

    # gather() creates tasks in argument order: sibling's context snapshot
    # is taken BEFORE binder/self ever bind, so it can never see them.
    await asyncio.gather(sibling(), binder(), self_check())
    assert seen["self"] == "self-visible"
    assert seen["sibling"] is None, "a task created pre-bind must never see the context"
    assert request_id() is None, "unbind must clear the current task"


async def test_non_http_scope_passes_through() -> None:
    """Lifespan/websocket scopes are not wrapped (no context, no headers)."""
    from backend.app.infrastructure.observability.middleware import RequestContextMiddleware

    async def receive() -> dict:
        return {}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    downstream_called = False

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        nonlocal downstream_called
        downstream_called = True
        assert scope["type"] == "lifespan"

    middleware = RequestContextMiddleware(downstream)  # type: ignore[arg-type]
    await middleware({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    assert downstream_called
    assert sent == [], "non-http scopes must not get response headers"
    assert request_id() is None
