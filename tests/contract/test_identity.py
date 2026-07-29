"""Tests for Task 5.2 — Identity & Tenancy Domain Model.

Tests cover:
- Model creation and defaults
- Serialization / deserialisation
- Invalid value rejection
- Membership scope validation
- Role-permission mapping
- Tenant / Venue relationship
- ActorContext authorization checks
- Task 4 primitive reuse (IDs, UTC, versioning)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.common import (
    SCHEMA_VERSION,
    MembershipId,
    RoleId,
    TenantId,
    UserId,
    VenueId,
    new_uuid,
)
from contracts.identity.models import (
    ActorContext,
    Membership,
    MembershipScope,
    MembershipStatus,
    Permission,
    Role,
    RoleName,
    Tenant,
    TenantStatus,
    User,
    UserStatus,
    Venue,
    VenueStatus,
    permissions_for_role,
)

# =============================================================================
# Helpers
# =============================================================================


def _utc() -> datetime:
    return datetime.now(UTC)


def _tenant_id() -> TenantId:
    return TenantId(new_uuid())


def _user_id() -> UserId:
    return UserId(new_uuid())


def _venue_id() -> VenueId:
    return VenueId(new_uuid())


def _role_id() -> RoleId:
    return RoleId(new_uuid())


def _membership_id() -> MembershipId:
    return MembershipId(new_uuid())


# =============================================================================
# Tenant
# =============================================================================


class TestTenant:
    def test_create_active(self) -> None:
        tenant = Tenant(
            tenant_id=_tenant_id(),
            name="Oceanview Hotels",
            created_at=_utc(),
        )
        assert tenant.status == TenantStatus.ACTIVE
        assert tenant.schema_version == SCHEMA_VERSION

    def test_create_suspended(self) -> None:
        tenant = Tenant(
            tenant_id=_tenant_id(),
            name="Suspended Corp",
            status=TenantStatus.SUSPENDED,
            created_at=_utc(),
        )
        assert tenant.status == TenantStatus.SUSPENDED

    def test_name_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Tenant(
                tenant_id=_tenant_id(),
                name="",
                created_at=_utc(),
            )

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Tenant(
                tenant_id=_tenant_id(),
                name="Test",
                status="invalid_status",  # type: ignore[arg-type]
                created_at=_utc(),
            )

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Tenant(
                tenant_id=_tenant_id(),
                name="Test",
                created_at=_utc(),
                extra_field="forbidden",
            )

    def test_serialize_round_trip(self) -> None:
        created = _utc()
        tenant = Tenant(
            tenant_id=_tenant_id(),
            name="Round Trip Hotel",
            created_at=created,
        )
        serialized = tenant.model_dump(mode="json")
        restored = Tenant.model_validate(serialized)
        assert restored == tenant

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            Tenant(
                tenant_id=_tenant_id(),
                name="Test",
                created_at=datetime(2026, 1, 1),  # no tzinfo
            )


# =============================================================================
# Venue
# =============================================================================


class TestVenue:
    def test_create_with_tenant(self) -> None:
        tid = _tenant_id()
        venue = Venue(
            venue_id=_venue_id(),
            tenant_id=tid,
            name="Lobby",
            created_at=_utc(),
        )
        assert venue.tenant_id == tid
        assert venue.status == VenueStatus.ACTIVE

    def test_tenant_id_required(self) -> None:
        with pytest.raises(ValidationError):
            Venue(  # type: ignore[call-arg]
                venue_id=_venue_id(),
                name="No tenant",
                created_at=_utc(),
            )

    def test_serialize_round_trip(self) -> None:
        venue = Venue(
            venue_id=_venue_id(),
            tenant_id=_tenant_id(),
            name="Pool Area",
            status=VenueStatus.INACTIVE,
            created_at=_utc(),
        )
        serialized = venue.model_dump(mode="json")
        restored = Venue.model_validate(serialized)
        assert restored == venue


# =============================================================================
# User
# =============================================================================


class TestUser:
    def test_create_active(self) -> None:
        user = User(
            user_id=_user_id(),
            display_name="Alice",
            email="alice@example.com",
            created_at=_utc(),
        )
        assert user.status == UserStatus.ACTIVE

    def test_invalid_email_still_validates_pydantic(self) -> None:
        """Pydantic str field — email is not validated as email pattern unless
        Field(pattern=...) is used. We test basic string constraints."""
        user = User(
            user_id=_user_id(),
            display_name="Bob",
            email="not-an-email",
            created_at=_utc(),
        )
        assert user.email == "not-an-email"

    def test_display_name_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            User(
                user_id=_user_id(),
                display_name="",
                email="test@example.com",
                created_at=_utc(),
            )

    def test_no_tenant_id_on_user(self) -> None:
        """Users do NOT have tenant_id directly — they participate via Membership."""
        user = User(
            user_id=_user_id(),
            display_name="Carol",
            email="carol@example.com",
            created_at=_utc(),
        )
        assert not hasattr(user, "tenant_id")

    def test_serialize_round_trip(self) -> None:
        user = User(
            user_id=_user_id(),
            display_name="Dave",
            email="dave@example.com",
            status=UserStatus.DISABLED,
            created_at=_utc(),
        )
        serialized = user.model_dump(mode="json")
        restored = User.model_validate(serialized)
        assert restored == user


# =============================================================================
# Role & Permissions
# =============================================================================


class TestRole:
    def test_admin_permissions(self) -> None:
        perms = permissions_for_role(RoleName.ADMIN)
        assert Permission.VENUE_MANAGE in perms
        assert Permission.USER_MANAGE in perms
        assert Permission.MEMBERSHIP_MANAGE in perms

    def test_operator_limited_permissions(self) -> None:
        perms = permissions_for_role(RoleName.OPERATOR)
        assert Permission.VENUE_READ in perms
        assert Permission.ANALYTICS_READ in perms
        assert Permission.ALERT_READ in perms
        assert Permission.VENUE_MANAGE not in perms
        assert Permission.USER_MANAGE not in perms
        assert Permission.MEMBERSHIP_MANAGE not in perms

    def test_manager_mid_permissions(self) -> None:
        perms = permissions_for_role(RoleName.MANAGER)
        assert Permission.RECOMMENDATION_MANAGE in perms
        assert Permission.ALERT_MANAGE in perms
        assert Permission.USER_MANAGE not in perms
        assert Permission.MEMBERSHIP_MANAGE not in perms

    def test_role_model_permissions(self) -> None:
        role = Role(role_id=_role_id(), name=RoleName.MANAGER)
        perms = role.permissions()
        assert Permission.EVIDENCE_READ in perms
        assert Permission.VIDEO_ANALYZE in perms

    def test_serialize_round_trip(self) -> None:
        role = Role(role_id=_role_id(), name=RoleName.ADMIN)
        serialized = role.model_dump(mode="json")
        restored = Role.model_validate(serialized)
        assert restored == role
        assert restored.permissions() == role.permissions()


# =============================================================================
# Membership
# =============================================================================


class TestMembership:
    def test_all_venues_scope(self) -> None:
        membership = Membership(
            membership_id=_membership_id(),
            user_id=_user_id(),
            tenant_id=_tenant_id(),
            role_id=_role_id(),
            scope=MembershipScope.ALL_VENUES,
            created_at=_utc(),
        )
        assert membership.scope == MembershipScope.ALL_VENUES
        assert membership.venue_ids == frozenset()

    def test_specific_venues_scope(self) -> None:
        vid_1 = _venue_id()
        vid_2 = _venue_id()
        membership = Membership(
            membership_id=_membership_id(),
            user_id=_user_id(),
            tenant_id=_tenant_id(),
            role_id=_role_id(),
            scope=MembershipScope.SPECIFIC_VENUES,
            venue_ids=frozenset({vid_1, vid_2}),
            created_at=_utc(),
        )
        assert vid_1 in membership.venue_ids
        assert vid_2 in membership.venue_ids

    def test_specific_venues_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Membership(
                membership_id=_membership_id(),
                user_id=_user_id(),
                tenant_id=_tenant_id(),
                role_id=_role_id(),
                scope=MembershipScope.SPECIFIC_VENUES,
                venue_ids=frozenset(),
                created_at=_utc(),
            )

    def test_status_default(self) -> None:
        membership = Membership(
            membership_id=_membership_id(),
            user_id=_user_id(),
            tenant_id=_tenant_id(),
            role_id=_role_id(),
            scope=MembershipScope.ALL_VENUES,
            created_at=_utc(),
        )
        assert membership.status == MembershipStatus.ACTIVE

    def test_serialize_round_trip(self) -> None:
        membership = Membership(
            membership_id=_membership_id(),
            user_id=_user_id(),
            tenant_id=_tenant_id(),
            role_id=_role_id(),
            scope=MembershipScope.ALL_VENUES,
            status=MembershipStatus.INACTIVE,
            created_at=_utc(),
        )
        serialized = membership.model_dump(mode="json")
        restored = Membership.model_validate(serialized)
        assert restored == membership


# =============================================================================
# ActorContext
# =============================================================================


class TestActorContext:
    def test_admin_has_manage_permissions(self) -> None:
        ctx = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.ADMIN,
            permissions=permissions_for_role(RoleName.ADMIN),
            authenticated_at=_utc(),
        )
        assert ctx.has_permission(Permission.VENUE_MANAGE)
        assert ctx.has_permission(Permission.USER_MANAGE)
        assert ctx.has_permission(Permission.MEMBERSHIP_MANAGE)

    def test_operator_no_manage(self) -> None:
        ctx = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            authenticated_at=_utc(),
        )
        assert not ctx.has_permission(Permission.VENUE_MANAGE)
        assert not ctx.has_permission(Permission.USER_MANAGE)

    def test_venue_access_allowed(self) -> None:
        vid = _venue_id()
        ctx = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.MANAGER,
            permissions=permissions_for_role(RoleName.MANAGER),
            venue_scope=frozenset({vid}),
            authenticated_at=_utc(),
        )
        assert ctx.has_venue_access(vid)

    def test_venue_access_denied(self) -> None:
        ctx = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.MANAGER,
            permissions=permissions_for_role(RoleName.MANAGER),
            venue_scope=frozenset(),
            authenticated_at=_utc(),
        )
        other_vid = _venue_id()
        assert not ctx.has_venue_access(other_vid)

    def test_admin_helper(self) -> None:
        ctx = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.ADMIN,
            permissions=permissions_for_role(RoleName.ADMIN),
            authenticated_at=_utc(),
        )
        assert ctx.is_admin()

        ctx2 = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            authenticated_at=_utc(),
        )
        assert not ctx2.is_admin()

    def test_serialize_round_trip(self) -> None:
        ctx = ActorContext(
            actor_id=_user_id(),
            tenant_id=_tenant_id(),
            role_name=RoleName.ADMIN,
            permissions=permissions_for_role(RoleName.ADMIN),
            venue_scope=frozenset({_venue_id()}),
            authenticated_at=_utc(),
        )
        serialized = ctx.model_dump(mode="json")
        restored = ActorContext.model_validate(serialized)
        assert restored == ctx
        assert restored.has_permission(Permission.VENUE_MANAGE)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            ActorContext(
                actor_id=_user_id(),
                tenant_id=_tenant_id(),
                role_name=RoleName.ADMIN,
                permissions=permissions_for_role(RoleName.ADMIN),
                authenticated_at=datetime(2026, 1, 1),  # no tzinfo
            )


# =============================================================================
# Relationship Validation
# =============================================================================


class TestRelationships:
    def test_venue_links_to_tenant(self) -> None:
        tid = _tenant_id()
        venue = Venue(
            venue_id=_venue_id(),
            tenant_id=tid,
            name="Restaurant",
            created_at=_utc(),
        )
        assert venue.tenant_id == tid

    def test_membership_links_user_tenant_role(self) -> None:
        uid = _user_id()
        tid = _tenant_id()
        rid = _role_id()
        membership = Membership(
            membership_id=_membership_id(),
            user_id=uid,
            tenant_id=tid,
            role_id=rid,
            scope=MembershipScope.ALL_VENUES,
            created_at=_utc(),
        )
        assert membership.user_id == uid
        assert membership.tenant_id == tid
        assert membership.role_id == rid

    def test_actor_context_from_role(self) -> None:
        """ActorContext can be constructed from a Role and Membership."""
        tid = _tenant_id()
        uid = _user_id()
        role = Role(role_id=_role_id(), name=RoleName.MANAGER)

        ctx = ActorContext(
            actor_id=uid,
            tenant_id=tid,
            role_name=role.name,
            permissions=role.permissions(),
            authenticated_at=_utc(),
        )
        assert ctx.has_permission(Permission.RECOMMENDATION_MANAGE)
        assert not ctx.has_permission(Permission.VENUE_MANAGE)

    def test_membership_scope_serializes_as_list_in_json(self) -> None:
        """frozenset serializes as list in JSON mode — verify round-trip."""
        vid_1 = _venue_id()
        vid_2 = _venue_id()
        membership = Membership(
            membership_id=_membership_id(),
            user_id=_user_id(),
            tenant_id=_tenant_id(),
            role_id=_role_id(),
            scope=MembershipScope.SPECIFIC_VENUES,
            venue_ids=frozenset({vid_1, vid_2}),
            created_at=_utc(),
        )
        raw = membership.model_dump(mode="json")
        assert isinstance(raw["venue_ids"], list)
        assert len(raw["venue_ids"]) == 2

        restored = Membership.model_validate(raw)
        assert restored.venue_ids == membership.venue_ids
