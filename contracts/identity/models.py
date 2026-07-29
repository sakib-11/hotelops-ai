"""Canonical identity/tenancy contract models.

Domain relationships:

    Tenant (1)
      ├── Venue (0..N)
      └── Membership (0..N)
             ├── User (1)
             ├── Role (1)
             └── Venue Scope (ALL_VENUES | SPECIFIC_VENUES)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    MembershipId,
    RoleId,
    TenantId,
    UserId,
    VenueId,
    validate_schema_version,
    validate_utc,
)

# =============================================================================
# Enums
# =============================================================================


class TenantStatus(StrEnum):
    """Explicit tenant lifecycle state.

    Avoids boolean combinations (is_active, is_disabled, is_suspended)
    that can create impossible states.
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class VenueStatus(StrEnum):
    """Lifecycle state of a venue within a tenant."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class UserStatus(StrEnum):
    """Lifecycle state of a platform user."""

    ACTIVE = "active"
    DISABLED = "disabled"


class RoleName(StrEnum):
    """Fixed system roles for v1.0.

    Roles are NOT tenant-customizable in this release.
    """

    ADMIN = "admin"
    MANAGER = "manager"
    OPERATOR = "operator"


class MembershipStatus(StrEnum):
    """Lifecycle state of a membership relationship."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class MembershipScope(StrEnum):
    """Explicit venue scope model for a membership.

    ALL_VENUES grants access to every venue within the tenant.
    SPECIFIC_VENUES is paired with venue_ids on the Membership.
    Never use venue_ids=[] to mean "all venues".
    """

    ALL_VENUES = "all_venues"
    SPECIFIC_VENUES = "specific_venues"


# =============================================================================
# Permission Catalog
# =============================================================================


class Permission(StrEnum):
    """Canonical permission catalog.

    Permissions are capabilities. Roles group permissions.
    Authorization checks using has_permission() rather than role name.
    """

    # Venue
    VENUE_READ = "venue.read"
    VENUE_MANAGE = "venue.manage"

    # Video
    VIDEO_READ = "video.read"
    VIDEO_ANALYZE = "video.analyze"

    # Analytics
    ANALYTICS_READ = "analytics.read"

    # Evidence
    EVIDENCE_READ = "evidence.read"

    # Recommendation
    RECOMMENDATION_READ = "recommendation.read"
    RECOMMENDATION_MANAGE = "recommendation.manage"

    # Alert
    ALERT_READ = "alert.read"
    ALERT_MANAGE = "alert.manage"

    # User
    USER_READ = "user.read"
    USER_MANAGE = "user.manage"

    # Membership
    MEMBERSHIP_READ = "membership.read"
    MEMBERSHIP_MANAGE = "membership.manage"


# Role-to-Permission mapping — frozen at contract level for clarity
_ROLE_PERMISSIONS: dict[RoleName, frozenset[Permission]] = {
    RoleName.ADMIN: frozenset({
        Permission.VENUE_READ,
        Permission.VENUE_MANAGE,
        Permission.VIDEO_READ,
        Permission.VIDEO_ANALYZE,
        Permission.ANALYTICS_READ,
        Permission.EVIDENCE_READ,
        Permission.RECOMMENDATION_READ,
        Permission.RECOMMENDATION_MANAGE,
        Permission.ALERT_READ,
        Permission.ALERT_MANAGE,
        Permission.USER_READ,
        Permission.USER_MANAGE,
        Permission.MEMBERSHIP_READ,
        Permission.MEMBERSHIP_MANAGE,
    }),
    RoleName.MANAGER: frozenset({
        Permission.VENUE_READ,
        Permission.VIDEO_READ,
        Permission.VIDEO_ANALYZE,
        Permission.ANALYTICS_READ,
        Permission.EVIDENCE_READ,
        Permission.RECOMMENDATION_READ,
        Permission.RECOMMENDATION_MANAGE,
        Permission.ALERT_READ,
        Permission.ALERT_MANAGE,
    }),
    RoleName.OPERATOR: frozenset({
        Permission.VENUE_READ,
        Permission.VIDEO_READ,
        Permission.ANALYTICS_READ,
        Permission.EVIDENCE_READ,
        Permission.RECOMMENDATION_READ,
        Permission.ALERT_READ,
    }),
}


def permissions_for_role(role: RoleName) -> frozenset[Permission]:
    """Return the frozen set of permissions granted to a role."""
    return _ROLE_PERMISSIONS[role]


# =============================================================================
# Tenant
# =============================================================================


class Tenant(BaseModel, frozen=True):
    """A client organization.

    Represents a hotel property group/customer.
    Has its own venue(s), users, and isolated data.
    """

    model_config = {"extra": "forbid"}

    tenant_id: TenantId
    schema_version: str = Field(default=SCHEMA_VERSION)
    name: str = Field(min_length=1, max_length=255)
    status: TenantStatus = TenantStatus.ACTIVE
    created_at: datetime
    metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)


# =============================================================================
# Venue
# =============================================================================


class Venue(BaseModel, frozen=True):
    """A specific physical location belonging to a tenant.

    Tenant relationship is explicit and non-optional.
    A venue_id alone must never grant access.
    """

    model_config = {"extra": "forbid"}

    venue_id: VenueId
    tenant_id: TenantId
    schema_version: str = Field(default=SCHEMA_VERSION)
    name: str = Field(min_length=1, max_length=255)
    status: VenueStatus = VenueStatus.ACTIVE
    created_at: datetime
    metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)


# =============================================================================
# User
# =============================================================================


class User(BaseModel, frozen=True):
    """Platform identity.

    Does NOT contain tenant_id directly — users participate in tenants
    through Membership. Does NOT contain authentication credentials.
    """

    model_config = {"extra": "forbid"}

    user_id: UserId
    schema_version: str = Field(default=SCHEMA_VERSION)
    display_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=5, max_length=255)
    status: UserStatus = UserStatus.ACTIVE
    created_at: datetime
    metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)


# =============================================================================
# Role
# =============================================================================


class Role(BaseModel, frozen=True):
    """A named set of permissions.

    Roles are system-defined for v1.0 (not tenant-customizable).
    Permissions are derived from the canonical _ROLE_PERMISSIONS mapping.
    """

    model_config = {"extra": "forbid"}

    role_id: RoleId
    schema_version: str = Field(default=SCHEMA_VERSION)
    name: RoleName

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    def permissions(self) -> frozenset[Permission]:
        """Return the frozen set of permissions for this role."""
        return permissions_for_role(self.name)


# =============================================================================
# Membership
# =============================================================================


class Membership(BaseModel, frozen=True):
    """Connects a User to a Tenant with a Role and venue scope.

    Scope is explicit:
      - ALL_VENUES: access to every venue in the tenant
      - SPECIFIC_VENUES: access only to venues in venue_ids

    Never use venue_ids=[] to mean "all venues" — use ALL_VENUES scope.
    venue_ids must be non-empty when scope is SPECIFIC_VENUES.
    """

    model_config = {"extra": "forbid"}

    membership_id: MembershipId
    user_id: UserId
    tenant_id: TenantId
    role_id: RoleId
    schema_version: str = Field(default=SCHEMA_VERSION)
    scope: MembershipScope
    venue_ids: frozenset[VenueId] = Field(default_factory=frozenset)
    status: MembershipStatus = MembershipStatus.ACTIVE
    created_at: datetime

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)

    @field_validator("venue_ids")
    @classmethod
    def _validate_venue_ids(cls, v: frozenset[VenueId], info: Any) -> frozenset[VenueId]:
        scope = info.data.get("scope")
        if scope == MembershipScope.SPECIFIC_VENUES and not v:
            raise ValueError("venue_ids must be non-empty when scope is SPECIFIC_VENUES")
        return v


# =============================================================================
# ActorContext
# =============================================================================


class ActorContext(BaseModel, frozen=True):
    """Server-constructed immutable authorization context.

    Constructed entirely server-side after authentication and authorization
    resolution. Client-supplied values (tenant_id, role, venue_ids) are
    NEVER trusted — they are derived from server-side state.
    """

    model_config = {"extra": "forbid"}

    actor_id: UserId
    tenant_id: TenantId
    role_name: RoleName
    permissions: frozenset[Permission]
    venue_scope: frozenset[VenueId] = Field(default_factory=frozenset)
    authenticated_at: datetime
    active: bool = True

    _validate_auth_time = field_validator("authenticated_at")(validate_utc)

    def has_permission(self, permission: Permission) -> bool:
        """Check if the actor has a specific permission."""
        return permission in self.permissions

    def has_venue_access(self, venue_id: VenueId) -> bool:
        """Check if the actor has access to a specific venue."""
        if not self.venue_scope:
            return False
        return venue_id in self.venue_scope

    def is_admin(self) -> bool:
        """Convenience check — prefer has_permission() in business logic."""
        return self.role_name == RoleName.ADMIN
