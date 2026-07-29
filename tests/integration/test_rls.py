"""Integration tests for Task 5.9 — PostgreSQL Row-Level Security.

Tests prove that RLS enforces tenant isolation at the database level
as a defense-in-depth layer beneath application authorization and
repository scoping.

Requires PostgreSQL with the hotelops_app role and RLS enabled
(run migration 002). Set INTEGRATION_TESTS=1 to execute.

Run:
    docker compose -f infrastructure/docker/compose.yaml up -d postgres
    INTEGRATION_TESTS=1 pytest tests/integration/test_rls.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.identity import (
    MembershipModel,
    RoleModel,
    TenantModel,
    UserModel,
    VenueModel,
)
from backend.app.infrastructure.database.repositories.identity import (
    VenueRepository,
)
from backend.app.infrastructure.database.rls import clear_rls_on_session, set_rls_on_session
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext, RoleName, permissions_for_role

# Skip if INTEGRATION_TESTS not set
need_postgres = pytest.mark.skipif(
    not os.environ.get("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 and start PostgreSQL (docker compose -f infrastructure/docker/compose.yaml up -d postgres)",
)

# Connect as the application role (NOBYPASSRLS) for RLS tests
_APP_DATABASE_URL = os.environ.get(
    "DATABASE_URL_APP",
    "postgresql+asyncpg://hotelops_app:CHANGE_ME@localhost:5433/hotelops",
)

# Connect as the admin role (bypasses RLS) for seed/setup
_ADMIN_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hotelops:CHANGE_ME@localhost:5433/hotelops",
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture(scope="module")
async def admin_engine():
    """Admin engine — bypasses RLS for setup/verification.

    Creates tables then enables RLS and creates policies matching
    the production migration (002_enable_rls.py).
    """
    e = create_async_engine(_ADMIN_DATABASE_URL, pool_size=2, max_overflow=0)
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Enable RLS on tenant-scoped tables
        await conn.execute(text("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE tenants FORCE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE venues ENABLE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE venues FORCE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE memberships FORCE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE membership_venues ENABLE ROW LEVEL SECURITY;"))
        await conn.execute(text("ALTER TABLE membership_venues FORCE ROW LEVEL SECURITY;"))

        # Create RLS policies
        await _create_rls_policies(conn)

    yield e
    await e.dispose()


async def _create_rls_policies(conn: AsyncConnection) -> None:
    """Create RLS policies matching the production migration.

    Drops existing policies first so this is idempotent across test runs.
    """
    # Drop policies if they already exist (from a previous test run)
    for tbl, pol in [
        ("tenants", "tenants_select"),
        ("tenants", "tenants_insert"),
        ("tenants", "tenants_update"),
        ("tenants", "tenants_delete"),
        ("venues", "venues_all"),
        ("memberships", "memberships_all"),
        ("membership_venues", "membership_venues_all"),
    ]:
        await conn.execute(text(f"DROP POLICY IF EXISTS {pol} ON {tbl}"))

    # Tenants — separate policies for each operation
    await conn.execute(
        text("""
        CREATE POLICY tenants_select ON tenants FOR SELECT TO hotelops_app
        USING (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid);
    """)
    )
    await conn.execute(
        text("""
        CREATE POLICY tenants_insert ON tenants FOR INSERT TO hotelops_app
        WITH CHECK (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid);
    """)
    )
    await conn.execute(
        text("""
        CREATE POLICY tenants_update ON tenants FOR UPDATE TO hotelops_app
        USING (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid)
        WITH CHECK (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid);
    """)
    )
    await conn.execute(
        text("""
        CREATE POLICY tenants_delete ON tenants FOR DELETE TO hotelops_app
        USING (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid);
    """)
    )
    # Venues — unified policy
    await conn.execute(
        text("""
        CREATE POLICY venues_all ON venues FOR ALL TO hotelops_app
        USING (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid)
        WITH CHECK (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid);
    """)
    )
    # Memberships — unified policy
    await conn.execute(
        text("""
        CREATE POLICY memberships_all ON memberships FOR ALL TO hotelops_app
        USING (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid)
        WITH CHECK (tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid);
    """)
    )
    # Membership-Venues — scoped via membership subquery
    await conn.execute(
        text("""
        CREATE POLICY membership_venues_all ON membership_venues FOR ALL TO hotelops_app
        USING (membership_id IN (
            SELECT membership_id FROM memberships
            WHERE tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
        ))
        WITH CHECK (membership_id IN (
            SELECT membership_id FROM memberships
            WHERE tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
        ));
    """)
    )


@pytest_asyncio.fixture
async def app_session():
    """Application session — RLS enforced via the app role.

    Creates its own engine to avoid event-loop conflicts with
    module-scoped fixtures.
    """
    e = create_async_engine(_APP_DATABASE_URL, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(e, expire_on_commit=False)
    async with factory() as s:
        yield s
    await e.dispose()


@pytest_asyncio.fixture(autouse=True)
async def seed_data():
    """Seed test data before each test using admin.

    Creates its own engine to avoid event-loop conflicts with
    the module-scoped admin_engine (which handles table + RLS
    setup once per module).
    """
    e = create_async_engine(_ADMIN_DATABASE_URL, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(e, expire_on_commit=False)
    async with factory() as s:
        for table in (
            "membership_venues",
            "memberships",
            "venues",
            "tenants",
            "users",
            "roles",
        ):
            await s.execute(text(f"DELETE FROM {table}"))
        await _seed_data(s)
        await s.commit()
    await e.dispose()


async def _seed_data(session: AsyncSession) -> None:
    """Seed minimal identity data for RLS testing."""
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

    admin_role = RoleModel(
        role_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        name="admin",
    )
    session.add_all([admin_role])

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
    session.add_all([venue_a1, venue_a2, venue_b1])

    user_a = UserModel(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        display_name="User A",
        email="user_a@example.com",
        status="active",
    )
    session.add_all([user_a])

    membership_a = MembershipModel(
        membership_id=uuid.UUID("00000000-0000-0000-0000-000000000040"),
        user_id=user_a.user_id,
        tenant_id=tenant_a.tenant_id,
        role_id=admin_role.role_id,
        scope="all_venues",
        status="active",
    )
    session.add_all([membership_a])


# =============================================================================
# Helpers
# =============================================================================

TENANT_A_ID_STR = "00000000-0000-0000-0000-000000000001"
TENANT_B_ID_STR = "00000000-0000-0000-0000-000000000002"
TENANT_A_ID = TenantId(uuid.UUID(TENANT_A_ID_STR))
TENANT_B_ID = TenantId(uuid.UUID(TENANT_B_ID_STR))
VENUE_A1_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000020"))
VENUE_A2_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000021"))
VENUE_B1_ID = VenueId(uuid.UUID("00000000-0000-0000-0000-000000000022"))

A_TENANT_A = ActorContext(
    actor_id=TenantId(uuid.UUID("00000000-0000-0000-0000-000000000030")),
    tenant_id=TENANT_A_ID,
    role_name=RoleName.ADMIN,
    permissions=permissions_for_role(RoleName.ADMIN),
    authenticated_at=datetime.now(UTC),
)


# =============================================================================
# Cross-Tenant Isolation Tests
# =============================================================================


@need_postgres
class TestCrossTenantIsolation:
    """Core RLS isolation: Tenant A sees A, Tenant A does not see B."""

    async def test_tenant_a_sees_a(self, app_session: AsyncSession) -> None:
        """Tenant A with RLS context can see their own tenant."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)
        result = await app_session.execute(
            text("SELECT name FROM tenants WHERE tenant_id = :tid"),
            {"tid": TENANT_A_ID_STR},
        )
        row = result.one_or_none()
        assert row is not None, "RLS should allow Tenant A to see their own tenant"
        assert row[0] == "Tenant A"

    async def test_tenant_a_does_not_see_b(self, app_session: AsyncSession) -> None:
        """Tenant A with RLS context cannot see Tenant B."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)
        result = await app_session.execute(
            text("SELECT name FROM tenants WHERE tenant_id = :tid"),
            {"tid": TENANT_B_ID_STR},
        )
        row = result.one_or_none()
        assert row is None, "RLS should block Tenant A from seeing Tenant B"

    async def test_tenant_a_cannot_list_b(self, app_session: AsyncSession) -> None:
        """Tenant A listing tenants only sees their own row."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)
        result = await app_session.execute(text("SELECT tenant_id FROM tenants"))
        rows = [row[0] for row in result]
        assert uuid.UUID(TENANT_A_ID_STR) in rows
        assert uuid.UUID(TENANT_B_ID_STR) not in rows

    async def test_tenant_b_sees_b(self, app_session: AsyncSession) -> None:
        """Tenant B with RLS context can see their own tenant."""
        await set_rls_on_session(app_session, TENANT_B_ID_STR)
        result = await app_session.execute(
            text("SELECT name FROM tenants WHERE tenant_id = :tid"),
            {"tid": TENANT_B_ID_STR},
        )
        row = result.one_or_none()
        assert row is not None
        assert row[0] == "Tenant B"

    async def test_tenant_b_does_not_see_a(self, app_session: AsyncSession) -> None:
        """Tenant B with RLS context cannot see Tenant A."""
        await set_rls_on_session(app_session, TENANT_B_ID_STR)
        result = await app_session.execute(
            text("SELECT name FROM tenants WHERE tenant_id = :tid"),
            {"tid": TENANT_A_ID_STR},
        )
        row = result.one_or_none()
        assert row is None


# =============================================================================
# Missing Context Tests (Fail-Closed)
# =============================================================================


@need_postgres
class TestMissingContextFailClosed:
    """Missing tenant context must fail closed — no rows returned."""

    async def test_missing_context_select_returns_nothing(self, app_session: AsyncSession) -> None:
        """Without RLS context, SELECT on tenant-scoped tables returns nothing."""
        result = await app_session.execute(text("SELECT COUNT(*) FROM tenants"))
        count = result.scalar_one()
        assert count == 0, "Missing RLS context should return 0 rows"

    async def test_missing_context_on_venues(self, app_session: AsyncSession) -> None:
        """Without RLS context, venues table returns nothing."""
        result = await app_session.execute(text("SELECT COUNT(*) FROM venues"))
        count = result.scalar_one()
        assert count == 0

    async def test_missing_context_on_memberships(self, app_session: AsyncSession) -> None:
        """Without RLS context, memberships table returns nothing."""
        result = await app_session.execute(text("SELECT COUNT(*) FROM memberships"))
        count = result.scalar_one()
        assert count == 0

    async def test_missing_context_on_membership_venues(self, app_session: AsyncSession) -> None:
        """Without RLS context, membership_venues returns nothing."""
        result = await app_session.execute(text("SELECT COUNT(*) FROM membership_venues"))
        count = result.scalar_one()
        assert count == 0


# =============================================================================
# Cross-Tenant INSERT / UPDATE / DELETE Tests
# =============================================================================


@need_postgres
class TestCrossTenantWriteIsolation:
    """RLS prevents cross-tenant writes."""

    @pytest_asyncio.fixture(autouse=True)
    async def _admin_session(self):
        """Admin session for verification — bypasses RLS.

        Creates its own engine to avoid event-loop conflicts with
        the module-scoped admin_engine fixture.
        """
        e = create_async_engine(_ADMIN_DATABASE_URL, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(e, expire_on_commit=False)
        async with factory() as s:
            self._admin = s
            yield
        await e.dispose()

    async def test_cross_tenant_insert_rejected(self, app_session: AsyncSession) -> None:
        """Inserting a row for another tenant is blocked by RLS."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)
        new_venue_id = uuid.uuid4()

        # RLS WITH CHECK raises InsufficientPrivilegeError
        with pytest.raises(Exception, match="row-level security"):
            await app_session.execute(
                text(
                    "INSERT INTO venues (venue_id, tenant_id, name, status, created_at) "
                    "VALUES (:vid, :tid, :name, 'active', NOW())"
                ),
                {
                    "vid": new_venue_id,
                    "tid": uuid.UUID(TENANT_B_ID_STR),  # DIFFERENT tenant
                    "name": "Cross-Tenant Venue",
                },
            )

        # Rollback the failed transaction
        await app_session.rollback()

        # Verify the row was NOT actually inserted via admin
        result = await self._admin.execute(
            text("SELECT COUNT(*) FROM venues WHERE venue_id = :vid"),
            {"vid": new_venue_id},
        )
        count = result.scalar_one()
        assert count == 0, "RLS should block cross-tenant INSERT"

    async def test_cross_tenant_update_rejected(self, app_session: AsyncSession) -> None:
        """Updating a row from another tenant is blocked by RLS."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)

        # Try to update Venue B-1 (belongs to Tenant B) — should affect 0 rows
        result = await app_session.execute(
            text("UPDATE venues SET name = 'Hacked!' WHERE venue_id = :vid"),
            {"vid": uuid.UUID("00000000-0000-0000-0000-000000000022")},  # Venue B-1
        )
        await app_session.commit()

        assert result.rowcount == 0, "RLS should block cross-tenant UPDATE"

        # Verify no change via admin
        result = await self._admin.execute(
            text("SELECT name FROM venues WHERE venue_id = :vid"),
            {"vid": uuid.UUID("00000000-0000-0000-0000-000000000022")},
        )
        name = result.scalar_one()
        assert name == "Venue B-1", "RLS should prevent cross-tenant UPDATE"

    async def test_cross_tenant_delete_rejected(self, app_session: AsyncSession) -> None:
        """Deleting a row from another tenant is blocked by RLS."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)

        # Try to delete Venue B-1 — should affect 0 rows
        result = await app_session.execute(
            text("DELETE FROM venues WHERE venue_id = :vid"),
            {"vid": uuid.UUID("00000000-0000-0000-0000-000000000022")},  # Venue B-1
        )
        await app_session.commit()

        assert result.rowcount == 0, "RLS should block cross-tenant DELETE"

        # Verify not actually deleted via admin
        result = await self._admin.execute(
            text("SELECT COUNT(*) FROM venues WHERE venue_id = :vid"),
            {"vid": uuid.UUID("00000000-0000-0000-0000-000000000022")},
        )
        count = result.scalar_one()
        assert count == 1, "RLS should prevent cross-tenant DELETE"

    async def test_same_tenant_insert_succeeds(self, app_session: AsyncSession) -> None:
        """Inserting a row for the same tenant succeeds."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)
        new_venue_id = uuid.uuid4()
        await app_session.execute(
            text(
                "INSERT INTO venues (venue_id, tenant_id, name, status, created_at) "
                "VALUES (:vid, :tid, :name, 'active', NOW())"
            ),
            {
                "vid": new_venue_id,
                "tid": uuid.UUID(TENANT_A_ID_STR),
                "name": "New Venue A-3",
            },
        )
        await app_session.commit()

        # Verify via admin
        result = await self._admin.execute(
            text("SELECT name FROM venues WHERE venue_id = :vid"),
            {"vid": new_venue_id},
        )
        name = result.scalar_one()
        assert name == "New Venue A-3"


# =============================================================================
# Repository + RLS Defense-in-Depth
# =============================================================================


@need_postgres
class TestRepositoryWithRLS:
    """Repository scoping + RLS provides defense in depth."""

    async def test_repository_plus_rls_double_protection(self, app_session: AsyncSession) -> None:
        """Both repository and RLS must agree for access."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)

        repo = VenueRepository(app_session)

        # Repository scopes by tenant_id AND RLS enforces the same
        venue = await repo.get_for_actor(A_TENANT_A, VENUE_A1_ID)
        assert venue is not None

        # Repository returns None for cross-tenant, RLS would also block
        cross = await repo.get_for_actor(A_TENANT_A, VENUE_B1_ID)
        assert cross is None

    async def test_repository_bug_still_protected_by_rls(self, app_session: AsyncSession) -> None:
        """If repository accidentally omits tenant filter, RLS still protects."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)

        # Simulate a repository bug: raw query without tenant filter
        result = await app_session.execute(
            text("SELECT name FROM venues WHERE venue_id = :vid"),
            {"vid": uuid.UUID("00000000-0000-0000-0000-000000000022")},  # Tenant B's venue
        )
        row = result.one_or_none()
        assert row is None, "RLS should protect even when app code omits tenant filter"


# =============================================================================
# Connection Pool Safety
# =============================================================================


@need_postgres
class TestConnectionPoolSafety:
    """Tenant context must not leak across connections."""

    async def test_context_cleared_after_commit(self) -> None:
        """SET LOCAL is transaction-scoped — cleared after commit."""
        e = create_async_engine(_APP_DATABASE_URL, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(e, expire_on_commit=False)

        # First transaction: set context, query, commit
        async with factory() as session1:
            await set_rls_on_session(session1, TENANT_A_ID_STR)
            result = await session1.execute(
                text("SELECT name FROM tenants WHERE tenant_id = :tid"),
                {"tid": TENANT_A_ID_STR},
            )
            name = result.scalar_one()
            assert name == "Tenant A"
            await session1.commit()

        # Second transaction: check context is gone
        async with factory() as session2:
            # Without re-setting context, RLS should fail-closed
            result = await session2.execute(text("SELECT COUNT(*) FROM tenants"))
            count = result.scalar_one()
            assert count == 0, "RLS context should not leak across transactions"
        await e.dispose()

    async def test_context_no_leak_between_sessions(self) -> None:
        """Context set in one session doesn't affect another."""
        e = create_async_engine(_APP_DATABASE_URL, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(e, expire_on_commit=False)

        # Set context on session1
        async with factory() as session1:
            await set_rls_on_session(session1, TENANT_A_ID_STR)

            # Verify session1 works
            result = await session1.execute(
                text("SELECT name FROM tenants WHERE tenant_id = :tid"),
                {"tid": TENANT_A_ID_STR},
            )
            row = result.one_or_none()
            assert row is not None

        # Different session2 — no context set
        async with factory() as session2:
            # Even with a direct ID query, RLS blocks it
            result = await session2.execute(
                text("SELECT name FROM tenants WHERE tenant_id = :tid"),
                {"tid": TENANT_A_ID_STR},
            )
            row = result.one_or_none()
            assert row is None, "RLS context should not leak between sessions"
        await e.dispose()

    async def test_clear_then_use_fails_closed(self, app_session: AsyncSession) -> None:
        """After manually clearing RLS context, queries fail closed."""
        await set_rls_on_session(app_session, TENANT_A_ID_STR)

        # Verify context works initially
        result = await app_session.execute(
            text("SELECT COUNT(*) FROM tenants"),
        )
        count = result.scalar_one()
        assert count > 0

        # Clear context
        await clear_rls_on_session(app_session)

        # Now queries should return nothing
        result = await app_session.execute(text("SELECT COUNT(*) FROM tenants"))
        count = result.scalar_one()
        assert count == 0, "After clearing RLS context, queries should fail closed"
