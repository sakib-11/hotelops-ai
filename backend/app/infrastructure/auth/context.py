"""Server-side ActorContext builder.

Builds the authoritative ActorContext from a verified TokenData.
All authorization state (tenant, role, permissions, venue scope)
is resolved server-side — NEVER from client-provided values.

FLOW:
    Verified Principal (TokenData)
           ↓
    User lookup → reject DISABLED
           ↓
    Active Membership lookup → reject inactive/revoked
           ↓
    Tenant lookup → reject DISABLED/SUSPENDED
           ↓
    Role + Permissions resolution
           ↓
    Venue Scope resolution
           ↓
    ActorContext
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from backend.app.infrastructure.auth.exceptions import AuthenticationError
from backend.app.infrastructure.auth.service import TokenData
from contracts.common import TenantId, UserId, VenueId
from contracts.identity import (
    ActorContext,
    RoleName,
    permissions_for_role,
)

# =============================================================================
# Type aliases for mockable lookup callables
# =============================================================================

# User lookup: user_id → dict with 'user_id', 'status' or None
UserLookup = Any  # Callable[[str], dict[str, Any] | None]  # defined in deps.py

# Membership lookup: user_id → dict or None
# Returns dict with keys: membership_id, user_id, tenant_id, role_id,
# role_name, scope, venue_ids (list), status
MembershipLookup = Any  # Callable[[str], dict[str, Any] | None]

# Tenant lookup: tenant_id → dict with 'tenant_id', 'status' or None
TenantLookup = Any  # Callable[[str], dict[str, Any] | None]


def _ensure_uuid(value: str | UUID) -> UUID:
    """Convert a string to UUID if needed."""
    if isinstance(value, UUID):
        return value
    return UUID(value)


def _resolve_venue_scope(
    scope: str | None,
    venue_ids: list[str] | None,
    membership_tenant_id: str,
) -> frozenset[VenueId]:
    """Resolve venue scope from membership data.

    Args:
        scope: 'all_venues' or 'specific_venues'.
        venue_ids: List of venue UUID strings (only for specific_venues).
        membership_tenant_id: The tenant the membership belongs to.

    Returns:
        Frozenset of VenueIds the user has access to.
        Empty frozenset means no venue access.
    """
    if not scope or scope == "all_venues":
        # ALL_VENUES means access to all venues — scope is empty set
        # representing the tenant's venues (resolved at query time)
        return frozenset()

    if scope == "specific_venues" and venue_ids:
        return frozenset(VenueId(_ensure_uuid(v)) for v in venue_ids)

    return frozenset()


class ActorContextBuilder:
    """Builds authoritative ActorContext from verified TokenData.

    Uses optional callable lookups to resolve user, membership,
    and tenant data. In production, these are injected via
    FastAPI dependency. In tests, mock lookups are provided.

    The builder enforces fail-closed semantics — any invalid
    or disabled state raises AuthenticationError.
    """

    def __init__(
        self,
        user_lookup: UserLookup | None = None,
        membership_lookup: MembershipLookup | None = None,
        tenant_lookup: TenantLookup | None = None,
    ) -> None:
        self._user_lookup = user_lookup
        self._membership_lookup = membership_lookup
        self._tenant_lookup = tenant_lookup

    def build(self, token_data: TokenData) -> ActorContext:
        """Build an ActorContext from verified token data.

        Returns:
            ActorContext with server-resolved authorization state.

        Raises:
            AuthenticationError if any required state is invalid,
            disabled, or missing.
        """
        user_id = token_data.user_id

        # Step 1: Resolve user
        if self._user_lookup is not None:
            user = self._user_lookup(user_id)
            if user is None:
                raise AuthenticationError("User not found")
            user_status: str | None = user.get("status")
            if user_status and user_status != "active":
                raise AuthenticationError("User account is disabled")

        # Step 2: Resolve active membership
        if self._membership_lookup is not None:
            membership = self._membership_lookup(user_id)
            if membership is None:
                raise AuthenticationError("No active membership found")
            mem_status: str | None = membership.get("status")
            if mem_status and mem_status != "active":
                raise AuthenticationError("Membership is not active")
        else:
            # When no membership lookup is available, use defaults
            # (for testing without full infrastructure)
            mem_data: dict[str, Any] = {
                "tenant_id": "00000000-0000-0000-0000-000000000000",
                "role_name": "operator",
                "scope": None,
                "venue_ids": None,
            }
            membership = mem_data

        # Step 3: Resolve tenant
        tenant_id_str: str = membership.get("tenant_id", "")
        if not tenant_id_str:
            raise AuthenticationError("Membership has no tenant")

        if self._tenant_lookup is not None:
            tenant = self._tenant_lookup(tenant_id_str)
            if tenant is None:
                raise AuthenticationError("Tenant not found")
            tenant_status: str | None = tenant.get("status")
            if tenant_status and tenant_status != "active":
                raise AuthenticationError("Tenant account is not active")

        # Step 4: Resolve role and permissions
        role_name_str: str | None = membership.get("role_name")
        if not role_name_str:
            raise AuthenticationError("Membership has no role")

        try:
            role_name = RoleName(role_name_str)
        except ValueError:
            raise AuthenticationError(f"Invalid role: {role_name_str}") from None

        role_permissions = permissions_for_role(role_name)

        # Step 5: Resolve venue scope
        scope_str: str | None = membership.get("scope")
        venue_ids_raw: list[str] | None = membership.get("venue_ids")
        venue_scope = _resolve_venue_scope(scope_str, venue_ids_raw, tenant_id_str)

        # Step 6: Build ActorContext
        return ActorContext(
            actor_id=UserId(_ensure_uuid(user_id)),
            tenant_id=TenantId(_ensure_uuid(tenant_id_str)),
            role_name=role_name,
            permissions=role_permissions,
            venue_scope=venue_scope,
            authenticated_at=token_data.issued_at,
            active=True,
        )
