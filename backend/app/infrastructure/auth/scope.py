"""Tenant and venue scope enforcement.

Centralized authorization checks that prevent IDOR attacks.
These functions are the single source of truth for resource-scope
authorization — never scatter `if resource.tenant_id != actor.tenant_id`
through hundreds of routes.

TENANT RULE
    Actor Tenant A → Resource Tenant B → DENY

VENUE RULE
    Actor with Venue 1 access → Resource Venue 2 → DENY
    unless ActorContext has tenant-wide venue scope (ALL_VENUES).

CLIENT TRUST
    /resource?tenant_id=A          → does NOT authorize Tenant A
    /resource?venue_id=1           → does NOT authorize Venue 1
    These are resource selectors only. ActorContext determines authorization.
"""

from __future__ import annotations

from backend.app.infrastructure.auth.exceptions import AuthorizationError
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext


def require_same_tenant(
    actor: ActorContext,
    resource_tenant_id: TenantId,
) -> None:
    """Verify the resource belongs to the actor's tenant.

    Prevents cross-tenant IDOR — even if the actor knows a valid
    Tenant B resource UUID, access is denied.

    Args:
        actor: The authenticated actor's context.
        resource_tenant_id: The tenant ID of the requested resource.

    Raises:
        AuthorizationError (→ 403) if the actor's tenant does not
        match the resource tenant.
    """
    if actor.tenant_id != resource_tenant_id:
        msg = f"Tenant mismatch: actor={actor.tenant_id}, resource={resource_tenant_id}"
        raise AuthorizationError(msg)


def require_venue_access(
    actor: ActorContext,
    venue_id: VenueId,
) -> None:
    """Verify the actor has access to the specified venue.

    Empty venue_scope (ALL_VENUES membership) grants access to every
    venue within the tenant. SPECIFIC_VENUES membership checks the
    venue is in the actor's explicit venue scope.

    This differs from ActorContext.has_venue_access() which treats empty
    scope as "no access" — here, empty scope means tenant-wide access.

    Args:
        actor: The authenticated actor's context.
        venue_id: The venue to check access for.

    Raises:
        AuthorizationError (→ 403) if the actor lacks access to the venue.
    """
    # Empty venue_scope means ALL_VENUES — tenant-wide access
    if not actor.venue_scope:
        return

    if venue_id not in actor.venue_scope:
        msg = f"No access to venue: {venue_id}"
        raise AuthorizationError(msg)


def require_tenant_venue_access(
    actor: ActorContext,
    resource_tenant_id: TenantId,
    venue_id: VenueId,
) -> None:
    """Verify tenant scope AND venue access in a single call.

    Combines require_same_tenant and require_venue_access so routes
    don't scatter two separate checks.

    Args:
        actor: The authenticated actor's context.
        resource_tenant_id: The tenant ID of the requested resource.
        venue_id: The venue to check access for.

    Raises:
        AuthorizationError (→ 403) if tenant mismatch or no venue access.
    """
    require_same_tenant(actor, resource_tenant_id)
    require_venue_access(actor, venue_id)
