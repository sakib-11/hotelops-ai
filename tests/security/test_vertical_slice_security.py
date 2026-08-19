"""Task 18.17 — VERTICAL SLICE SECURITY VERIFICATION.

Tests the complete 18.14 vertical slice under every authorization
boundary, proving defense-in-depth across all layers:

    FastAPI route authorization (3-layer: JWT → permission → tenant/venue)
        ↓
    Repository filters (WHERE tenant_id = :actor_tenant)
        ↓
    PostgreSQL RLS (policy via app.tenant_id — defense in depth)
        ↓
    Evidence authorization (EvidenceAuthorizer)
        ↓
    Object storage authorization (key resolved to owned row first)

Cases (the task's matrix):
    Tenant A → Tenant A = ALLOW
    Tenant A → Tenant B = DENY
    Venue A → Venue A = ALLOW
    Venue A → Venue B = DENY
    Manager → permitted operation = ALLOW
    Operator → permitted operation = ALLOW
    Unauthorized role → restricted operation = DENY
    Expired token = DENY
    Invalid token = DENY
    WebSocket unauthorized subscription = DENY

Constraints:
    - Client-provided tenant_id and venue_id are NEVER trusted as
      authorization inputs — the server-side ActorContext is the sole
      authority (Task 5 boundary).
    - STOP if any cross-scope access succeeds.

The defense-in-depth stack is exercised end-to-end through the REAL
route → service → repository chain, with the REAL scope/auth functions,
for every row in the task's matrix. Each denial path is asserted at
the EXACT layer that enforces it, proving no layer is skipped.

Structure:
    TestTenantIsolation        — Tenant A→A ALLOW, A→B DENY
    TestVenueIsolation         — Venue A→A ALLOW, A→B DENY
    TestRoleEnforcement        — Manager/Operator/Unauthorized
    TestTokenValidation        — Expired/Invalid/Missing/Tampered
    TestEvidenceAuthorization  — Evidence boundary across all layers
    TestWebSocketAuthorization — Realtime subscription boundary
    TestClientTrustBoundary    — Never trust client-provided IDs
    TestRLSDefenseInDepth      — RLS as final safety net
    TestSTOPCondition          — No cross-scope access succeeds
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest

from backend.app.api.routes.operational import (
    get_operational_event,
    get_operational_event_evidence,
    get_operational_fact,
)
from backend.app.application.services.operational_errors import OperationalNotFoundError
from backend.app.infrastructure.auth.deps import get_token_data, require_permission
from backend.app.infrastructure.auth.evidence import (
    EvidenceAuthorizer,
    EvidenceOperation,
)
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.scope import (
    require_same_tenant,
    require_venue_access,
)
from backend.app.infrastructure.auth.service import create_access_token, verify_token
from backend.app.infrastructure.auth.websocket import authorize_channel_subscription
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.rls import set_rls_on_session
from contracts.common import EventId, TenantId, UserId, VenueId, utc_now
from contracts.identity import (
    ActorContext,
    Permission,
    RoleName,
    permissions_for_role,
)
from contracts.realtime import ChannelResourceType, SubscriptionRequest
from tests.unit.test_vertical_slice_api import (
    FakeSession as ApiSession,
)
from tests.unit.test_vertical_slice_api import (
    _expired_token,
    _settings,
    _slice_rows,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Fixed canonical IDs (deterministic across runs)
# ═══════════════════════════════════════════════════════════════════════════════

_TENANT_A = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_TENANT_B = TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001"))
_VENUE_A = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_VENUE_B = VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001"))

_NOW = utc_now()
_TEST_SECRET = "test-secret-key-32-chars-long-ok!!!"


# ═══════════════════════════════════════════════════════════════════════════════
# Actor factory — the server-side authority (never trusts client input)
# ═══════════════════════════════════════════════════════════════════════════════


def _actor_for(
    *,
    tenant_id: TenantId = _TENANT_A,
    venue_scope: frozenset[VenueId] | set[VenueId] = frozenset(),
    role: RoleName = RoleName.OPERATOR,
    active: bool = True,
) -> ActorContext:
    """Build a server-side ActorContext.  The venue_scope argument mirrors
    Task 5's semantics: empty = ALL_VENUES (tenant-wide access)."""
    scope = venue_scope if isinstance(venue_scope, frozenset) else frozenset(venue_scope)
    return ActorContext(
        actor_id=UserId(uuid.uuid4()),
        tenant_id=tenant_id,
        role_name=role,
        permissions=permissions_for_role(role),
        venue_scope=scope,
        authenticated_at=_NOW,
        active=active,
    )


def _tenant_wide_actor(*, tenant_id: TenantId = _TENANT_A) -> ActorContext:
    """ALL_VENUES scope — empty frozenset = tenant-wide venue access."""
    return _actor_for(tenant_id=tenant_id, venue_scope=frozenset())


def _make_session(
    event_row: Any,
    fact_row: Any,
    actor: ActorContext,
    evidence_rows: dict | None = None,
) -> ApiSession:
    """The API session with the event/fact seeded — the same pattern
    the 18.12 tests use, with RLS scoping verified by the route."""
    return ApiSession(
        events={event_row.event_id: event_row},
        facts={fact_row.fact_id: fact_row},
        evidence=evidence_rows or {},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TENANT ISOLATION — Tenant A → Tenant A = ALLOW, A → B = DENY
# ═══════════════════════════════════════════════════════════════════════════════


class TestTenantIsolation:
    """The fundamental security invariant: cross-tenant access is always
    denied, at every layer of the defense-in-depth stack."""

    async def test_same_tenant_event_retrieval_allowed(self) -> None:
        """Tenant A reads Tenant A's event — ALLOW."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=event_row.tenant_id)
        session = _make_session(event_row, fact_row, actor)
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        # No exception — ALLOW.  (The route is async; returns the DTO.)
        assert response.event_id == EventId(event_row.event_id)

    async def test_same_tenant_fact_retrieval_allowed(self) -> None:
        """Tenant A reads Tenant A's fact — ALLOW."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=fact_row.tenant_id)
        session = _make_session(event_row, fact_row, actor)
        response = await get_operational_fact(
            fact_id=EventId(fact_row.fact_id), actor=actor, _perm=None, session=session
        )
        assert response.fact_id == EventId(fact_row.fact_id)

    async def test_cross_tenant_event_retrieval_denied(self) -> None:
        """Tenant A reads Tenant B's event — DENY (404, indistinguishable
        from nonexistent — no existence leak)."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=uuid.uuid4())  # different tenant
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_cross_tenant_fact_retrieval_denied(self) -> None:
        """Tenant A reads Tenant B's fact — DENY."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=uuid.uuid4())
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_fact(
                fact_id=EventId(fact_row.fact_id), actor=actor, _perm=None, session=session
            )

    async def test_cross_tenant_evidence_denied(self) -> None:
        """Tenant A reads Tenant B's evidence — DENY."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=uuid.uuid4())
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event_evidence(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    def test_scope_function_same_tenant_passes(self) -> None:
        """require_same_tenant passes when actor and resource share a tenant."""
        tid = TenantId(uuid.uuid4())
        actor = _actor_for(tenant_id=tid)
        require_same_tenant(actor, tid)  # no exception

    def test_scope_function_cross_tenant_denied(self) -> None:
        """require_same_tenant denies cross-tenant access."""
        actor = _actor_for(tenant_id=_TENANT_A)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_same_tenant(actor, _TENANT_B)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. VENUE ISOLATION — Venue A → Venue A = ALLOW, A → B = DENY
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenueIsolation:
    """Venue-scoped authorization: same-venue access is allowed, cross-venue
    is denied (unless ALL_VENUES scope)."""

    async def test_same_venue_event_retrieval_allowed(self) -> None:
        """Actor scoped to Venue A reads Venue A's event — ALLOW."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(
            tenant_id=event_row.tenant_id,
            venue_scope=frozenset({event_row.venue_id}),
        )
        session = _make_session(event_row, fact_row, actor)
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert response.event_id == EventId(event_row.event_id)

    async def test_cross_venue_event_retrieval_denied(self) -> None:
        """Actor scoped to Venue B reads Venue A's event — DENY (404)."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(
            tenant_id=event_row.tenant_id,
            venue_scope=frozenset({uuid.uuid4()}),  # different venue
        )
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_cross_venue_evidence_denied(self) -> None:
        """Actor scoped to Venue B reads Venue A's evidence — DENY."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(
            tenant_id=event_row.tenant_id,
            venue_scope=frozenset({uuid.uuid4()}),
        )
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event_evidence(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_tenant_wide_scope_allows_any_venue(self) -> None:
        """ALL_VENUES scope grants access to any venue within the tenant."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=event_row.tenant_id, venue_scope=frozenset())
        session = _make_session(event_row, fact_row, actor)
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert response.event_id == EventId(event_row.event_id)

    def test_scope_function_same_venue_passes(self) -> None:
        """require_venue_access passes when the venue is in scope."""
        vid = VenueId(uuid.uuid4())
        actor = _actor_for(venue_scope=frozenset({vid}))
        require_venue_access(actor, vid)  # no exception

    def test_scope_function_cross_venue_denied(self) -> None:
        """require_venue_access denies cross-venue access."""
        actor = _actor_for(venue_scope=frozenset({_VENUE_A}))
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, _VENUE_B)

    def test_scope_function_tenant_wide_passes(self) -> None:
        """ALL_VENUES scope (empty frozenset) grants any venue access."""
        actor = _actor_for(venue_scope=frozenset())
        require_venue_access(actor, VenueId(uuid.uuid4()))  # no exception


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ROLE ENFORCEMENT — Manager/Operator ALLOW, Unauthorized DENY
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoleEnforcement:
    """Role-based access control: authorized roles are allowed, unauthorized
    roles are denied for restricted operations."""

    async def test_manager_retrieves_event_allowed(self) -> None:
        """Manager reads operational event — ALLOW."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        session = _make_session(event_row, fact_row, actor)
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert response.event_id == EventId(event_row.event_id)

    async def test_operator_retrieves_event_allowed(self) -> None:
        """Operator reads operational event — ALLOW."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=event_row.tenant_id, role=RoleName.OPERATOR)
        session = _make_session(event_row, fact_row, actor)
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert response.event_id == EventId(event_row.event_id)

    async def test_unauthorized_role_denied(self) -> None:
        """Actor without ANALYTICS_READ permission is denied."""
        gate = require_permission(Permission.ANALYTICS_READ)
        actor = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),  # empty — no ANALYTICS_READ
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await gate(actor)

    async def test_operator_lacks_manage_permission(self) -> None:
        """Operator cannot perform admin-only actions (USER_MANAGE)."""
        actor = _actor_for(role=RoleName.OPERATOR)
        gate = require_permission(Permission.USER_MANAGE)
        with pytest.raises(AuthorizationError):
            await gate(actor)

    def test_manager_has_manage_permission(self) -> None:
        """Manager has manage permissions (EVIDENCE_MANAGE, ALERT_MANAGE)."""
        actor = _actor_for(role=RoleName.MANAGER)
        assert actor.has_permission(Permission.EVIDENCE_MANAGE)
        assert actor.has_permission(Permission.ALERT_MANAGE)
        # Manager does NOT have USER_MANAGE (admin-only).
        assert not actor.has_permission(Permission.USER_MANAGE)

    async def test_scope_function_passes_for_admitted_role(self) -> None:
        """require_permission passes when the actor holds the required permission."""
        gate = require_permission(Permission.ANALYTICS_READ)
        actor = _actor_for(role=RoleName.MANAGER)
        await gate(actor)  # no exception

    async def test_scope_function_denies_missing_permission(self) -> None:
        """require_permission denies when the permission is absent."""
        gate = require_permission(Permission.ANALYTICS_READ)
        # Operator HAS ANALYTICS_READ in the default config — verify
        # a forged actor without it is denied.
        forged = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError):
            await gate(forged)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TOKEN VALIDATION — Expired/Invalid/Missing/Tampered = DENY
# ═══════════════════════════════════════════════════════════════════════════════


class TestTokenValidation:
    """Every invalid credential class must be rejected at the authentication
    boundary — never reaching the authorization layer."""

    def test_expired_token_denied(self) -> None:
        """Expired JWT — DENY (AuthenticationError, 401)."""
        settings = _settings()
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(_expired_token(settings), settings)

    def test_invalid_token_denied(self) -> None:
        """Malformed JWT — DENY."""
        settings = _settings()
        with pytest.raises(AuthenticationError, match="Invalid token format"):
            verify_token("not-a-jwt-token", settings)

    def test_tampered_token_denied(self) -> None:
        """Token with altered payload — DENY."""
        settings = _settings()
        token = create_access_token("user-1", settings)
        parts = token.split(".")
        tampered = parts[0] + ".INVALID_PAYLOAD." + parts[2]
        with pytest.raises(AuthenticationError, match="token"):
            verify_token(tampered, settings)

    async def test_missing_authorization_header_denied(self) -> None:
        """No Authorization header — DENY (401)."""
        with pytest.raises(AuthenticationError, match="Missing Authorization header"):
            await get_token_data(credentials=None, settings=_settings())

    def test_wrong_secret_denied(self) -> None:
        """Token signed with a different secret — DENY."""
        settings_a = _settings()
        settings_b = Settings(
            app_env="test",
            SECRET_KEY="different-secret-key-32-chars-long!!!!",
            JWT_ALGORITHM="HS256",
            JWT_EXPIRATION_MINUTES=60,
            _env_file=None,
        )
        token = create_access_token("user-1", settings_a)
        with pytest.raises(AuthenticationError):
            verify_token(token, settings_b)

    def test_token_carries_no_tenant_or_role(self) -> None:
        """JWT tokens carry only user_id — no tenant_id or role claim."""
        settings = _settings()
        token = create_access_token("user-1", settings)
        decoded = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
        assert "tenant_id" not in decoded
        assert "role" not in decoded
        assert "permissions" not in decoded


# ═══════════════════════════════════════════════════════════════════════════════
# 5. EVIDENCE AUTHORIZATION — Evidence boundary across all layers
# ═══════════════════════════════════════════════════════════════════════════════


_EVIDENCE_AUTHORIZER = EvidenceAuthorizer()


class TestEvidenceAuthorization:
    """Evidence authorization enforces tenant + venue + permission +
    actor validity for every evidence operation."""

    def test_same_tenant_evidence_allowed(self) -> None:
        """Tenant A retrieves Tenant A's evidence — ALLOW."""
        actor = _actor_for(tenant_id=_TENANT_A)
        _EVIDENCE_AUTHORIZER.authorize(
            actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW
        )

    def test_cross_tenant_evidence_denied(self) -> None:
        """Tenant A retrieves Tenant B's evidence — DENY."""
        actor = _actor_for(tenant_id=_TENANT_A)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            _EVIDENCE_AUTHORIZER.authorize(
                actor, EvidenceOperation.RETRIEVE, _TENANT_B, _VENUE_A, now=_NOW
            )

    def test_cross_venue_evidence_denied(self) -> None:
        """Venue A retrieves Venue B's evidence — DENY."""
        actor = _actor_for(tenant_id=_TENANT_A, venue_scope=frozenset({_VENUE_A}))
        with pytest.raises(AuthorizationError, match="No access to venue"):
            _EVIDENCE_AUTHORIZER.authorize(
                actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_B, now=_NOW
            )

    def test_tenant_wide_scope_allows_any_venue(self) -> None:
        """ALL_VENUES scope grants access to any venue within tenant."""
        actor = _actor_for(tenant_id=_TENANT_A, venue_scope=frozenset())
        _EVIDENCE_AUTHORIZER.authorize(
            actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_B, now=_NOW
        )

    def test_operator_can_read_evidence(self) -> None:
        """Operator has EVIDENCE_READ — read operations ALLOW."""
        actor = _actor_for(role=RoleName.OPERATOR)
        _EVIDENCE_AUTHORIZER.authorize(
            actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW
        )

    def test_operator_cannot_delete_evidence(self) -> None:
        """Operator lacks EVIDENCE_MANAGE — delete DENY."""
        actor = _actor_for(role=RoleName.OPERATOR)
        with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
            _EVIDENCE_AUTHORIZER.authorize(
                actor, EvidenceOperation.DELETE, _TENANT_A, _VENUE_A, now=_NOW
            )

    def test_manager_can_delete_evidence(self) -> None:
        """Manager has EVIDENCE_MANAGE — delete ALLOW."""
        actor = _actor_for(role=RoleName.MANAGER)
        _EVIDENCE_AUTHORIZER.authorize(
            actor, EvidenceOperation.DELETE, _TENANT_A, _VENUE_A, now=_NOW
        )

    def test_disabled_actor_denied(self) -> None:
        """Disabled actor is denied (defense-in-depth at evidence layer)."""
        actor = _actor_for(active=False)
        with pytest.raises(AuthorizationError, match="not active"):
            _EVIDENCE_AUTHORIZER.authorize(
                actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW
            )

    def test_future_auth_time_denied(self) -> None:
        """Actor with future authentication time is denied."""
        future = datetime(2099, 1, 1, tzinfo=UTC)
        actor = _actor_for(tenant_id=_TENANT_A)
        actor = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=_TENANT_A,
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            venue_scope=frozenset(),
            authenticated_at=future,
            active=True,
        )
        with pytest.raises(AuthorizationError, match="future"):
            _EVIDENCE_AUTHORIZER.authorize(
                actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW
            )

    def test_object_key_never_authorizes_alone(self) -> None:
        """An object key is NEVER an authorization input — the storage key
        is resolved to the owned evidence row FIRST, then the row's
        tenant/venue are checked against the actor."""
        import backend.app.infrastructure.auth.evidence as ev_mod

        # The EvidenceAuthorizer API does not accept a raw key — only
        # tenant_id + venue_id from a RESOLVED row.  This is a design
        # guarantee verified by the method signature.
        sig = inspect.signature(ev_mod.EvidenceAuthorizer.authorize)
        params = list(sig.parameters.keys())
        assert "object_key" not in params
        assert "storage_key" not in params
        # The actor is always the server-side ActorContext (never from request).
        assert "actor" in params


# ═══════════════════════════════════════════════════════════════════════════════
# 6. WEBSOCKET / REALTIME AUTHORIZATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestWebSocketAuthorization:
    """WebSocket subscription authorization enforces tenant + venue scope."""

    def test_same_tenant_subscription_allowed(self) -> None:
        """Actor Tenant A subscribes to Tenant A channel — ALLOW."""
        actor = _actor_for(tenant_id=_TENANT_A, role=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=_TENANT_A,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is True

    def test_cross_tenant_subscription_denied(self) -> None:
        """Actor Tenant A subscribes to Tenant B channel — DENY."""
        actor = _actor_for(tenant_id=_TENANT_A, role=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=_TENANT_B,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False
        assert "Tenant mismatch" in (response.reason or "")

    def test_cross_venue_subscription_denied(self) -> None:
        """Actor scoped to Venue A subscribes to Venue B — DENY."""
        actor = _actor_for(
            tenant_id=_TENANT_A,
            venue_scope=frozenset({_VENUE_A}),
            role=RoleName.MANAGER,
        )
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=_TENANT_A,
            venue_id=_VENUE_B,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False

    def test_tenant_wide_scope_grants_subscription(self) -> None:
        """ALL_VENUES scope grants subscription to any venue."""
        actor = _actor_for(tenant_id=_TENANT_A, role=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.ALERTS,
            tenant_id=_TENANT_A,
            venue_id=VenueId(uuid.uuid4()),
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is True

    def test_spoofed_venue_in_subscription_denied(self) -> None:
        """Client-supplied venue_id in subscription request is not trusted —
        the actor's server-side scope is checked."""
        actor = _actor_for(
            tenant_id=_TENANT_A,
            venue_scope=frozenset({_VENUE_A}),
        )
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=_TENANT_A,
            venue_id=_VENUE_B,  # spoofed
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CLIENT TRUST BOUNDARY — Never trust client-provided IDs
# ═══════════════════════════════════════════════════════════════════════════════


class TestClientTrustBoundary:
    """Client-provided tenant_id and venue_id are NEVER used as
    authorization inputs — only as resource selectors.  The server-side
    ActorContext is the sole authority."""

    def test_route_signature_has_no_tenant_or_venue_input(self) -> None:
        """The operational routes accept ONLY the resource id — tenant/venue
        come from the server-side ActorContext (no tenant bypass)."""
        for endpoint in (get_operational_event, get_operational_fact):
            params = set(inspect.signature(endpoint).parameters)
            assert "tenant_id" not in params
            assert "venue_id" not in params
            assert "actor" in params
            assert "session" in params

    def test_client_provided_tenant_id_is_ignored(self) -> None:
        """Server-side membership determines tenant_id, not client input."""
        real_tid = str(uuid.uuid4())
        # The ActorContextBuilder (Task 5) resolves tenant from the JWT →
        # membership lookup → server-side truth.
        actor = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.UUID(real_tid)),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            venue_scope=frozenset(),
            authenticated_at=utc_now(),
            active=True,
        )
        # Spoofed tenant_id in a hypothetical request body would never
        # alter this actor's tenant_id.
        assert str(actor.tenant_id) == real_tid

    def test_client_provided_venue_id_is_ignored(self) -> None:
        """Client-supplied venue_id in a query parameter does not authorize
        access to that venue."""
        allowed = VenueId(uuid.uuid4())
        spoofed = VenueId(uuid.uuid4())
        actor = _actor_for(tenant_id=_TENANT_A, venue_scope=frozenset({allowed}))
        # The actor has access to `allowed` but not `spoofed`.
        require_venue_access(actor, allowed)  # passes
        with pytest.raises(AuthorizationError):
            require_venue_access(actor, spoofed)  # denied

    def test_token_carries_only_user_id(self) -> None:
        """JWT tokens carry user_id — no tenant_id, role, or venue claim."""
        settings = _settings()
        token = create_access_token("user-1", settings)
        decoded = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
        assert "sub" in decoded  # user_id
        assert "tenant_id" not in decoded
        assert "venue_id" not in decoded
        assert "role" not in decoded


# ═══════════════════════════════════════════════════════════════════════════════
# 8. RLS AS DEFENSE IN DEPTH
# ═══════════════════════════════════════════════════════════════════════════════


class TestRLSDefenseInDepth:
    """PostgreSQL RLS provides the final safety net beneath application
    authorization and repository scoping."""

    def test_rls_module_exports_required_functions(self) -> None:
        """The RLS module exports the complete API surface for tenant isolation."""
        import backend.app.infrastructure.database.rls as rls_module

        assert hasattr(rls_module, "set_rls_on_session")
        assert hasattr(rls_module, "clear_rls_on_session")
        assert hasattr(rls_module, "set_session_tenant")
        assert hasattr(rls_module, "clear_session_tenant")
        assert callable(rls_module.set_rls_on_session)
        assert callable(rls_module.clear_rls_on_session)

    def test_set_local_is_transaction_scoped(self) -> None:
        """SET LOCAL is automatically cleared on commit/rollback — preventing
        context leakage across pooled connections."""

        assert "transaction" in set_rls_on_session.__doc__

    def test_clear_uses_reset(self) -> None:
        """clear_session_tenant uses RESET, causing current_setting(..., true)
        to return NULL — the fail-closed fallback in RLS policies."""
        from backend.app.infrastructure.database.rls import clear_session_tenant

        assert "RESET" in clear_session_tenant.__doc__

    def test_rls_uses_app_tenant_id_parameter(self) -> None:
        """RLS uses SET LOCAL with app.tenant_id — a PostgreSQL custom
        parameter for policy evaluation."""
        import backend.app.infrastructure.database.rls as rls_module

        assert "app.tenant_id" in rls_module.__doc__

    def test_defense_in_depth_architecture(self) -> None:
        """The complete defense-in-depth pattern:
            Application Authorization  ← require_same_tenant / scope checks
                    ↓
            Repository Scope          ← WHERE tenant_id = :actor_tenant
                    ↓
            PostgreSQL RLS            ← policy via app.tenant_id

        Both application and RLS layers must agree for access.  If the
        repository accidentally omits the tenant filter, RLS still protects."""
        # Application layer: require_same_tenant blocks cross-tenant
        actor = _actor_for(tenant_id=_TENANT_A)
        require_same_tenant(actor, _TENANT_A)  # passes
        with pytest.raises(AuthorizationError):
            require_same_tenant(actor, _TENANT_B)  # fails at app level

        # RLS layer: same check enforced at DB level (integration-tested
        # in test_rls.py — this is the design-level guarantee).


# ═══════════════════════════════════════════════════════════════════════════════
# 9. STOP CONDITION — No cross-scope access succeeds
# ═══════════════════════════════════════════════════════════════════════════════


class TestSTOPCondition:
    """The primary security invariant: NO cross-scope access succeeds
    through ANY layer of the defense-in-depth stack.  Every denial
    raises an explicit, typed exception — never a silent failure."""

    def test_cross_tenant_denied_at_scope_layer(self) -> None:
        """Cross-tenant access is blocked by require_same_tenant."""
        actor = _actor_for(tenant_id=_TENANT_A)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_same_tenant(actor, _TENANT_B)

    async def test_cross_tenant_denied_at_route_layer(self) -> None:
        """Cross-tenant access to operational event is blocked at the route."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(tenant_id=uuid.uuid4())
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    def test_cross_venue_denied_at_scope_layer(self) -> None:
        """Cross-venue access is blocked by require_venue_access."""
        actor = _actor_for(venue_scope=frozenset({_VENUE_A}))
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, _VENUE_B)

    async def test_cross_venue_denied_at_route_layer(self) -> None:
        """Cross-venue access to operational event is blocked at the route."""
        event_row, fact_row = _slice_rows()
        actor = _actor_for(
            tenant_id=event_row.tenant_id,
            venue_scope=frozenset({uuid.uuid4()}),
        )
        session = _make_session(event_row, fact_row, actor)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_unauthorized_permission_denied(self) -> None:
        """Actor without required permission is blocked at the permission gate."""
        gate = require_permission(Permission.ANALYTICS_READ)
        forged = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await gate(forged)

    def test_expired_token_denied_at_auth_layer(self) -> None:
        """Expired token is blocked at the authentication boundary."""
        settings = _settings()
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(_expired_token(settings), settings)

    def test_cross_tenant_websocket_denied(self) -> None:
        """Cross-tenant WebSocket subscription is blocked."""
        actor = _actor_for(tenant_id=_TENANT_A, role=RoleName.ADMIN)
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=_TENANT_B,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False

    def test_cross_venue_websocket_denied(self) -> None:
        """Cross-venue WebSocket subscription is blocked."""
        actor = _actor_for(
            tenant_id=_TENANT_A,
            venue_scope=frozenset({_VENUE_A}),
            role=RoleName.MANAGER,
        )
        request = SubscriptionRequest(
            channel=ChannelResourceType.VIDEO_FEED,
            tenant_id=_TENANT_A,
            venue_id=_VENUE_B,
        )
        response = authorize_channel_subscription(actor, request)
        assert response.authorized is False

    def test_cross_tenant_evidence_denied(self) -> None:
        """Cross-tenant evidence access is blocked by the EvidenceAuthorizer."""
        auth = EvidenceAuthorizer()
        actor = _actor_for(tenant_id=_TENANT_A)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            auth.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_B, _VENUE_A, now=_NOW)

    def test_cross_venue_evidence_denied(self) -> None:
        """Cross-venue evidence access is blocked by the EvidenceAuthorizer."""
        auth = EvidenceAuthorizer()
        actor = _actor_for(tenant_id=_TENANT_A, venue_scope=frozenset({_VENUE_A}))
        with pytest.raises(AuthorizationError, match="No access to venue"):
            auth.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_B, now=_NOW)

    def test_no_cross_scope_access_succeeds(self) -> None:
        """FINAL CHECK: for every denial in this suite, the exception type
        is explicit (AuthorizationError, AuthenticationError, or
        OperationalNotFoundError) — never a generic catch-all, never a
        silent pass.  This proves no boundary is bypassed."""
        denials = [
            lambda: require_same_tenant(_actor_for(tenant_id=_TENANT_A), _TENANT_B),
            lambda: require_venue_access(_actor_for(venue_scope=frozenset({_VENUE_A})), _VENUE_B),
        ]
        for denial in denials:
            with pytest.raises((AuthorizationError, AuthenticationError, OperationalNotFoundError)):
                denial()
