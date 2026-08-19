"""Integration tests for Task 5.8 — Tenant/Venue safe repositories.

Tests prove cross-tenant and cross-venue isolation at the database
query level. Requires a running PostgreSQL instance (see infrastructure
compose.yaml) and INTEGRATION_TESTS=1 environment variable.

Run:
    docker compose -f infrastructure/docker/compose.yaml up -d postgres
    INTEGRATION_TESTS=1 pytest tests/integration/test_identity_repositories.py -v

Each test:
1. Seeds test data with two tenants, venues, users, roles, memberships
2. Creates ActorContext for Tenant A and Tenant B
3. Executes scoped repository operations
4. Verifies cross-tenant/venue data cannot be accessed
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.identity import (
    MembershipModel,
    RoleModel,
    TenantModel,
    UserModel,
    VenueModel,
)
from backend.app.infrastructure.database.models.integrations import (
    METADATA_NO_SECRETS_FUNCTION_SQL,
)
from backend.app.infrastructure.database.repositories.identity import (
    TenantRepository,
    VenueRepository,
)
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext, RoleName, permissions_for_role

# Skip if INTEGRATION_TESTS not set
pytestmark = [pytest.mark.integration]

need_postgres = pytest.mark.skipif(
    not os.environ.get("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 and start PostgreSQL (docker compose -f infrastructure/docker/compose.yaml up -d postgres)",
)

_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hotelops:CHANGE_ME@localhost:5433/hotelops",
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture(scope="function")
async def seeded_session():
    """Create a session with a fresh engine and seeded test data.

    Each test gets its own engine and session to avoid event-loop
    conflicts (same approach as test_rls.py).
    """
    e = create_async_engine(_DATABASE_URL, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(e, expire_on_commit=False)
    async with e.begin() as conn:
        # Migration 013 creates this helper; create_all-only fixtures must too
        # (the integrations CHECK references it).
        await conn.execute(text(METADATA_NO_SECRETS_FUNCTION_SQL))
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        await _seed_data(s)
        yield s
    await e.dispose()


async def _seed_data(session: AsyncSession) -> None:
    """Seed minimal identity data for integration testing."""

    # Clear any existing data first
    for table in ("membership_venues", "memberships", "venues", "tenants", "users", "roles"):
        await session.execute(text(f"DELETE FROM {table}"))

    # --- Tenants ---
    tenant_a = TenantModel(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="Tenant A",
        status="active",
    )
    tenant_b = TenantModel(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        name="Tenant B",
        status="active",
    )
    session.add_all([tenant_a, tenant_b])

    # --- Roles ---
    admin_role = RoleModel(
        role_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        name="admin",
    )
    operator_role = RoleModel(
        role_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
        name="operator",
    )
    session.add_all([admin_role, operator_role])

    # --- Venues (2 per tenant) ---
    venue_a1 = VenueModel(
        venue_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        tenant_id=tenant_a.tenant_id,
        name="Venue A-1",
        status="active",
    )
    venue_a2 = VenueModel(
        venue_id=uuid.UUID("00000000-0000-0000-0000-000000000021"),
        tenant_id=tenant_a.tenant_id,
        name="Venue A-2",
        status="active",
    )
    venue_b1 = VenueModel(
        venue_id=uuid.UUID("00000000-0000-0000-0000-000000000022"),
        tenant_id=tenant_b.tenant_id,
        name="Venue B-1",
        status="active",
    )
    venue_b2 = VenueModel(
        venue_id=uuid.UUID("00000000-0000-0000-0000-000000000023"),
        tenant_id=tenant_b.tenant_id,
        name="Venue B-2",
        status="active",
    )
    session.add_all([venue_a1, venue_a2, venue_b1, venue_b2])

    # --- Users ---
    user_a = UserModel(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        display_name="User A",
        email="user_a@example.com",
        status="active",
    )
    user_b = UserModel(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000031"),
        display_name="User B",
        email="user_b@example.com",
        status="active",
    )
    session.add_all([user_a, user_b])

    # --- Memberships ---
    membership_a_admin = MembershipModel(
        membership_id=uuid.UUID("00000000-0000-0000-0000-000000000040"),
        user_id=user_a.user_id,
        tenant_id=tenant_a.tenant_id,
        role_id=admin_role.role_id,
        scope="all_venues",
        status="active",
    )
    membership_b_admin = MembershipModel(
        membership_id=uuid.UUID("00000000-0000-0000-0000-000000000041"),
        user_id=user_b.user_id,
        tenant_id=tenant_b.tenant_id,
        role_id=admin_role.role_id,
        scope="all_venues",
        status="active",
    )
    session.add_all([membership_a_admin, membership_b_admin])

    await session.flush()


# =============================================================================
# ActorContext helpers
# =============================================================================


def _actor_a(venue_ids: set[VenueId] | None = None) -> ActorContext:
    """Actor A — full admin of Tenant A with ALL_VENUES scope."""
    return ActorContext(
        actor_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000030")),
        tenant_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000001")),
        role_name=RoleName.ADMIN,
        permissions=permissions_for_role(RoleName.ADMIN),
        venue_scope=frozenset(venue_ids or set()),
        authenticated_at=datetime.now(UTC),
    )


def _actor_a_venued(venue_ids: set[VenueId]) -> ActorContext:
    """Actor A — operator with SPECIFIC_VENUES scope."""
    return ActorContext(
        actor_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000030")),
        tenant_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000001")),
        role_name=RoleName.OPERATOR,
        permissions=permissions_for_role(RoleName.OPERATOR),
        venue_scope=frozenset(venue_ids),
        authenticated_at=datetime.now(UTC),
    )


def _actor_b(venue_ids: set[VenueId] | None = None) -> ActorContext:
    """Actor B — full admin of Tenant B with ALL_VENUES scope."""
    return ActorContext(
        actor_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000031")),
        tenant_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000002")),
        role_name=RoleName.ADMIN,
        permissions=permissions_for_role(RoleName.ADMIN),
        venue_scope=frozenset(venue_ids or set()),
        authenticated_at=datetime.now(UTC),
    )


# =============================================================================
# Shared IDs
# =============================================================================

TENANT_A_ID = TenantId(uuid.UUID("00000000-0000-0000-0000-000000000001"))
TENANT_B_ID = TenantId(uuid.UUID("00000000-0000-0000-0000-000000000002"))

VENUE_A1_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000020"))
VENUE_A2_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000021"))
VENUE_B1_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000022"))
VENUE_B2_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000023"))


# =============================================================================
# Tenant Repository Tests
# =============================================================================


@need_postgres
class TestTenantRepositoryCrossTenant:
    """Prove Tenant A cannot access Tenant B data via TenantRepository."""

    async def test_tenant_a_can_get_own_tenant(self, seeded_session: AsyncSession) -> None:
        repo = TenantRepository(seeded_session)
        actor = _actor_a()
        result = await repo.get_for_actor(actor, TENANT_A_ID)
        assert result is not None
        assert result.name == "Tenant A"

    async def test_tenant_a_cannot_get_tenant_b(self, seeded_session: AsyncSession) -> None:
        repo = TenantRepository(seeded_session)
        actor = _actor_a()
        result = await repo.get_for_actor(actor, TENANT_B_ID)
        assert result is None  # No leak — returns None without error

    async def test_tenant_b_cannot_get_tenant_a(self, seeded_session: AsyncSession) -> None:
        repo = TenantRepository(seeded_session)
        actor = _actor_b()
        result = await repo.get_for_actor(actor, TENANT_A_ID)
        assert result is None

    async def test_tenant_a_cannot_update_tenant_b(self, seeded_session: AsyncSession) -> None:
        repo = TenantRepository(seeded_session)
        actor = _actor_a()
        result = await repo.update_for_actor(actor, TENANT_B_ID, name="Hacked!")
        assert result is None  # No leak

        # Verify Tenant B's name was NOT changed
        other_repo = TenantRepository(seeded_session)
        actor_b = _actor_b()
        tenant_b = await other_repo.get_for_actor(actor_b, TENANT_B_ID)
        assert tenant_b is not None
        assert tenant_b.name == "Tenant B"  # Still original

    async def test_tenant_b_cannot_update_tenant_a(self, seeded_session: AsyncSession) -> None:
        repo = TenantRepository(seeded_session)
        actor = _actor_b()
        result = await repo.update_for_actor(actor, TENANT_A_ID, name="Compromised")
        assert result is None

        actor_a = _actor_a()
        tenant_a = await repo.get_for_actor(actor_a, TENANT_A_ID)
        assert tenant_a is not None
        assert tenant_a.name == "Tenant A"


# =============================================================================
# Venue Repository Tests — Cross-Tenant
# =============================================================================


@need_postgres
class TestVenueRepositoryCrossTenant:
    """Prove Tenant A cannot access Tenant B venue data."""

    async def test_tenant_a_can_get_own_venue(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()  # ALL_VENUES
        result = await repo.get_for_actor(actor, VENUE_A1_ID)
        assert result is not None
        assert result.name == "Venue A-1"

    async def test_tenant_a_cannot_get_venue_b(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()
        result = await repo.get_for_actor(actor, VENUE_B1_ID)
        assert result is None  # No leak

    async def test_tenant_b_cannot_get_venue_a(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_b()
        result = await repo.get_for_actor(actor, VENUE_A1_ID)
        assert result is None

    async def test_list_does_not_leak_foreign_venues(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)

        actor_a = _actor_a()
        venues_a = await repo.list_for_actor(actor_a)
        venue_a_ids = {v.venue_id for v in venues_a}
        assert VENUE_A1_ID in venue_a_ids
        assert VENUE_A2_ID in venue_a_ids
        assert VENUE_B1_ID not in venue_a_ids
        assert VENUE_B2_ID not in venue_a_ids

        actor_b = _actor_b()
        venues_b = await repo.list_for_actor(actor_b)
        venue_b_ids = {v.venue_id for v in venues_b}
        assert VENUE_B1_ID in venue_b_ids
        assert VENUE_B2_ID in venue_b_ids
        assert VENUE_A1_ID not in venue_b_ids

    async def test_count_does_not_leak_foreign_data(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor_a = _actor_a()
        actor_b = _actor_b()

        count_a = await repo.count_for_actor(actor_a)
        count_b = await repo.count_for_actor(actor_b)

        assert count_a == 2  # Tenant A has 2 venues
        assert count_b == 2  # Tenant B has 2 venues

    async def test_tenant_a_cannot_update_venue_b(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()
        result = await repo.update_for_actor(actor, VENUE_B1_ID, name="Hacked!")
        assert result is None

        # Verify no change
        actor_b = _actor_b()
        venue_b1 = await repo.get_for_actor(actor_b, VENUE_B1_ID)
        assert venue_b1 is not None
        assert venue_b1.name == "Venue B-1"

    async def test_tenant_b_cannot_update_venue_a(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_b()
        result = await repo.update_for_actor(actor, VENUE_A1_ID, name="Compromised")
        assert result is None

        actor_a = _actor_a()
        venue_a1 = await repo.get_for_actor(actor_a, VENUE_A1_ID)
        assert venue_a1 is not None
        assert venue_a1.name == "Venue A-1"

    async def test_tenant_a_cannot_delete_venue_b(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()
        deleted = await repo.delete_for_actor(actor, VENUE_B1_ID)
        assert deleted is False  # Not deleted

        # Verify venue still exists for Tenant B
        actor_b = _actor_b()
        venue_b1 = await repo.get_for_actor(actor_b, VENUE_B1_ID)
        assert venue_b1 is not None

    async def test_tenant_b_cannot_delete_venue_a(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_b()
        deleted = await repo.delete_for_actor(actor, VENUE_A1_ID)
        assert deleted is False

        actor_a = _actor_a()
        venue_a1 = await repo.get_for_actor(actor_a, VENUE_A1_ID)
        assert venue_a1 is not None

    async def test_tenant_a_can_delete_own_venue(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()
        deleted = await repo.delete_for_actor(actor, VENUE_A2_ID)
        assert deleted is True


# =============================================================================
# Venue Repository Tests — Cross-Venue (within same tenant)
# =============================================================================


@need_postgres
class TestVenueRepositoryCrossVenue:
    """Prove actor with SPECIFIC_VENUES cannot access unauthorized venues."""

    async def test_actor_with_venue_a1_can_access_venue_a1(
        self, seeded_session: AsyncSession
    ) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a_venued({VENUE_A1_ID})
        result = await repo.get_for_actor(actor, VENUE_A1_ID)
        assert result is not None
        assert result.name == "Venue A-1"

    async def test_actor_with_venue_a1_cannot_access_venue_a2(
        self, seeded_session: AsyncSession
    ) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a_venued({VENUE_A1_ID})
        result = await repo.get_for_actor(actor, VENUE_A2_ID)
        assert result is None  # Same tenant, different venue — blocked

    async def test_list_respects_venue_scope(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a_venued({VENUE_A1_ID})
        venues = await repo.list_for_actor(actor)
        venue_ids = {v.venue_id for v in venues}
        assert VENUE_A1_ID in venue_ids
        assert VENUE_A2_ID not in venue_ids  # Venue A-2 not in scope
        assert VENUE_B1_ID not in venue_ids  # Cross-tenant

    async def test_count_respects_venue_scope(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a_venued({VENUE_A1_ID})
        count = await repo.count_for_actor(actor)
        assert count == 1  # Only VENUE_A1 is accessible

    async def test_update_respects_venue_scope(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a_venued({VENUE_A1_ID})

        # Can update accessible venue
        updated = await repo.update_for_actor(actor, VENUE_A1_ID, name="Venue A-1 Updated")
        assert updated is not None
        assert updated.name == "Venue A-1 Updated"

        # Cannot update inaccessible venue
        blocked = await repo.update_for_actor(actor, VENUE_A2_ID, name="Hacked!")
        assert blocked is None

    async def test_delete_respects_venue_scope(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a_venued({VENUE_A1_ID})

        # Can delete accessible venue
        deleted = await repo.delete_for_actor(actor, VENUE_A1_ID)
        assert deleted is True

        # Cannot delete inaccessible venue
        blocked = await repo.delete_for_actor(actor, VENUE_A2_ID)
        assert blocked is False

    async def test_all_venues_scope_grants_access_to_all_tenant_venues(
        self, seeded_session: AsyncSession
    ) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()  # ALL_VENUES (empty venue_scope)
        venues = await repo.list_for_actor(actor)
        venue_ids = {v.venue_id for v in venues}
        assert VENUE_A1_ID in venue_ids
        assert VENUE_A2_ID in venue_ids
        assert VENUE_B1_ID not in venue_ids  # Still cross-tenant protected
        assert len(venues) == 2

    async def test_random_venue_uuid_returns_none(self, seeded_session: AsyncSession) -> None:
        repo = VenueRepository(seeded_session)
        actor = _actor_a()
        random_vid = VenueId(uuid.uuid4())
        result = await repo.get_for_actor(actor, random_vid)
        assert result is None  # Random UUID within same tenant — returns None
