"""Canonical identity/tenancy contracts for HotelOps AI.

Tenant, Venue, User, Role, Membership, and ActorContext models.
These define the identity and authorization boundary for the platform.
"""

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

__all__ = [
    "ActorContext",
    "Membership",
    "MembershipScope",
    "MembershipStatus",
    "Permission",
    "Role",
    "RoleName",
    "Tenant",
    "TenantStatus",
    "User",
    "UserStatus",
    "Venue",
    "VenueStatus",
    "permissions_for_role",
]
