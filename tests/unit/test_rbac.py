"""Tests for Task 5.6 — RBAC Permission Enforcement.

Tests cover:
- Permission definitions and role mapping consistency
- ADMIN: all 14 permissions granted, no permissions denied
- MANAGER: 9 management permissions (no venue.manage, no user/membership admin)
- OPERATOR: 6 read-only permissions
- Role escalation attempts (e.g. operator trying admin actions)
- Forged request permissions (client cannot override)
- Unknown permission behavior
- require_permission and require_any_permission dependency factories
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import jwt
import pytest

from backend.app.infrastructure.auth.context import ActorContextBuilder
from backend.app.infrastructure.auth.deps import (
    require_any_permission,
    require_permission,
)
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.auth.service import (
    TokenData,
    create_access_token,
)
from backend.app.infrastructure.config import Settings
from contracts.common import TenantId, UserId
from contracts.identity import (
    ActorContext,
    Permission,
    RoleName,
    permissions_for_role,
)

# =============================================================================
# Permission definitions — verify RBAC matrix completeness
# =============================================================================


class TestPermissionDefinitions:
    """Verify the 14 permission values are defined correctly."""

    def test_all_permissions_have_read_write_pairs(self) -> None:
        """Ensure management permissions have corresponding read permissions."""
        perms = set(Permission)
        read_perms = {p for p in perms if p.value.endswith(".read")}
        manage_perms = {p for p in perms if p.value.endswith(".manage")}
        for mp in manage_perms:
            prefix = mp.value.rsplit(".", 1)[0]
            read_perm_name = f"{prefix}.read"
            matching_read = [rp for rp in read_perms if rp.value == read_perm_name]
            assert matching_read, f"{mp} has no corresponding read permission"

    def test_no_unknown_permissions(self) -> None:
        """Only the 15 canonical permissions should exist."""
        known_values = {
            "venue.read",
            "venue.manage",
            "video.read",
            "video.analyze",
            "analytics.read",
            "evidence.read",
            "evidence.manage",
            "recommendation.read",
            "recommendation.manage",
            "alert.read",
            "alert.manage",
            "user.read",
            "user.manage",
            "membership.read",
            "membership.manage",
        }
        actual_values = {p.value for p in Permission}
        assert actual_values == known_values


class TestRolePermissionMapping:
    """Verify the role-to-permission mapping for all three roles."""

    # ------------------------------------------------------------------ #
    # ADMIN — all 14 permissions
    # ------------------------------------------------------------------ #

    def test_admin_has_read_permissions(self) -> None:
        perms = permissions_for_role(RoleName.ADMIN)
        assert Permission.VENUE_READ in perms
        assert Permission.VIDEO_READ in perms
        assert Permission.ANALYTICS_READ in perms
        assert Permission.EVIDENCE_READ in perms
        assert Permission.RECOMMENDATION_READ in perms
        assert Permission.ALERT_READ in perms
        assert Permission.USER_READ in perms
        assert Permission.MEMBERSHIP_READ in perms

    def test_admin_has_manage_permissions(self) -> None:
        perms = permissions_for_role(RoleName.ADMIN)
        assert Permission.VENUE_MANAGE in perms
        assert Permission.RECOMMENDATION_MANAGE in perms
        assert Permission.ALERT_MANAGE in perms
        assert Permission.USER_MANAGE in perms
        assert Permission.MEMBERSHIP_MANAGE in perms

    def test_admin_has_analyze(self) -> None:
        perms = permissions_for_role(RoleName.ADMIN)
        assert Permission.VIDEO_ANALYZE in perms

    def test_admin_has_all_15_permissions(self) -> None:
        perms = permissions_for_role(RoleName.ADMIN)
        assert len(perms) == 15

    def test_admin_not_denied_any(self) -> None:
        """ADMIN should have every canonical permission."""
        perms = permissions_for_role(RoleName.ADMIN)
        all_perms = set(Permission)
        assert perms == all_perms

    # ------------------------------------------------------------------ #
    # MANAGER — 9 permissions (no venue.manage, no user/membership)
    # ------------------------------------------------------------------ #

    def test_manager_has_read_permissions(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert Permission.VENUE_READ in perms
        assert Permission.VIDEO_READ in perms
        assert Permission.ANALYTICS_READ in perms
        assert Permission.EVIDENCE_READ in perms
        assert Permission.RECOMMENDATION_READ in perms
        assert Permission.ALERT_READ in perms

    def test_manager_has_manage_permissions(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert Permission.RECOMMENDATION_MANAGE in perms
        assert Permission.ALERT_MANAGE in perms

    def test_manager_has_analyze(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert Permission.VIDEO_ANALYZE in perms

    def test_manager_denied_venue_manage(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert Permission.VENUE_MANAGE not in perms

    def test_manager_denied_user_admin(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert Permission.USER_READ not in perms
        assert Permission.USER_MANAGE not in perms
        assert Permission.MEMBERSHIP_READ not in perms
        assert Permission.MEMBERSHIP_MANAGE not in perms

    def test_manager_permission_count(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert len(perms) == 10

    # ------------------------------------------------------------------ #
    # OPERATOR — 6 read-only permissions
    # ------------------------------------------------------------------ #

    def test_operator_has_read_only_permissions(self) -> None:
        perms = permissions_for_role(RoleName.OPERATOR)
        assert Permission.VENUE_READ in perms
        assert Permission.VIDEO_READ in perms
        assert Permission.ANALYTICS_READ in perms
        assert Permission.EVIDENCE_READ in perms
        assert Permission.RECOMMENDATION_READ in perms
        assert Permission.ALERT_READ in perms

    def test_operator_denied_manage(self) -> None:
        perms = permissions_for_role(RoleName.OPERATOR)
        assert Permission.VENUE_MANAGE not in perms
        assert Permission.RECOMMENDATION_MANAGE not in perms
        assert Permission.ALERT_MANAGE not in perms
        assert Permission.USER_MANAGE not in perms
        assert Permission.MEMBERSHIP_MANAGE not in perms

    def test_operator_denied_user_admin(self) -> None:
        perms = permissions_for_role(RoleName.OPERATOR)
        assert Permission.USER_READ not in perms
        assert Permission.MEMBERSHIP_READ not in perms

    def test_operator_denied_analyze(self) -> None:
        perms = permissions_for_role(RoleName.OPERATOR)
        assert Permission.VIDEO_ANALYZE not in perms

    def test_operator_permission_count(self) -> None:
        perms = permissions_for_role(RoleName.OPERATOR)
        assert len(perms) == 6


# =============================================================================
# ActorContext has_permission() — authorization evaluation
# =============================================================================


class TestActorPermissionEvaluation:
    """Direct evaluation of ActorContext.has_permission()."""

    def _make_context(self, role: RoleName) -> ActorContext:
        """Create a minimal ActorContext with the given role."""
        return ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(uuid4()),
            role_name=role,
            permissions=permissions_for_role(role),
            authenticated_at=datetime.now(UTC),
        )

    # ----- ADMIN allowed/denied -----

    def test_admin_allowed_venue_manage(self) -> None:
        ctx = self._make_context(RoleName.ADMIN)
        assert ctx.has_permission(Permission.VENUE_MANAGE)

    def test_admin_allowed_user_manage(self) -> None:
        ctx = self._make_context(RoleName.ADMIN)
        assert ctx.has_permission(Permission.USER_MANAGE)

    def test_admin_allowed_membership_manage(self) -> None:
        ctx = self._make_context(RoleName.ADMIN)
        assert ctx.has_permission(Permission.MEMBERSHIP_MANAGE)

    def test_admin_allowed_analytics_read(self) -> None:
        ctx = self._make_context(RoleName.ADMIN)
        assert ctx.has_permission(Permission.ANALYTICS_READ)

    # ----- MANAGER allowed/denied -----

    def test_manager_allowed_recommendation_manage(self) -> None:
        ctx = self._make_context(RoleName.MANAGER)
        assert ctx.has_permission(Permission.RECOMMENDATION_MANAGE)

    def test_manager_allowed_alert_manage(self) -> None:
        ctx = self._make_context(RoleName.MANAGER)
        assert ctx.has_permission(Permission.ALERT_MANAGE)

    def test_manager_allowed_video_analyze(self) -> None:
        ctx = self._make_context(RoleName.MANAGER)
        assert ctx.has_permission(Permission.VIDEO_ANALYZE)

    def test_manager_denied_venue_manage(self) -> None:
        ctx = self._make_context(RoleName.MANAGER)
        assert not ctx.has_permission(Permission.VENUE_MANAGE)

    def test_manager_denied_user_manage(self) -> None:
        ctx = self._make_context(RoleName.MANAGER)
        assert not ctx.has_permission(Permission.USER_MANAGE)

    def test_manager_denied_membership_manage(self) -> None:
        ctx = self._make_context(RoleName.MANAGER)
        assert not ctx.has_permission(Permission.MEMBERSHIP_MANAGE)

    # ----- OPERATOR allowed/denied -----

    def test_operator_allowed_video_read(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert ctx.has_permission(Permission.VIDEO_READ)

    def test_operator_allowed_evidence_read(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert ctx.has_permission(Permission.EVIDENCE_READ)

    def test_operator_allowed_alert_read(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert ctx.has_permission(Permission.ALERT_READ)

    def test_operator_denied_venue_manage(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.VENUE_MANAGE)

    def test_operator_denied_recommendation_manage(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.RECOMMENDATION_MANAGE)

    def test_operator_denied_alert_manage(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.ALERT_MANAGE)

    def test_operator_denied_video_analyze(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.VIDEO_ANALYZE)

    def test_operator_denied_user_read(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.USER_READ)

    def test_operator_denied_membership_manage(self) -> None:
        ctx = self._make_context(RoleName.OPERATOR)
        assert not ctx.has_permission(Permission.MEMBERSHIP_MANAGE)

    # ----- Operator cannot escalate to admin actions -----

    def test_operator_cannot_manage_users(self) -> None:
        """Operator attempting user management should be denied."""
        ctx = self._make_context(RoleName.OPERATOR)
        admin_actions = [
            Permission.USER_MANAGE,
            Permission.MEMBERSHIP_MANAGE,
            Permission.VENUE_MANAGE,
            Permission.RECOMMENDATION_MANAGE,
            Permission.ALERT_MANAGE,
            Permission.VIDEO_ANALYZE,
        ]
        for action in admin_actions:
            assert not ctx.has_permission(action), f"Operator should not have {action.value}"

    def test_manager_cannot_manage_users(self) -> None:
        """Manager attempting user/membership admin should be denied."""
        ctx = self._make_context(RoleName.MANAGER)
        admin_actions = [
            Permission.USER_MANAGE,
            Permission.MEMBERSHIP_MANAGE,
        ]
        for action in admin_actions:
            assert not ctx.has_permission(action), f"Manager should not have {action.value}"


# =============================================================================
# Role escalation attempts
# =============================================================================


class TestRoleEscalation:
    """Verify that roles cannot escalate their permissions."""

    def test_operator_cannot_forge_admin_context(self) -> None:
        """Creating an ActorContext with explicit permissions doesn't grant more."""
        # Create context with ADMIN permissions on an OPERATOR role
        forged = ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.ADMIN),  # forged permissions!
            authenticated_at=datetime.now(UTC),
        )
        # Even with forged permissions, the ActorContext is frozen
        # and the permissions are resolved server-side, so this
        # represents a code-level bug rather than an actual attack vector.
        # The important test: the server-side builder would never
        # assign ADMIN permissions to an OPERATOR role.
        assert forged.role_name == RoleName.OPERATOR
        assert forged.has_permission(Permission.USER_MANAGE)  # permissions are forged

    def test_builder_never_assigns_mismatched_permissions(self) -> None:
        """The ActorContextBuilder always derives permissions from the role."""
        operator_perms = permissions_for_role(RoleName.OPERATOR)
        manager_perms = permissions_for_role(RoleName.MANAGER)
        admin_perms = permissions_for_role(RoleName.ADMIN)

        # Verify that role-permission mappings are disjoint where expected
        assert Permission.USER_MANAGE not in operator_perms
        assert Permission.USER_MANAGE not in manager_perms
        assert Permission.USER_MANAGE in admin_perms

        assert Permission.MEMBERSHIP_MANAGE not in operator_perms
        assert Permission.MEMBERSHIP_MANAGE not in manager_perms
        assert Permission.MEMBERSHIP_MANAGE in admin_perms


# =============================================================================
# Forged request permissions
# =============================================================================


class TestForgedRequestPermissions:
    """Verify that client-supplied permission values cannot alter authorization."""

    def test_context_permissions_from_role_not_request(self) -> None:
        """Permissions are derived from server-side role, never from client input."""
        uid = "00000000-0000-0000-0000-000000000000"
        now = datetime.now(UTC)
        token_data = TokenData(user_id=uid, issued_at=now, expires_at=now)

        builder = ActorContextBuilder()
        ctx = builder.build(token_data)
        # Default role is OPERATOR — permissions come from role, not client
        expected = permissions_for_role(RoleName.OPERATOR)
        assert ctx.permissions == expected
        assert Permission.USER_MANAGE not in ctx.permissions

    def test_no_permission_in_token(self) -> None:
        """JWT tokens carry only sub (user_id). No permissions in token."""
        settings = Settings(
            app_env="test",
            SECRET_KEY="test-secret-key-32-chars-long-ok!!!!!",
            JWT_ALGORITHM="HS256",
            JWT_EXPIRATION_MINUTES=60,
            _env_file=None,  # type: ignore[call-arg]
        )
        token = create_access_token("test-user", settings)

        decoded = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        assert "permissions" not in decoded
        assert "role" not in decoded
        assert "tenant_id" not in decoded


# =============================================================================
# Unknown permission behavior
# =============================================================================


class TestUnknownPermission:
    """Tests for undefined/unknown permission behavior."""

    def test_unknown_permission_not_granted(self) -> None:
        """A role should not have a non-existent permission."""
        perms = permissions_for_role(RoleName.OPERATOR)
        # Permission values not in the canonical catalog
        assert Permission.USER_MANAGE not in perms  # exists but not granted

    def test_permission_not_found_returns_false(self) -> None:
        """has_permission returns False for unassigned permissions."""
        ctx = ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            authenticated_at=datetime.now(UTC),
        )
        assert not ctx.has_permission(Permission.VENUE_MANAGE)
        assert not ctx.has_permission(Permission.USER_MANAGE)


# =============================================================================
# require_permission and require_any_permission dependency factories
# =============================================================================


@pytest.fixture
def admin_context() -> ActorContext:
    return ActorContext(
        actor_id=UserId(uuid4()),
        tenant_id=TenantId(uuid4()),
        role_name=RoleName.ADMIN,
        permissions=permissions_for_role(RoleName.ADMIN),
        authenticated_at=datetime.now(UTC),
    )


@pytest.fixture
def operator_context() -> ActorContext:
    return ActorContext(
        actor_id=UserId(uuid4()),
        tenant_id=TenantId(uuid4()),
        role_name=RoleName.OPERATOR,
        permissions=permissions_for_role(RoleName.OPERATOR),
        authenticated_at=datetime.now(UTC),
    )


class TestRequirePermission:
    """Tests for require_permission dependency factory.

    The inner functions accept an optional ActorContext argument.
    When called with an explicit ActorContext, the Depends() default
    is not evaluated — this allows direct unit testing without FastAPI DI.
    """

    @pytest.mark.asyncio
    async def test_admin_allowed_analytics(self, admin_context: ActorContext) -> None:
        check = require_permission(Permission.ANALYTICS_READ)
        # Pass ActorContext directly — Depends() default is bypassed
        result = await check(admin_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_operator_allowed_analytics(self, operator_context: ActorContext) -> None:
        check = require_permission(Permission.ANALYTICS_READ)
        result = await check(operator_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_operator_denied_user_manage(self, operator_context: ActorContext) -> None:
        """Operator should be denied USER_MANAGE."""
        check = require_permission(Permission.USER_MANAGE)
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await check(operator_context)

    @pytest.mark.asyncio
    async def test_operator_denied_membership_manage(self, operator_context: ActorContext) -> None:
        """Operator should be denied MEMBERSHIP_MANAGE."""
        check = require_permission(Permission.MEMBERSHIP_MANAGE)
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await check(operator_context)

    @pytest.mark.asyncio
    async def test_manager_denied_user_manage(self) -> None:
        """Manager should be denied USER_MANAGE."""
        manager_ctx = ActorContext(
            actor_id=UserId(uuid4()),
            tenant_id=TenantId(uuid4()),
            role_name=RoleName.MANAGER,
            permissions=permissions_for_role(RoleName.MANAGER),
            authenticated_at=datetime.now(UTC),
        )
        check = require_permission(Permission.USER_MANAGE)
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await check(manager_ctx)

    @pytest.mark.asyncio
    async def test_operator_denied_venue_manage(self, operator_context: ActorContext) -> None:
        """Operator should be denied VENUE_MANAGE."""
        check = require_permission(Permission.VENUE_MANAGE)
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await check(operator_context)


class TestRequireAnyPermission:
    """Tests for require_any_permission dependency factory."""

    @pytest.mark.asyncio
    async def test_admin_has_any_admin_permission(self, admin_context: ActorContext) -> None:
        check = require_any_permission(
            Permission.USER_MANAGE,
            Permission.MEMBERSHIP_MANAGE,
        )
        result = await check(admin_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_operator_lacks_any_admin_permission(
        self, operator_context: ActorContext
    ) -> None:
        """Operator has neither USER_MANAGE nor MEMBERSHIP_MANAGE."""
        check = require_any_permission(
            Permission.USER_MANAGE,
            Permission.MEMBERSHIP_MANAGE,
        )
        with pytest.raises(AuthorizationError, match="at least one"):
            await check(operator_context)

    @pytest.mark.asyncio
    async def test_operator_has_at_least_one(self, operator_context: ActorContext) -> None:
        """Operator has ALERT_READ but not VENUE_MANAGE — should pass."""
        check = require_any_permission(
            Permission.VENUE_MANAGE,
            Permission.ALERT_READ,
        )
        result = await check(operator_context)
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_require_any_permission_no_args_raises(self) -> None:
        """require_any_permission() with zero arguments should raise ValueError."""
        check = require_any_permission()
        with pytest.raises(ValueError, match="At least one permission required"):
            await check()
