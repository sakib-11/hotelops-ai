"""Task 8.5 — actor/tenant/venue observability context tests.

Covers binding the server-validated ActorContext into the task-local
observability context, venue-id derivation rules, cleanup, the safe
representation of anonymous operations, logger injection, and the
per-request lifecycle of the auth dependency (no cross-request leaks).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.infrastructure.auth.deps import get_actor_context
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.handler import (
    authentication_error_handler,
    authorization_error_handler,
)
from backend.app.infrastructure.auth.service import AuthService
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability.context import (
    actor_id,
    bind_actor_context,
    get_request_context,
    tenant_id,
    unbind,
    venue_id,
)
from contracts.common import utc_now
from contracts.identity import ActorContext, Permission, RoleName


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        SECRET_KEY="unit-test-secret",
        JWT_ALGORITHM="HS256",
        JWT_EXPIRATION_MINUTES=60,
    )


def _make_actor(*, venue_scope: frozenset = frozenset()) -> ActorContext:
    return ActorContext(
        actor_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role_name=RoleName.OPERATOR,
        permissions=frozenset({Permission.VIDEO_READ}),
        venue_scope=venue_scope,
        authenticated_at=utc_now(),
        active=True,
    )


# =========================================================================
# bind_actor_context / derivation rules
# =========================================================================


async def test_bind_actor_context_sets_actor_and_tenant() -> None:
    actor = _make_actor(venue_scope=frozenset({uuid.uuid4()}))
    tokens = bind_actor_context(actor)
    try:
        assert actor_id() == str(actor.actor_id)
        assert tenant_id() == str(actor.tenant_id)
        assert venue_id() == str(next(iter(actor.venue_scope)))
        ctx = get_request_context()
        assert ctx["actor_id"] == str(actor.actor_id)
        assert ctx["tenant_id"] == str(actor.tenant_id)
    finally:
        unbind(tokens)


async def test_single_venue_scope_sets_venue_id() -> None:
    venue = uuid.uuid4()
    tokens = bind_actor_context(_make_actor(venue_scope=frozenset({venue})))
    try:
        assert venue_id() == str(venue)
    finally:
        unbind(tokens)


async def test_multi_venue_scope_leaves_venue_id_none() -> None:
    """Ambiguous venue context must not be guessed for logs."""
    tokens = bind_actor_context(_make_actor(venue_scope=frozenset({uuid.uuid4(), uuid.uuid4()})))
    try:
        assert venue_id() is None
        assert "venue_id" not in get_request_context()
    finally:
        unbind(tokens)


async def test_all_venues_scope_leaves_venue_id_none() -> None:
    tokens = bind_actor_context(_make_actor())  # empty scope = ALL_VENUES
    try:
        assert venue_id() is None
    finally:
        unbind(tokens)


async def test_unbind_clears_actor_context() -> None:
    tokens = bind_actor_context(_make_actor())
    unbind(tokens)
    assert actor_id() is None
    assert tenant_id() is None
    assert venue_id() is None
    assert get_request_context() == {}


async def test_anonymous_has_no_actor_fields() -> None:
    """Anonymous/system operations: no actor/tenant/venue keys at all."""
    ctx = get_request_context()
    assert "actor_id" not in ctx
    assert "tenant_id" not in ctx
    assert "venue_id" not in ctx
    assert actor_id() is None


# =========================================================================
# Logger integration
# =========================================================================


async def test_context_filter_injects_actor_fields_into_logs() -> None:
    from backend.app.infrastructure.logging import ContextFilter, JsonFormatter

    filter_ = ContextFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )
    tokens = bind_actor_context(_make_actor(venue_scope=frozenset({uuid.uuid4()})))
    try:
        assert filter_.filter(record) is True
    finally:
        unbind(tokens)
    assert record.actor_id is not None
    assert record.tenant_id is not None
    assert record.venue_id is not None

    # The JSON formatter emits the allowlisted actor fields.
    import json

    payload = json.loads(JsonFormatter(service="s", environment="e", version="v").format(record))
    assert payload["actor_id"] == record.actor_id
    assert payload["tenant_id"] == record.tenant_id
    assert payload["venue_id"] == record.venue_id


async def test_anonymous_logs_have_no_actor_fields() -> None:
    from backend.app.infrastructure.logging import JsonFormatter

    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="anon",
        args=(),
        exc_info=None,
    )
    import json

    payload = json.loads(JsonFormatter(service="s", environment="e", version="v").format(record))
    assert "actor_id" not in payload
    assert "tenant_id" not in payload
    assert "venue_id" not in payload


# =========================================================================
# Auth dependency lifecycle (real token + real get_actor_context)
# =========================================================================


# B008: the repo allows the Depends() pattern only in app modules, so
# tests use a module-level dependency marker.
_actor_context_dep = Depends(get_actor_context)


def _make_auth_app() -> FastAPI:
    from backend.app.dependencies import get_settings

    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: _settings()
    # Mirror production main.py: auth exceptions become 401/403 JSON.
    app.add_exception_handler(AuthenticationError, authentication_error_handler)
    app.add_exception_handler(AuthorizationError, authorization_error_handler)

    @app.get("/whoami")
    async def whoami(actor: ActorContext = _actor_context_dep) -> dict:
        return {
            "actor_id": str(actor.actor_id),
            "tenant_id": str(actor.tenant_id),
            "context": get_request_context(),
        }

    return app


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = AuthService(_settings()).create_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def test_actor_context_bound_during_authenticated_request() -> None:
    app = _make_auth_app()
    user_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/whoami", headers=_auth_headers(user_id))
    assert response.status_code == 200
    body = response.json()
    assert body["actor_id"] == str(user_id)
    # Default builder (no lookups) resolves the zero tenant + operator role.
    assert body["context"]["actor_id"] == str(user_id)
    assert body["context"]["tenant_id"] == body["tenant_id"]
    # The dependency teardown has run — nothing may leak.
    assert actor_id() is None
    assert get_request_context() == {}


async def test_actor_context_cleared_after_anonymous_request() -> None:
    app = _make_auth_app()
    # No Authorization header → the auth dependency never binds anything.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/whoami")
    assert response.status_code == 401  # still protected (auth preserved)
    assert get_request_context() == {}


async def test_actor_context_cleared_when_handler_raises() -> None:
    """Generator-dependency teardown runs even when the handler raises.

    Starlette's generator context manager re-enters the generator (athrow)
    when the code inside raises, so the dependency's ``finally: unbind``
    executes even on an unhandled handler exception — the actor context
    must never survive into the next request.
    """
    app = FastAPI()
    from backend.app.dependencies import get_settings

    app.dependency_overrides[get_settings] = lambda: _settings()

    @app.get("/boom")
    async def boom(actor: ActorContext = _actor_context_dep) -> dict:
        msg = "handler exploded"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="handler exploded"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.get("/boom", headers=_auth_headers(uuid.uuid4()))
    # The dependency teardown ran despite the propagated exception.
    assert get_request_context() == {}
    assert actor_id() is None


async def test_sequential_requests_do_not_leak_actor() -> None:
    app = _make_auth_app()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/whoami", headers=_auth_headers(user_a))
        second = await client.get("/whoami", headers=_auth_headers(user_b))
    assert first.json()["actor_id"] == str(user_a)
    assert second.json()["actor_id"] == str(user_b)
    assert actor_id() is None


async def test_concurrent_authenticated_requests_do_not_leak() -> None:
    app = _make_auth_app()
    users = [uuid.uuid4() for _ in range(10)]

    async def hit(user: uuid.UUID) -> str:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/whoami", headers=_auth_headers(user))
        return response.json()["actor_id"]

    results = await asyncio.gather(*[hit(user) for user in users])
    assert results == [str(user) for user in users], "each request sees only its own actor"
    assert actor_id() is None
