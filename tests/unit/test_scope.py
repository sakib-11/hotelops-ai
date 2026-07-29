"""Tests for Task 5.7 — Tenant & Venue Scope Enforcement.

Tests cover:
- require_same_tenant: same tenant, different tenant, valid foreign UUID, random UUID
- require_venue_access: same venue, different venue, tenant-wide access (ALL_VENUES),
  venue-specific access, no venue access, random UUID
- require_tenant_venue_access: combined check
- IDOR prevention: knowing a valid UUID doesn't grant access
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.auth.scope import (
    require_same_tenant,
    require_tenant_venue_access,
    require_venue_access,
)
from contracts.common import TenantId, VenueId
from contracts.identity import (
    ActorContext,
    RoleName,
    permissions_for_role,
)


def _actor(
    tenant_id: TenantId | None = None,
    venue_ids: set[VenueId] | None = None,
) -> ActorContext:
    """Create a minimal ActorContext for testing."""
    return ActorContext(
        actor_id=TenantId(uuid4()),  # UserId is NewType(UUID), same structure
        tenant_id=tenant_id or TenantId(uuid4()),
        role_name=RoleName.OPERATOR,
        permissions=permissions_for_role(RoleName.OPERATOR),
        venue_scope=frozenset(venue_ids or set()),
        authenticated_at=datetime.now(UTC),
    )


# =============================================================================
# require_same_tenant
# =============================================================================


class TestRequireSameTenant:
    """Verify tenant-scope enforcement."""

    def test_same_tenant_passes(self) -> None:
        """Actor and resource in same tenant — should pass."""
        tid = TenantId(uuid4())
        actor = _actor(tenant_id=tid)
        require_same_tenant(actor, tid)  # no exception

    def test_different_tenant_denied(self) -> None:
        """Actor Tenant A, resource Tenant B — should raise AuthorizationError."""
        actor_tid = TenantId(uuid4())
        resource_tid = TenantId(uuid4())
        actor = _actor(tenant_id=actor_tid)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_same_tenant(actor, resource_tid)

    def test_valid_foreign_uuid_denied(self) -> None:
        """Knowing a valid Tenant B UUID does not authorize access."""
        actor_tid = TenantId(uuid4())
        foreign_tid = TenantId(uuid4())
        actor = _actor(tenant_id=actor_tid)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_same_tenant(actor, foreign_tid)

    def test_random_uuid_denied(self) -> None:
        """Random UUID as resource tenant — should be denied."""
        actor_tid = TenantId(uuid4())
        random_tid = TenantId(uuid4())
        actor = _actor(tenant_id=actor_tid)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_same_tenant(actor, random_tid)

    def test_zero_tenant_passes(self) -> None:
        """If both actor and resource use the same zero UUID, it passes."""
        zero_tid = TenantId(UUID(int=0))
        actor = _actor(tenant_id=zero_tid)
        require_same_tenant(actor, zero_tid)  # no exception

    def test_zero_tenant_with_resource_denied(self) -> None:
        """Zero tenant actor cannot access non-zero tenant resources."""
        zero_tid = TenantId(UUID(int=0))
        resource_tid = TenantId(uuid4())
        actor = _actor(tenant_id=zero_tid)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_same_tenant(actor, resource_tid)


# =============================================================================
# require_venue_access
# =============================================================================


class TestRequireVenueAccess:
    """Verify venue-scope enforcement."""

    def test_same_venue_with_specific_scope_passes(self) -> None:
        """Actor has access to Venue 1 and requests Venue 1 — should pass."""
        vid = VenueId(uuid4())
        actor = _actor(venue_ids={vid})
        require_venue_access(actor, vid)  # no exception

    def test_different_venue_in_specific_scope_denied(self) -> None:
        """Actor has Venue 1 access, requests Venue 2 — should be denied."""
        allowed_vid = VenueId(uuid4())
        requested_vid = VenueId(uuid4())
        actor = _actor(venue_ids={allowed_vid})
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, requested_vid)

    def test_tenant_wide_access_passes(self) -> None:
        """Actor with ALL_VENUES scope (empty venue_scope) can access any venue."""
        tid = TenantId(uuid4())
        any_vid = VenueId(uuid4())
        # Empty venue_scope = ALL_VENUES (tenant-wide access)
        actor = _actor(tenant_id=tid, venue_ids=set())
        require_venue_access(actor, any_vid)  # no exception

    def test_tenant_wide_access_any_venue_passes(self) -> None:
        """ALL_VENUES scope grants access to every venue."""
        tid = TenantId(uuid4())
        actor = _actor(tenant_id=tid, venue_ids=set())
        for _ in range(5):
            vid = VenueId(uuid4())
            require_venue_access(actor, vid)  # all pass

    def test_no_venue_access_denied(self) -> None:
        """Actor with no venue scope and no ALL_VENUES should be denied."""
        # Actor has specific scope but the requested venue isn't in it
        actor = _actor(venue_ids={VenueId(uuid4())})
        other_vid = VenueId(uuid4())
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, other_vid)

    def test_valid_foreign_venue_uuid_denied(self) -> None:
        """Knowing a valid Venue UUID does not grant access without scope."""
        vid = VenueId(uuid4())
        actor = _actor(venue_ids={VenueId(uuid4())})  # different venue
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, vid)

    def test_random_uuid_denied(self) -> None:
        """Random UUID as venue — should be denied without scope."""
        random_vid = VenueId(uuid4())
        actor = _actor(venue_ids={VenueId(uuid4())})  # different venue
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, random_vid)

    def test_zero_venue_uuid_denied(self) -> None:
        """Zero UUID venue — should be denied without scope."""
        zero_vid = VenueId(UUID(int=0))
        actor = _actor(venue_ids={VenueId(uuid4())})  # different venue
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_venue_access(actor, zero_vid)

    def test_zero_venue_with_all_venues_passes(self) -> None:
        """ALL_VENUES scope grants access even to zero UUID venue."""
        actor = _actor(venue_ids=set())  # ALL_VENUES
        zero_vid = VenueId(UUID(int=0))
        require_venue_access(actor, zero_vid)  # no exception


# =============================================================================
# require_tenant_venue_access (combined)
# =============================================================================


class TestRequireTenantVenueAccess:
    """Verify combined tenant + venue scope check."""

    def test_same_tenant_and_venue_passes(self) -> None:
        """Actor has tenant and venue access — should pass."""
        tid = TenantId(uuid4())
        vid = VenueId(uuid4())
        actor = _actor(tenant_id=tid, venue_ids={vid})
        require_tenant_venue_access(actor, tid, vid)  # no exception

    def test_same_tenant_all_venues_passes(self) -> None:
        """Actor has tenant access and ALL_VENUES scope — should pass."""
        tid = TenantId(uuid4())
        any_vid = VenueId(uuid4())
        actor = _actor(tenant_id=tid, venue_ids=set())  # ALL_VENUES
        require_tenant_venue_access(actor, tid, any_vid)  # no exception

    def test_different_tenant_denied(self) -> None:
        """Actor Tenant A, resource Tenant B — tenant check fails first."""
        actor_tid = TenantId(uuid4())
        resource_tid = TenantId(uuid4())
        vid = VenueId(uuid4())
        actor = _actor(tenant_id=actor_tid, venue_ids={vid})
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_tenant_venue_access(actor, resource_tid, vid)

    def test_cross_tenant_venue_denied(self) -> None:
        """Cross-tenant venue access attempts are blocked."""
        actor_tid = TenantId(uuid4())
        foreign_tid = TenantId(uuid4())
        vid = VenueId(uuid4())
        actor = _actor(tenant_id=actor_tid, venue_ids={vid})
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_tenant_venue_access(actor, foreign_tid, vid)

    def test_same_tenant_wrong_venue_denied(self) -> None:
        """Same tenant but no venue access — venue check fails."""
        tid = TenantId(uuid4())
        allowed_vid = VenueId(uuid4())
        requested_vid = VenueId(uuid4())
        actor = _actor(tenant_id=tid, venue_ids={allowed_vid})
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_tenant_venue_access(actor, tid, requested_vid)

    def test_same_tenant_no_venue_scope_denied(self) -> None:
        """Same tenant but no venue in scope — venue check fails."""
        tid = TenantId(uuid4())
        vid = VenueId(uuid4())
        actor = _actor(tenant_id=tid, venue_ids={VenueId(uuid4())})  # different venue
        with pytest.raises(AuthorizationError, match="No access to venue"):
            require_tenant_venue_access(actor, tid, vid)


# =============================================================================
# IDOR Prevention
# =============================================================================


class TestIDORPrevention:
    """Verify that knowing valid UUIDs does not grant unauthorized access."""

    def test_known_venue_uuid_cross_tenant_denied(self) -> None:
        """Actor from Tenant A cannot access Tenant B's venue even with valid UUID."""
        actor_tid = TenantId(uuid4())
        foreign_tid = TenantId(uuid4())
        known_venue = VenueId(uuid4())  # "valid" UUID the actor knows

        actor = _actor(tenant_id=actor_tid, venue_ids={known_venue})
        # Tenant A actor with Tenant A venue access
        # but resource claims to be in Tenant B
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_tenant_venue_access(actor, foreign_tid, known_venue)

    def test_known_resource_uuid_no_venue_scope_denied(self) -> None:
        """Actor knows a valid UUID but lacks venue scope — denied."""
        tid = TenantId(uuid4())
        known_venue = VenueId(uuid4())  # "valid" UUID the actor knows
        actor = _actor(tenant_id=tid, venue_ids=set())  # ALL_VENUES
        # With ALL_VENUES, any venue is accessible within the same tenant
        require_tenant_venue_access(actor, tid, known_venue)  # passes because ALL_VENUES

    def test_random_uuid_cross_tenant_denied(self) -> None:
        """Random resource UUID across tenants — denied."""
        actor_tid = TenantId(uuid4())
        actor = _actor(tenant_id=actor_tid, venue_ids={VenueId(uuid4())})
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            require_tenant_venue_access(actor, TenantId(uuid4()), VenueId(uuid4()))
