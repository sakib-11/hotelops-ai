"""Evidence authorization policy (Task 17.8).

Prevents evidence leakage across tenants and venues. Reuses Task 5
authorization (``require_tenant_venue_access``, ``ActorContext``,
``AuthorizationError``) as the single source of truth — no evidence
route/service scatters its own ``if tenant != ...`` checks.

Every evidence operation enforces, in order:

    TENANT      — actor.tenant_id == evidence.tenant_id
    VENUE       — actor venue scope contains evidence.venue_id
                  (empty venue_scope = tenant-wide access)
    PERMISSION  — the operation's required capability
                  (EVIDENCE_READ read ops; EVIDENCE_MANAGE destructive ops)
    OWNERSHIP   — resource ownership resolved server-side (the evidence
                  row), never from client input

CLIENT TRUST (NEVER):
    - tenant_id from the request body          → a resource selector only
    - venue_id from query parameters           → a resource selector only
    - storage key alone                        → never authorizes; the
      object key is resolved to the owned evidence row first, and ONLY
      the row's tenant/venue authorize access.

The actor is ALWAYS the server-side ``ActorContext`` (Task 5): expired,
invalid, or disabled actors are rejected here as defense-in-depth (the
JWT layer already rejects expired tokens at the boundary).

Usage:
    authorizer = EvidenceAuthorizer()
    authorizer.authorize(actor, EvidenceOperation.RETRIEVE, evidence)
    # or for an object-key lookup that has not been resolved yet:
    authorizer.require_scope(actor, tenant_id, venue_id, operation)
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.auth.scope import (
    require_same_tenant,
    require_venue_access,
)
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext, Permission


class EvidenceOperation(StrEnum):
    """Every operation the evidence layer must authorize."""

    CREATE = "create"
    RETRIEVE = "retrieve"
    METADATA = "metadata"
    SIGNED_URL = "signed_url"
    DELETE = "delete"
    RETENTION = "retention"


# Operation → required capability. Destructive/management operations
# require EVIDENCE_MANAGE (admin/manager); read-style operations require
# EVIDENCE_READ. An OPERATOR holds EVIDENCE_READ but NOT EVIDENCE_MANAGE,
# so delete/retention are denied for operators (unauthorized role).
_OPERATION_PERMISSIONS: dict[EvidenceOperation, Permission] = {
    EvidenceOperation.CREATE: Permission.EVIDENCE_READ,
    EvidenceOperation.RETRIEVE: Permission.EVIDENCE_READ,
    EvidenceOperation.METADATA: Permission.EVIDENCE_READ,
    EvidenceOperation.SIGNED_URL: Permission.EVIDENCE_READ,
    EvidenceOperation.DELETE: Permission.EVIDENCE_MANAGE,
    EvidenceOperation.RETENTION: Permission.EVIDENCE_MANAGE,
}


class EvidenceAuthorizer:
    """Centralized authorization for all evidence operations (Task 17.8)."""

    # ------------------------------------------------------------------
    # Operation-level checks
    # ------------------------------------------------------------------

    def authorize(
        self,
        actor: ActorContext,
        operation: EvidenceOperation,
        evidence_tenant_id: TenantId,
        evidence_venue_id: VenueId,
        *,
        now: datetime | None = None,
    ) -> None:
        """Authorize an operation against a RESOLVED evidence row's scope.

        The tenant/venue are the evidence row's OWNER scope — resolved
        server-side by the repository (never from the request body/query
        params). The storage key never reaches this method; ownership is
        proven by the row before authorization.

        Raises:
            AuthorizationError (→ 403) on any denial.
        """
        self.require_valid_actor(actor, now=now)
        self.require_scope(actor, evidence_tenant_id, evidence_venue_id)
        self.require_permission(actor, operation)

    def require_scope(
        self,
        actor: ActorContext,
        evidence_tenant_id: TenantId,
        evidence_venue_id: VenueId,
    ) -> None:
        """Enforce tenant + venue scope for a resolved evidence row.

        Tenant and venue are checked separately so an actor with
        tenant-wide venue access (empty venue_scope) still cannot cross
        tenants, and a tenant-matched actor still cannot cross venues.
        """
        require_same_tenant(actor, evidence_tenant_id)
        require_venue_access(actor, evidence_venue_id)

    def require_permission(
        self,
        actor: ActorContext,
        operation: EvidenceOperation,
    ) -> None:
        """Enforce the operation's required capability on the actor."""
        required = _OPERATION_PERMISSIONS[operation]
        if not actor.has_permission(required):
            msg = f"Missing required permission: {required.value} (operation {operation.value})"
            raise AuthorizationError(msg)

    def require_valid_actor(
        self,
        actor: ActorContext,
        *,
        now: datetime | None = None,
    ) -> None:
        """Reject expired, invalid, or disabled actors (defense-in-depth).

        The JWT boundary rejects expired/invalid tokens (401); this
        check guards the authorization layer itself so a disabled or
        stale actor context can never authorize evidence access.
        """
        if not actor.active:
            msg = f"Actor {actor.actor_id} is not active — access denied"
            raise AuthorizationError(msg)
        reference = now if now is not None else datetime.now(UTC)
        if actor.authenticated_at > reference:
            msg = f"Actor {actor.actor_id} has an invalid (future) authentication time"
            raise AuthorizationError(msg)

    # ------------------------------------------------------------------
    # Storage-key resolution (never trust the key alone)
    # ------------------------------------------------------------------

    @staticmethod
    def authorize_object_key_scope(
        actor: ActorContext,
        evidence_tenant_id: TenantId,
        evidence_venue_id: VenueId,
    ) -> None:
        """Authorize AFTER the object key has been resolved to a row.

        The key itself is NEVER an authorization input. The caller must
        first resolve the key to the owned evidence row (repository),
        then pass the row's tenant/venue here. This exists so callers
        don't scatter ``require_scope`` on the hot path and to document
        the resolution-first contract explicitly.
        """
        require_same_tenant(actor, evidence_tenant_id)
        require_venue_access(actor, evidence_venue_id)
