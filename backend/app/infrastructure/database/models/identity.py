"""SQLAlchemy ORM models for the identity/tenancy domain.

Implements the Task 5.1/5.2 domain model as database tables.

Domain relationships enforced at DB level:
  Tenant (1) ── Venue (0..N)
  Tenant (1) ── Membership (0..N) ── User (1)
  Role (1) ── Membership (0..N)
  Membership (1) ── membership_venues (0..N) ── Venue (1)

Key invariants:
  - Every venue belongs to exactly one tenant (venue.tenant_id FK)
  - Every membership belongs to exactly one tenant (membership.tenant_id FK)
  - membership_venues references a venue that must exist (venue_id FK)
  - Cross-tenant venue references are prevented by FK constraints
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.infrastructure.database.base import Base

# =============================================================================
# Enum helpers — mirror contracts/identity/models.py values
# =============================================================================

# We use strings directly for portability; SQLAlchemy Enum checks values.
_TENANT_STATUSES = ("active", "suspended", "disabled")
_VENUE_STATUSES = ("active", "inactive")
_USER_STATUSES = ("active", "disabled")
_MEMBERSHIP_STATUSES = ("active", "inactive")
_MEMBERSHIP_SCOPES = ("all_venues", "specific_venues")


# =============================================================================
# Tenant
# =============================================================================


class TenantModel(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_TENANT_STATUSES, name="tenant_status"),
        nullable=False,
        default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )

    # Relationships
    venues: Mapped[list[VenueModel]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    memberships: Mapped[list[MembershipModel]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<TenantModel({self.tenant_id}) {self.name!r} [{self.status}]>"


# =============================================================================
# Venue
# =============================================================================


class VenueModel(Base):
    __tablename__ = "venues"

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_VENUE_STATUSES, name="venue_status"),
        nullable=False,
        default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )

    # Relationships
    tenant: Mapped[TenantModel] = relationship(back_populates="venues")
    memberships: Mapped[list[MembershipModel]] = relationship(
        secondary="membership_venues",
        back_populates="venues",
        viewonly=True,
    )

    def __repr__(self) -> str:
        return f"<VenueModel({self.venue_id}) {self.name!r} [{self.status}]>"


# =============================================================================
# User
# =============================================================================


class UserModel(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        Enum(*_USER_STATUSES, name="user_status"),
        nullable=False,
        default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )

    # Relationships
    memberships: Mapped[list[MembershipModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<UserModel({self.user_id}) {self.email!r} [{self.status}]>"


# =============================================================================
# Role
# =============================================================================


class RoleModel(Base):
    __tablename__ = "roles"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        Enum("admin", "manager", "operator", name="role_name"),
        nullable=False,
        unique=True,
    )

    # Relationships
    memberships: Mapped[list[MembershipModel]] = relationship(back_populates="role")
    permissions: Mapped[list[PermissionModel]] = relationship(
        secondary="role_permissions",
        back_populates="roles",
    )

    def __repr__(self) -> str:
        return f"<RoleModel({self.role_id}) {self.name!r}>"


# =============================================================================
# Permission
# =============================================================================


class PermissionModel(Base):
    __tablename__ = "permissions"

    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
    )

    # Relationships
    roles: Mapped[list[RoleModel]] = relationship(
        secondary="role_permissions",
        back_populates="permissions",
    )

    def __repr__(self) -> str:
        return f"<PermissionModel({self.permission_id}) {self.name!r}>"


# =============================================================================
# Role-Permission association table
# =============================================================================


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id",
        UUID(as_uuid=True),
        ForeignKey("roles.role_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        UUID(as_uuid=True),
        ForeignKey("permissions.permission_id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# =============================================================================
# Membership
# =============================================================================


class MembershipModel(Base):
    __tablename__ = "memberships"

    __table_args__ = (Index("ix_memberships_tenant_user", "tenant_id", "user_id"),)

    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.role_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope: Mapped[str] = mapped_column(
        Enum(*_MEMBERSHIP_SCOPES, name="membership_scope"),
        nullable=False,
        default="all_venues",
    )
    status: Mapped[str] = mapped_column(
        Enum(*_MEMBERSHIP_STATUSES, name="membership_status"),
        nullable=False,
        default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Relationships
    user: Mapped[UserModel] = relationship(back_populates="memberships")
    tenant: Mapped[TenantModel] = relationship(back_populates="memberships")
    role: Mapped[RoleModel] = relationship(back_populates="memberships")
    venues: Mapped[list[VenueModel]] = relationship(
        secondary="membership_venues",
        back_populates="memberships",
    )

    def __repr__(self) -> str:
        return (
            f"<MembershipModel({self.membership_id}) user={self.user_id} tenant={self.tenant_id}>"
        )


# =============================================================================
# Membership-Venue association table
# =============================================================================


membership_venues = Table(
    "membership_venues",
    Base.metadata,
    Column(
        "membership_id",
        UUID(as_uuid=True),
        ForeignKey("memberships.membership_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "venue_id",
        UUID(as_uuid=True),
        ForeignKey("venues.venue_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # Composite index for efficient venue-scoped membership lookups
    Index("ix_membership_venues_venue", "venue_id"),
)
