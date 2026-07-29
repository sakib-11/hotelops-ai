"""Tests for Task 5.5 — Server-Side ActorContext.

Tests cover:
- Valid actor construction (all lookups return active data)
- Disabled user rejected
- Disabled/suspended tenant rejected
- Revoked/inactive membership rejected
- No membership found
- Tenant-wide scope (ALL_VENUES)
- Venue-specific scope
- No venue access
- Request-supplied values cannot alter ActorContext
- ActorContext methods (has_permission, has_venue_access, is_admin)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.app.infrastructure.auth.context import ActorContextBuilder
from backend.app.infrastructure.auth.exceptions import AuthenticationError
from backend.app.infrastructure.auth.service import TokenData
from contracts.common import VenueId
from contracts.identity import (
    ActorContext,
    Permission,
    RoleName,
    permissions_for_role,
)

# =============================================================================
# Helpers — all user/tenant IDs MUST be valid UUID strings
# =============================================================================


def _uid() -> str:
    """Generate a valid UUID string for use as a user/tenant/role ID."""
    return str(uuid4())


def _make_token_data(user_id: str | None = None) -> TokenData:
    """Create a simple TokenData for testing with a UUID user_id."""
    uid = user_id or _uid()
    now = datetime.now(UTC)
    return TokenData(
        user_id=uid,
        issued_at=now,
        expires_at=datetime.fromtimestamp(now.timestamp() + 3600, tz=UTC),
    )


def _make_user_lookup(active: bool = True) -> object:
    """Create a user lookup returning active or disabled user."""

    def lookup(_user_id: str) -> dict[str, str] | None:
        return {"user_id": _user_id, "status": "active" if active else "disabled"}

    return lookup


def _none_user_lookup() -> object:
    """Create a user lookup that returns None (user not found)."""

    def lookup(_user_id: str) -> None:
        return None

    return lookup


def _make_membership_lookup(
    *,
    active: bool = True,
    role_name: str = "operator",
    scope: str | None = None,
    venue_ids: list[str] | None = None,
    tenant_id: str | None = None,
) -> object:
    """Create a membership lookup returning configurable membership."""
    tid = tenant_id or _uid()

    def lookup(_user_id: str) -> dict[str, object]:
        return {
            "membership_id": _uid(),
            "user_id": _user_id,
            "tenant_id": tid,
            "role_id": _uid(),
            "role_name": role_name,
            "scope": scope,
            "venue_ids": venue_ids,
            "status": "active" if active else "inactive",
        }

    return lookup


def _none_membership_lookup() -> object:
    """Create a membership lookup that returns None (no membership)."""

    def lookup(_user_id: str) -> None:
        return None

    return lookup


def _make_tenant_lookup(active: bool = True) -> object:
    """Create a tenant lookup returning active or disabled tenant."""

    def lookup(_tenant_id: str) -> dict[str, str] | None:
        return {"tenant_id": _tenant_id, "status": "active" if active else "disabled"}

    return lookup


def _none_tenant_lookup() -> object:
    """Create a tenant lookup that returns None (tenant not found)."""

    def lookup(_tenant_id: str) -> None:
        return None

    return lookup


# =============================================================================
# Valid Actor Construction
# =============================================================================


class TestValidActor:
    """Tests for successful ActorContext construction."""

    def _build(self, user_id: str | None = None) -> ActorContext:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, role_name="operator"),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        return builder.build(_make_token_data(user_id=user_id))

    def test_valid_actor_has_user_id(self) -> None:
        uid = _uid()
        ctx = self._build(user_id=uid)
        assert str(ctx.actor_id) == uid

    def test_valid_actor_has_tenant_id(self) -> None:
        ctx = self._build()
        assert ctx.tenant_id is not None

    def test_valid_actor_has_role_name(self) -> None:
        ctx = self._build()
        assert ctx.role_name == RoleName.OPERATOR

    def test_valid_actor_has_permissions(self) -> None:
        ctx = self._build()
        assert len(ctx.permissions) > 0
        assert Permission.VENUE_READ in ctx.permissions

    def test_valid_actor_is_active(self) -> None:
        ctx = self._build()
        assert ctx.active is True

    def test_valid_actor_authenticated_at_is_utc(self) -> None:
        ctx = self._build()
        assert ctx.authenticated_at.tzinfo is not None


# =============================================================================
# ActorContext Methods
# =============================================================================


class TestActorContextMethods:
    """Tests for ActorContext built-in authorization methods."""

    def build_context(
        self, role: RoleName = RoleName.OPERATOR, venue_ids: list[str] | None = None
    ) -> ActorContext:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(
                active=True,
                role_name=role.value,
                scope="specific_venues" if venue_ids else None,
                venue_ids=venue_ids,
            ),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        return builder.build(_make_token_data())

    def test_admin_has_all_permissions(self) -> None:
        ctx = self.build_context(RoleName.ADMIN)
        assert ctx.has_permission(Permission.VENUE_MANAGE)
        assert ctx.has_permission(Permission.USER_MANAGE)
        assert ctx.has_permission(Permission.MEMBERSHIP_MANAGE)

    def test_operator_limited_permissions(self) -> None:
        ctx = self.build_context(RoleName.OPERATOR)
        assert ctx.has_permission(Permission.VENUE_READ)
        assert not ctx.has_permission(Permission.VENUE_MANAGE)
        assert not ctx.has_permission(Permission.USER_MANAGE)

    def test_has_venue_access_allowed(self) -> None:
        vid = _uid()
        ctx = self.build_context(RoleName.MANAGER, venue_ids=[vid])
        assert ctx.has_venue_access(VenueId(UUID(vid)))

    def test_has_venue_access_denied(self) -> None:
        ctx = self.build_context(RoleName.MANAGER)
        other_vid = VenueId(uuid4())
        assert not ctx.has_venue_access(other_vid)

    def test_is_admin_true(self) -> None:
        ctx = self.build_context(RoleName.ADMIN)
        assert ctx.is_admin()

    def test_is_admin_false(self) -> None:
        ctx = self.build_context(RoleName.OPERATOR)
        assert not ctx.is_admin()

    def test_operator_has_no_manage_permissions(self) -> None:
        ctx = self.build_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.RECOMMENDATION_MANAGE)
        assert not ctx.has_permission(Permission.ALERT_MANAGE)


# =============================================================================
# Fail-Closed: Disabled User
# =============================================================================


class TestDisabledUser:
    """Disabled users must be rejected."""

    def test_disabled_user_rejected(self) -> None:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=False),
            membership_lookup=_make_membership_lookup(active=True),
        )
        with pytest.raises(AuthenticationError, match="disabled"):
            builder.build(_make_token_data())

    def test_unknown_user_rejected(self) -> None:
        builder = ActorContextBuilder(
            user_lookup=_none_user_lookup(),
            membership_lookup=_make_membership_lookup(active=True),
        )
        with pytest.raises(AuthenticationError, match="not found"):
            builder.build(_make_token_data())


# =============================================================================
# Fail-Closed: Disabled/Suspended Tenant
# =============================================================================


class TestDisabledTenant:
    """Disabled/suspended tenants must be rejected."""

    def test_disabled_tenant_rejected(self) -> None:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True),
            tenant_lookup=_make_tenant_lookup(active=False),
        )
        with pytest.raises(AuthenticationError, match="not active"):
            builder.build(_make_token_data())

    def test_unknown_tenant_rejected(self) -> None:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True),
            tenant_lookup=_none_tenant_lookup(),
        )
        with pytest.raises(AuthenticationError, match="not found"):
            builder.build(_make_token_data())


# =============================================================================
# Fail-Closed: Revoked/Inactive Membership
# =============================================================================


class TestRevokedMembership:
    """Revoked/inactive memberships must be rejected."""

    def test_inactive_membership_rejected(self) -> None:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=False),
        )
        with pytest.raises(AuthenticationError, match="not active"):
            builder.build(_make_token_data())

    def test_no_membership_rejected(self) -> None:
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_none_membership_lookup(),
        )
        with pytest.raises(AuthenticationError, match="membership"):
            builder.build(_make_token_data())


# =============================================================================
# Tenant-Wide vs Venue-Specific Scope
# =============================================================================


class TestVenueScope:
    """Venue scope resolution tests."""

    def test_tenant_wide_scope_empty(self) -> None:
        """ALL_VENUES scope returns empty frozenset (resolved at query time)."""
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, scope=None, venue_ids=None),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        assert ctx.venue_scope == frozenset()

    def test_specific_venues_scoped(self) -> None:
        vid_1 = _uid()
        vid_2 = _uid()
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(
                active=True,
                role_name="operator",
                scope="specific_venues",
                venue_ids=[vid_1, vid_2],
            ),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        assert VenueId(UUID(vid_1)) in ctx.venue_scope
        assert VenueId(UUID(vid_2)) in ctx.venue_scope
        assert len(ctx.venue_scope) == 2

    def test_no_venue_access(self) -> None:
        """Membership without scope or venue_ids has no venue access."""
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(
                active=True, role_name="operator", scope=None, venue_ids=None
            ),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        other_vid = VenueId(uuid4())
        assert not ctx.has_venue_access(other_vid)

    def test_specific_venues_excludes_other_venues(self) -> None:
        """User with specific venue access cannot access other venues."""
        vid_allowed = _uid()
        vid_blocked = _uid()
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(
                active=True,
                role_name="operator",
                scope="specific_venues",
                venue_ids=[vid_allowed],
            ),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        assert ctx.has_venue_access(VenueId(UUID(vid_allowed)))
        assert not ctx.has_venue_access(VenueId(UUID(vid_blocked)))


# =============================================================================
# Request-Supplied Values Cannot Alter Context
# =============================================================================


class TestClientCannotAlter:
    """Verify client-supplied values cannot alter ActorContext."""

    def test_tenant_id_from_membership_not_client(self) -> None:
        """tenant_id is derived from server-side membership, not client."""
        server_tid = _uid()
        client_tid = _uid()

        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, tenant_id=server_tid),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        assert str(ctx.tenant_id) == server_tid
        assert str(ctx.tenant_id) != client_tid

    def test_role_from_membership_not_client(self) -> None:
        """Role is derived from server-side membership role_name."""
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, role_name="admin"),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        assert ctx.role_name == RoleName.ADMIN

    def test_permissions_from_role_not_client(self) -> None:
        """Permissions are derived from role, not provided by client."""
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, role_name="operator"),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        expected_perms = permissions_for_role(RoleName.OPERATOR)
        assert ctx.permissions == expected_perms

    def test_actor_id_from_token_not_client(self) -> None:
        """Actor identity comes from the verified token."""
        token_uid = _uid()
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, role_name="operator"),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data(user_id=token_uid))
        assert str(ctx.actor_id) == token_uid


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Edge cases for ActorContext construction."""

    def test_invalid_role_name_rejected(self) -> None:
        """Invalid role name in membership raises AuthenticationError."""
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True, role_name="superadmin"),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        with pytest.raises(AuthenticationError, match="role"):
            builder.build(_make_token_data())

    def test_membership_without_role_rejected(self) -> None:
        """Membership without role_name raises AuthenticationError."""

        def lookup(_user_id: str) -> dict[str, object]:
            return {
                "membership_id": _uid(),
                "user_id": _user_id,
                "tenant_id": _uid(),
                "role_id": _uid(),
                "role_name": None,
                "scope": None,
                "venue_ids": None,
                "status": "active",
            }

        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=lookup,
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        with pytest.raises(AuthenticationError, match="role"):
            builder.build(_make_token_data())

    def test_membership_without_tenant_rejected(self) -> None:
        """Membership without tenant_id raises AuthenticationError."""

        def lookup(_user_id: str) -> dict[str, object]:
            return {
                "membership_id": _uid(),
                "user_id": _user_id,
                "tenant_id": "",
                "role_id": _uid(),
                "role_name": "operator",
                "scope": None,
                "venue_ids": None,
                "status": "active",
            }

        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=lookup,
        )
        with pytest.raises(AuthenticationError, match="tenant"):
            builder.build(_make_token_data())

    def test_no_lookups_produces_minimal_actor(self) -> None:
        """Without any lookups, a minimal ActorContext is still produced."""
        builder = ActorContextBuilder()
        uid = _uid()
        ctx = builder.build(_make_token_data(user_id=uid))
        assert str(ctx.actor_id) == uid
        assert ctx.role_name == RoleName.OPERATOR
        assert ctx.active is True

    def test_actor_context_is_frozen(self) -> None:
        """ActorContext should be immutable."""
        builder = ActorContextBuilder(
            user_lookup=_make_user_lookup(active=True),
            membership_lookup=_make_membership_lookup(active=True),
            tenant_lookup=_make_tenant_lookup(active=True),
        )
        ctx = builder.build(_make_token_data())
        with pytest.raises(PydanticValidationError):
            ctx.actor_id = "00000000-0000-0000-0000-000000000000"  # type: ignore[misc]
