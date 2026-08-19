"""Async-safe request context (Task 8.4).

Request, correlation and trace identifiers live in ``contextvars``
ContextVar objects — task-local storage that asyncio copies into
spawned tasks and that the owning middleware always resets after the
request. There are no module-level mutable globals, so context can
never leak between concurrent requests.

Identifier hygiene follows the project security conventions:

  - inbound identifiers are accepted only after sanitization: printable
    ASCII, no control characters (log-injection safe), length-bounded
    (truncated to ``_MAX_IDENTIFIER_LENGTH``). Invalid values are
    rejected and a fresh identifier is generated instead.
  - trace context follows the W3C Trace Context standard: the
    ``traceparent`` header (version-00, 32-hex trace id, 16-hex span
    id, 2-hex flags). Malformed/invalid ``traceparent`` values are
    ignored and a fresh trace context is generated.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contracts.identity import ActorContext

# Inbound identifiers longer than this are truncated (bounds log size).
_MAX_IDENTIFIER_LENGTH = 128
# Printable ASCII only — control characters are rejected (log injection).
_PRINTABLE_ASCII = re.compile(r"^[ -~]+$")

# W3C Trace Context: version-00, trace id (16 bytes), span id (8 bytes),
# flags byte (bit 0x01 = sampled).
_TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16
_TRACEPARENT_VERSION = (
    "00"  # --- Context storage (task-local; default None = no active request) ---
)
_request_id_var: ContextVar[str | None] = ContextVar("observability.request_id", default=None)
_correlation_id_var: ContextVar[str | None] = ContextVar(
    "observability.correlation_id", default=None
)
_trace_id_var: ContextVar[str | None] = ContextVar("observability.trace_id", default=None)
_span_id_var: ContextVar[str | None] = ContextVar("observability.span_id", default=None)
_trace_sampled_var: ContextVar[bool] = ContextVar("observability.trace_sampled", default=False)

# --- Actor identity (Task 8.5) ---
# Populated ONLY from a server-validated ActorContext (the auth
# dependency), never from client-supplied values. Absence of these
# fields is the safe representation of anonymous/system operations.
_actor_id_var: ContextVar[str | None] = ContextVar("observability.actor_id", default=None)
_tenant_id_var: ContextVar[str | None] = ContextVar("observability.tenant_id", default=None)
_venue_id_var: ContextVar[str | None] = ContextVar("observability.venue_id", default=None)


@dataclass(frozen=True)
class TraceContext:
    """A W3C Trace Context triple."""

    trace_id: str
    span_id: str
    sampled: bool


@dataclass(frozen=True)
class RequestContext:
    """The full request-correlation identity for one HTTP request."""

    request_id: str
    correlation_id: str
    trace_id: str
    span_id: str
    sampled: bool = True


# =========================================================================
# Identifier generation / sanitization
# =========================================================================


def new_request_id() -> str:
    """A fresh random request id (32 hex chars, collision-safe)."""
    return secrets.token_hex(16)


def new_trace_context(*, sampled: bool = True) -> TraceContext:
    """A fresh W3C-compliant trace context."""
    return TraceContext(
        trace_id=secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        sampled=sampled,
    )


def sanitize_identifier(value: str | None) -> str | None:
    """Sanitize an inbound identifier.

    Returns:
        The identifier (stripped, printable-ASCII, truncated to the
        bound) or None when the value is unusable (missing, empty, or
        contains control/non-printable characters).
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if not _PRINTABLE_ASCII.match(value):
        return None
    return value[:_MAX_IDENTIFIER_LENGTH]


# =========================================================================
# W3C traceparent
# =========================================================================


def parse_traceparent(value: str | None) -> TraceContext | None:
    """Parse a ``traceparent`` header value.

    Returns:
        The TraceContext, or None when the header is absent or invalid
        (the caller generates a fresh context in that case).
    """
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT_PATTERN.match(value.strip())
    if match is None or match.group("version") != _TRACEPARENT_VERSION:
        return None
    trace_id = match.group("trace_id")
    span_id = match.group("span_id")
    if trace_id == _ZERO_TRACE_ID or span_id == _ZERO_SPAN_ID:
        return None  # all-zero ids are invalid per the W3C spec
    sampled = bool(int(match.group("flags"), 16) & 0x01)
    return TraceContext(trace_id=trace_id, span_id=span_id, sampled=sampled)


def format_traceparent(ctx: TraceContext) -> str:
    """Serialize a TraceContext as a W3C ``traceparent`` header value."""
    flags = "01" if ctx.sampled else "00"
    return f"{_TRACEPARENT_VERSION}-{ctx.trace_id}-{ctx.span_id}-{flags}"


# =========================================================================
# Bind / unbind (middleware owns the lifecycle)
# =========================================================================


def bind(ctx: RequestContext) -> tuple[Token[Any], ...]:
    """Set the request context for the current task.

    Returns:
        The tokens to pass to :func:`unbind` (exactly once, in a
        finally block) so the context is cleared after the request.
    """
    return (
        _request_id_var.set(ctx.request_id),
        _correlation_id_var.set(ctx.correlation_id),
        _trace_id_var.set(ctx.trace_id),
        _span_id_var.set(ctx.span_id),
        _trace_sampled_var.set(ctx.sampled),
    )


def unbind(tokens: tuple[Token[Any], ...]) -> None:
    """Reset every token returned by :func:`bind`."""
    for token in tokens:
        token.var.reset(token)


# =========================================================================
# Read access
# =========================================================================


def request_id() -> str | None:
    """The active request id (None outside a request)."""
    return _request_id_var.get()


def correlation_id() -> str | None:
    """The active correlation id (None outside a request)."""
    return _correlation_id_var.get()


def trace_id() -> str | None:
    """The active W3C trace id (None outside a request)."""
    return _trace_id_var.get()


def actor_id() -> str | None:
    """The server-validated actor id (None for anonymous/system)."""
    return _actor_id_var.get()


def tenant_id() -> str | None:
    """The server-validated tenant id (None for anonymous/system)."""
    return _tenant_id_var.get()


def venue_id() -> str | None:
    """The server-derived venue id (None when ambiguous or anonymous)."""
    return _venue_id_var.get()


def bind_actor_context(actor: ActorContext) -> tuple[Token[Any], ...]:
    """Bind the server-validated actor identity for the current task.

    AUTH-LAYER-ONLY: call this solely from the auth dependency
    (``get_actor_context``) with the ActorContext it constructed.
    Binding an actor here is a trust decision — the values flow into
    logs/audit — so a caller must never pass a client-constructed or
    client-influenced ActorContext.

    Derives ONLY from the trusted ActorContext constructed by the auth
    layer (Task 5) — client-supplied tenant/venue/actor values are never
    accepted. ``venue_id`` is set only when the actor's venue scope is
    exactly one venue (unambiguous server-derived venue context); a
    multi-venue or all-venues scope leaves it unset rather than guessing.

    Returns:
        The tokens to pass to :func:`unbind` in a finally block.
    """
    venue_scope = actor.venue_scope
    venue_id: str | None = None
    if venue_scope is not None and len(venue_scope) == 1:
        venue_id = str(next(iter(venue_scope)))
    return (
        _actor_id_var.set(str(actor.actor_id)),
        _tenant_id_var.set(str(actor.tenant_id)),
        _venue_id_var.set(venue_id),
    )


def get_request_context() -> dict[str, Any]:
    """The active context as a flat dict (for the log filter / audit).

    Request/correlation/trace fields plus the server-validated actor
    identity. Only populated values are surfaced — an anonymous or
    system operation simply has no actor/tenant/venue keys, which is
    the safe representation. The sampled flag stays internal. An empty
    dict outside a request makes the log ContextFilter a no-op.
    """
    ctx: dict[str, Any] = {}
    if (value := _request_id_var.get()) is not None:
        ctx["request_id"] = value
    if (value := _correlation_id_var.get()) is not None:
        ctx["correlation_id"] = value
    if (value := _trace_id_var.get()) is not None:
        ctx["trace_id"] = value
    if (value := _span_id_var.get()) is not None:
        ctx["span_id"] = value
    if (value := _actor_id_var.get()) is not None:
        ctx["actor_id"] = value
    if (value := _tenant_id_var.get()) is not None:
        ctx["tenant_id"] = value
    if (value := _venue_id_var.get()) is not None:
        ctx["venue_id"] = value
    return ctx


__all__ = [
    "RequestContext",
    "TraceContext",
    "actor_id",
    "bind",
    "bind_actor_context",
    "correlation_id",
    "format_traceparent",
    "get_request_context",
    "new_request_id",
    "new_trace_context",
    "parse_traceparent",
    "request_id",
    "sanitize_identifier",
    "tenant_id",
    "trace_id",
    "unbind",
    "venue_id",
]
