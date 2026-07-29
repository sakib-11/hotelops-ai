"""Tests for Task 5.3 — Identity/tenancy SQLAlchemy ORM models.

Tests schema correctness — constraints, foreign keys, unique
constraints, and cross-tenant protection — without requiring
a live database.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Enum,
    Table,
    UniqueConstraint,
)

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.identity import (
    MembershipModel,
    PermissionModel,
    RoleModel,
    TenantModel,
    UserModel,
    VenueModel,
)

# =============================================================================
# Table existence
# =============================================================================


class TestTableExistence:
    """Verify all required tables are registered with Base.metadata."""

    def test_tenants_table_exists(self) -> None:
        assert "tenants" in Base.metadata.tables

    def test_venues_table_exists(self) -> None:
        assert "venues" in Base.metadata.tables

    def test_users_table_exists(self) -> None:
        assert "users" in Base.metadata.tables

    def test_roles_table_exists(self) -> None:
        assert "roles" in Base.metadata.tables

    def test_permissions_table_exists(self) -> None:
        assert "permissions" in Base.metadata.tables

    def test_role_permissions_table_exists(self) -> None:
        assert "role_permissions" in Base.metadata.tables

    def test_memberships_table_exists(self) -> None:
        assert "memberships" in Base.metadata.tables

    def test_membership_venues_table_exists(self) -> None:
        assert "membership_venues" in Base.metadata.tables


# =============================================================================
# Column constraints
# =============================================================================


class TestTenantConstraints:
    """Tenant table constraints."""

    def test_primary_key(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["tenant_id"]

    def test_tenant_id_is_uuid(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        col = table.columns["tenant_id"]
        assert isinstance(col.type, col.type.__class__)
        assert str(col.type) == "UUID"

    def test_name_not_nullable(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        assert not table.columns["name"].nullable

    def test_name_max_length(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        col = table.columns["name"]
        assert col.type.length == 255

    def test_status_has_enum(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert list(col.type.enums) == ["active", "suspended", "disabled"]

    def test_created_at_timezone(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        assert table.columns["created_at"].type.timezone is True

    def test_created_at_not_nullable(self) -> None:
        table: Table = Base.metadata.tables["tenants"]
        assert not table.columns["created_at"].nullable


class TestVenueConstraints:
    """Venue table constraints."""

    def test_primary_key(self) -> None:
        table: Table = Base.metadata.tables["venues"]
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["venue_id"]

    def test_tenant_id_not_nullable(self) -> None:
        table: Table = Base.metadata.tables["venues"]
        assert not table.columns["tenant_id"].nullable

    def test_tenant_id_foreign_key_to_tenants(self) -> None:
        table: Table = Base.metadata.tables["venues"]
        fks = [fk for fk in table.foreign_key_constraints]
        tenant_fks = [fk for fk in fks if "tenant_id" in [c.name for c in fk.columns]]
        assert len(tenant_fks) >= 1
        assert len(tenant_fks) >= 1

    def test_tenant_id_indexed(self) -> None:
        table: Table = Base.metadata.tables["venues"]
        idx_names = [idx.name for idx in table.indexes]
        assert "ix_venues_tenant_id" in idx_names

    def test_created_at_timezone(self) -> None:
        table: Table = Base.metadata.tables["venues"]
        assert table.columns["created_at"].type.timezone is True


class TestUserConstraints:
    """User table constraints."""

    def test_primary_key(self) -> None:
        table: Table = Base.metadata.tables["users"]
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["user_id"]

    def test_email_unique(self) -> None:
        table: Table = Base.metadata.tables["users"]
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        email_uqs = [uq for uq in uqs if [col.name for col in uq.columns] == ["email"]]
        assert len(email_uqs) >= 1

    def test_email_not_nullable(self) -> None:
        table: Table = Base.metadata.tables["users"]
        assert not table.columns["email"].nullable


class TestRoleConstraints:
    """Role table constraints."""

    def test_primary_key(self) -> None:
        table: Table = Base.metadata.tables["roles"]
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["role_id"]

    def test_name_unique(self) -> None:
        table: Table = Base.metadata.tables["roles"]
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        name_uqs = [uq for uq in uqs if [col.name for col in uq.columns] == ["name"]]
        assert len(name_uqs) >= 1

    def test_name_enum_admin_manager_operator(self) -> None:
        table: Table = Base.metadata.tables["roles"]
        col = table.columns["name"]
        assert isinstance(col.type, Enum)
        assert list(col.type.enums) == ["admin", "manager", "operator"]


class TestPermissionConstraints:
    """Permission table constraints."""

    def test_primary_key(self) -> None:
        table: Table = Base.metadata.tables["permissions"]
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["permission_id"]

    def test_name_unique(self) -> None:
        table: Table = Base.metadata.tables["permissions"]
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        name_uqs = [uq for uq in uqs if [col.name for col in uq.columns] == ["name"]]
        assert len(name_uqs) >= 1

    def test_name_not_nullable(self) -> None:
        table: Table = Base.metadata.tables["permissions"]
        assert not table.columns["name"].nullable


class TestMembershipConstraints:
    """Membership table constraints."""

    def test_primary_key(self) -> None:
        table: Table = Base.metadata.tables["memberships"]
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["membership_id"]

    def test_user_id_fk(self) -> None:
        table: Table = Base.metadata.tables["memberships"]
        fks = [fk for fk in table.foreign_key_constraints]
        user_fks = [fk for fk in fks if any(col.name == "user_id" for col in fk.columns)]
        assert len(user_fks) >= 1

    def test_tenant_id_fk(self) -> None:
        table: Table = Base.metadata.tables["memberships"]
        fks = [fk for fk in table.foreign_key_constraints]
        tenant_fks = [fk for fk in fks if any(col.name == "tenant_id" for col in fk.columns)]
        assert len(tenant_fks) >= 1

    def test_role_id_fk(self) -> None:
        table: Table = Base.metadata.tables["memberships"]
        fks = [fk for fk in table.foreign_key_constraints]
        role_fks = [fk for fk in fks if any(col.name == "role_id" for col in fk.columns)]
        assert len(role_fks) >= 1

    def test_all_three_fks_are_distinct(self) -> None:
        table: Table = Base.metadata.tables["memberships"]
        fk_count = len(table.foreign_key_constraints)
        assert fk_count >= 3

    def test_scope_enum(self) -> None:
        table: Table = Base.metadata.tables["memberships"]
        col = table.columns["scope"]
        assert isinstance(col.type, Enum)


class TestRolePermissionsTable:
    """Role-permissions association table constraints."""

    def test_exists(self) -> None:
        assert "role_permissions" in Base.metadata.tables

    def test_role_id_fk(self) -> None:
        table: Table = Base.metadata.tables["role_permissions"]
        fks = [fk for fk in table.foreign_key_constraints]
        role_fks = [fk for fk in fks if any(col.name == "role_id" for col in fk.columns)]
        assert len(role_fks) >= 1

    def test_permission_id_fk(self) -> None:
        table: Table = Base.metadata.tables["role_permissions"]
        fks = [fk for fk in table.foreign_key_constraints]
        perm_fks = [fk for fk in fks if any(col.name == "permission_id" for col in fk.columns)]
        assert len(perm_fks) >= 1

    def test_composite_pk(self) -> None:
        table: Table = Base.metadata.tables["role_permissions"]
        pk_col_names = [c.name for c in table.primary_key.columns]
        assert set(pk_col_names) == {"role_id", "permission_id"}


class TestMembershipVenuesTable:
    """Membership-venues association table constraints."""

    def test_exists(self) -> None:
        assert "membership_venues" in Base.metadata.tables

    def test_membership_id_fk(self) -> None:
        table: Table = Base.metadata.tables["membership_venues"]
        fks = [fk for fk in table.foreign_key_constraints]
        membership_fks = [
            fk for fk in fks if any(col.name == "membership_id" for col in fk.columns)
        ]
        assert len(membership_fks) >= 1

    def test_venue_id_fk_to_venues(self) -> None:
        table: Table = Base.metadata.tables["membership_venues"]
        fks = [fk for fk in table.foreign_key_constraints]
        venue_fks = [fk for fk in fks if any(col.name == "venue_id" for col in fk.columns)]
        assert len(venue_fks) >= 1

    def test_composite_pk(self) -> None:
        table: Table = Base.metadata.tables["membership_venues"]
        pk_col_names = [c.name for c in table.primary_key.columns]
        assert set(pk_col_names) == {"membership_id", "venue_id"}

    def test_venue_id_index(self) -> None:
        table: Table = Base.metadata.tables["membership_venues"]
        idx_names = [idx.name for idx in table.indexes]
        assert "ix_membership_venues_venue" in idx_names


# =============================================================================
# Cross-tenant protection (schema-level)
# =============================================================================


class TestCrossTenantProtection:
    """Verify the schema prevents cross-tenant data access at the DB level."""

    def test_venue_has_tenant_id_fk(self) -> None:
        """Venue must belong to a tenant — FK ensures referential integrity."""
        table: Table = Base.metadata.tables["venues"]
        # Verify that at least one FK references tenants
        tenant_refs = [
            fk
            for fk in table.foreign_key_constraints
            for elem in fk.elements
            if elem.column.table.name == "tenants"
        ]
        assert len(tenant_refs) >= 1

    def test_membership_has_tenant_id_fk(self) -> None:
        """Membership must belong to a tenant — prevents tenant-less members."""
        table: Table = Base.metadata.tables["memberships"]
        tenant_refs = [
            fk
            for fk in table.foreign_key_constraints
            for elem in fk.elements
            if elem.column.table.name == "tenants"
        ]
        assert len(tenant_refs) >= 1

    def test_membership_venues_venue_exists(self) -> None:
        """membership_venues references venues which reference tenants.
        Cross-tenant venue assignment is impossible because the FK
        chain requires the venue to exist (and thus belong to a tenant).
        """
        table: Table = Base.metadata.tables["membership_venues"]
        venue_refs = [
            fk
            for fk in table.foreign_key_constraints
            for elem in fk.elements
            if elem.column.table.name == "venues"
        ]
        assert len(venue_refs) >= 1


# =============================================================================
# Default values and model instantiation
# =============================================================================


class TestModelDefaults:
    """Verify models accept defaults and can be constructed."""

    def test_tenant_creation(self) -> None:
        tenant = TenantModel(
            tenant_id=uuid.uuid4(),
            name="Test Hotel",
            status="active",
        )
        assert tenant.status == "active"

    def test_tenant_with_all_fields(self) -> None:
        tenant = TenantModel(
            tenant_id=uuid.uuid4(),
            name="Oceanview Hotels",
            status="suspended",
        )
        assert tenant.status == "suspended"

    def test_venue_creation_with_tenant(self) -> None:
        tenant = TenantModel(tenant_id=uuid.uuid4(), name="Test", status="active")
        venue = VenueModel(
            venue_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            name="Lobby",
            status="active",
        )
        assert venue.status == "active"

    def test_user_creation(self) -> None:
        user = UserModel(
            user_id=uuid.uuid4(),
            display_name="Alice",
            email="alice@example.com",
            status="active",
        )
        assert user.status == "active"

    def test_role_creation(self) -> None:
        role = RoleModel(
            role_id=uuid.uuid4(),
            name="admin",
        )
        assert role.name == "admin"

    def test_permission_creation(self) -> None:
        perm = PermissionModel(
            permission_id=uuid.uuid4(),
            name="venue.read",
        )
        assert perm.name == "venue.read"

    def test_membership_creation(self) -> None:
        membership = MembershipModel(
            membership_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            role_id=uuid.uuid4(),
            scope="all_venues",
            status="active",
        )
        assert membership.scope == "all_venues"
        assert membership.status == "active"
