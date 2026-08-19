"""Task 8.5 — Security tests: actor/tenant/venue observability context.

Attack scenarios against the Task 8.5 integration of Task 5 security
with Task 8 observability:

 1. Tenant spoofing  — X-Tenant-Id header must never influence the
                       observability context (server-validated only).
 2. Venue spoofing   — X-Venue-Id header must never be reflected.
 3. Actor spoofing   — X-Actor-Id header must never be reflected.
 4. Credential leak  — the Bearer token must never appear in response
                       headers, request context, or emitted JSON logs.
 5. Secret extra     — non-allowlisted ``extra=`` keys (password,
                       token, authorization) are dropped by the formatter.
 6. Concurrent isolation — simultaneous requests under different actors
                       never leak tenant/actor context into each other.
 7. Anonymous safety — unauthenticated/system operations carry no
                       actor/tenant/venue fields at all.

These tests target PRODUCTION code only. Do not weaken production
code to make testing easier.
"""

from __future__ import annotations

import asyncio
import json
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
from backend.app.infrastructure.logging import ContextFilter, JsonFormatter
from backend.app.infrastructure.observability.context import (
    bind_actor_context,
    get_request_context,
    unbind,
)
from contracts.common import utc_now
from contracts.identity import ActorContext, Permission, RoleName

# ── Shared test helpers ────────────────────────────────────────────────────────


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


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = AuthService(_settings()).create_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# B008: the repo allows the Depends() pattern only in app modules, so
# tests use a module-level dependency marker.
_actor_context_dep = Depends(get_actor_context)


def _make_app() -> FastAPI:
    """Authenticated probe app: returns the live observability context."""
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


def _emitted_json(token: str, *, extra: dict) -> dict:
    """Run a record through ContextFilter + JsonFormatter and parse it."""
    filter_ = ContextFilter()
    record = logging.LogRecord(
        name="security.t8",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    assert filter_.filter(record) is True
    return json.loads(JsonFormatter(service="s", environment="e", version="v").format(record))


# ═══════════════════════════════════════════════════════════════════════════════
# 1-3. CLIENT-PROVIDED IDENTITY IS NEVER TRUSTED
# ═══════════════════════════════════════════════════════════════════════════════


class TestClientIdentitySpoofing:
    """Client-supplied tenant/venue/actor headers must not reach logs."""

    @pytest.mark.asyncio
    async def test_tenant_header_never_alters_context(self) -> None:
        app = _make_app()
        user_id = uuid.uuid4()
        spoofed_tenant = str(uuid.uuid4())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get(
                "/whoami",
                headers={**_auth_headers(user_id), "X-Tenant-Id": spoofed_tenant},
            )
        assert response.status_code == 200
        body = response.json()
        # Context tenant is the server-resolved tenant — never the spoofed header.
        assert body["context"]["tenant_id"] == body["tenant_id"]
        assert body["context"]["tenant_id"] != spoofed_tenant

    @pytest.mark.asyncio
    async def test_venue_header_never_alters_context(self) -> None:
        app = _make_app()
        spoofed_venue = str(uuid.uuid4())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get(
                "/whoami",
                headers={**_auth_headers(uuid.uuid4()), "X-Venue-Id": spoofed_venue},
            )
        body = response.json()
        assert body["context"].get("venue_id") != spoofed_venue

    @pytest.mark.asyncio
    async def test_actor_header_never_alters_context(self) -> None:
        app = _make_app()
        real_user = uuid.uuid4()
        spoofed_actor = str(uuid.uuid4())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get(
                "/whoami",
                headers={**_auth_headers(real_user), "X-Actor-Id": spoofed_actor},
            )
        body = response.json()
        # Context actor is derived from the verified token, not the header.
        assert body["context"]["actor_id"] == body["actor_id"] == str(real_user)
        assert body["context"]["actor_id"] != spoofed_actor

    @pytest.mark.asyncio
    async def test_spoofed_headers_not_echoed_in_response(self) -> None:
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get(
                "/whoami",
                headers={
                    **_auth_headers(uuid.uuid4()),
                    "X-Tenant-Id": str(uuid.uuid4()),
                    "X-Venue-Id": str(uuid.uuid4()),
                    "X-Actor-Id": str(uuid.uuid4()),
                },
            )
        # Only request/correlation/trace headers are ever echoed — never
        # identity headers (they would legitimize client-supplied identity).
        for header in ("x-tenant-id", "x-venue-id", "x-actor-id", "authorization"):
            assert header not in response.headers


# ═══════════════════════════════════════════════════════════════════════════════
# 4-5. CREDENTIALS NEVER REACH LOGS
# ═══════════════════════════════════════════════════════════════════════════════


class TestCredentialsNeverLogged:
    """Passwords, tokens and authorization headers must never be emitted."""

    def test_bearer_token_never_in_json_output(self) -> None:
        token = AuthService(_settings()).create_token(str(uuid.uuid4()))
        secret = "super-secret-password-42"
        payload = _emitted_json(
            token,
            extra={
                "authorization": f"Bearer {token}",
                "password": secret,
                "api_key": "sk-12345",
                "event_id": str(uuid.uuid4()),  # allowlisted — must survive
            },
        )
        rendered = json.dumps(payload)
        assert "Bearer" not in rendered
        assert token not in rendered
        assert secret not in rendered
        assert "sk-12345" not in rendered
        # The allowlisted field is emitted; the secrets are dropped.
        assert "event_id" in payload

    def test_request_context_surfaces_no_credentials(self) -> None:
        actor = _make_actor(venue_scope=frozenset({uuid.uuid4()}))
        tokens = bind_actor_context(actor)
        try:
            ctx = get_request_context()
        finally:
            unbind(tokens)
        rendered = json.dumps(ctx)
        assert "password" not in rendered
        assert "token" not in rendered
        assert "authorization" not in rendered
        assert ctx["actor_id"] == str(actor.actor_id)

    @pytest.mark.asyncio
    async def test_authenticated_request_response_has_no_credential_material(self) -> None:
        """End-to-end: the response body carries no credential material."""
        app = _make_app()
        user_id = uuid.uuid4()
        token = AuthService(_settings()).create_token(str(user_id))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        body = response.json()
        rendered = json.dumps(body)
        assert "Bearer" not in rendered
        assert token not in rendered
        # Sanity: the response still identifies the actor server-side.
        assert body["context"]["actor_id"] == str(user_id)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CONCURRENT ISOLATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentTenantIsolation:
    """Tenant context must never leak between concurrent requests."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_keep_own_tenant(self) -> None:
        app = _make_app()
        users = [uuid.uuid4() for _ in range(8)]

        async def hit(user_id: uuid.UUID) -> tuple[str, str]:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
                response = await client.get("/whoami", headers=_auth_headers(user_id))
            body = response.json()
            return body["context"]["actor_id"], body["context"]["tenant_id"]

        results = await asyncio.gather(*[hit(user) for user in users])
        # Actor isolation is proven at the HTTP layer: each request sees
        # exactly its own actor. (Distinct-tenant isolation is proven by
        # the direct bind_actor_context unit tests — the default no-lookup
        # builder resolves the same zero tenant for every token, so the
        # HTTP layer cannot vary tenants without dependency overrides.)
        seen_actors = [actor for actor, _ in results]
        assert seen_actors == [str(user) for user in users]
        # Context is fully cleared once every request has finished.
        assert get_request_context() == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 7. ANONYMOUS / SYSTEM SAFETY
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnonymousSafety:
    """Unauthenticated operations are represented without identity fields."""

    def test_no_actor_fields_outside_request(self) -> None:
        ctx = get_request_context()
        assert ctx == {}
        assert "actor_id" not in ctx
        assert "tenant_id" not in ctx
        assert "venue_id" not in ctx

    def test_anonymous_log_line_has_no_actor_fields(self) -> None:
        payload = _emitted_json("not-a-token", extra={})
        for key in ("actor_id", "tenant_id", "venue_id", "password", "token"):
            assert key not in payload

    @pytest.mark.asyncio
    async def test_unauthenticated_request_leaves_no_context(self) -> None:
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            response = await c.get("/whoami")
        assert response.status_code == 401  # auth behavior preserved
        assert get_request_context() == {}
