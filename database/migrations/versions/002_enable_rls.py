"""Enable PostgreSQL Row-Level Security for tenant/venue isolation.

Creates the application runtime role, enables RLS on all tenant-scoped
tables, and defines policies using `current_setting('app.tenant_id')`.

Architecture:
    Application Authorization
            ↓
    Repository Scope (WHERE tenant_id = :actor_tenant)
            ↓
    PostgreSQL RLS (policy: tenant_id = current_setting('app.tenant_id')::uuid)

The application runtime role (hotelops_app) has no special privileges
and cannot bypass RLS. Migrations run as a separate role (e.g. the
alembic configured user) that can bypass RLS.

Fail-closed: when app.tenant_id is not set, policies use 1=0.
Connection pool safety: SET LOCAL is transaction-scoped.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_enable_rls"
down_revision: str | None = "001_create_identity_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# =============================================================================
# Application runtime role — has no special bypass privileges
# =============================================================================

_SQL_CREATE_APP_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'hotelops_app') THEN
        CREATE ROLE hotelops_app WITH LOGIN NOBYPASSRLS PASSWORD 'CHANGE_ME';
    END IF;
END
$$;
"""

_SQL_GRANT_USAGE = """
GRANT USAGE ON SCHEMA public TO hotelops_app;
"""

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON venues TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON users TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON roles TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON permissions TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON role_permissions TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON membership_venues TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON tenants FROM hotelops_app;",
    "REVOKE ALL ON venues FROM hotelops_app;",
    "REVOKE ALL ON users FROM hotelops_app;",
    "REVOKE ALL ON roles FROM hotelops_app;",
    "REVOKE ALL ON permissions FROM hotelops_app;",
    "REVOKE ALL ON role_permissions FROM hotelops_app;",
    "REVOKE ALL ON memberships FROM hotelops_app;",
    "REVOKE ALL ON membership_venues FROM hotelops_app;",
    "REVOKE USAGE ON SCHEMA public FROM hotelops_app;",
]

# =============================================================================
# RLS policies — all use current_setting with fail-closed (1=0 when NULL)
# =============================================================================

# --- TENANTS ---
_SQL_ENABLE_RLS_TENANTS = "ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;"
_SQL_RLS_TENANTS_SELECT = """
CREATE POLICY tenants_select ON tenants FOR SELECT TO hotelops_app
USING (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
);
"""
_SQL_RLS_TENANTS_INSERT = """
CREATE POLICY tenants_insert ON tenants FOR INSERT TO hotelops_app
WITH CHECK (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
);
"""
_SQL_RLS_TENANTS_UPDATE = """
CREATE POLICY tenants_update ON tenants FOR UPDATE TO hotelops_app
USING (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
)
WITH CHECK (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
);
"""
_SQL_RLS_TENANTS_DELETE = """
CREATE POLICY tenants_delete ON tenants FOR DELETE TO hotelops_app
USING (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
);
"""

# --- VENUES ---
_SQL_ENABLE_RLS_VENUES = "ALTER TABLE venues ENABLE ROW LEVEL SECURITY;"
_SQL_RLS_VENUES = """
CREATE POLICY venues_all ON venues FOR ALL TO hotelops_app
USING (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
)
WITH CHECK (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
);
"""

# --- MEMBERSHIPS ---
_SQL_ENABLE_RLS_MEMBERSHIPS = "ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;"
_SQL_RLS_MEMBERSHIPS = """
CREATE POLICY memberships_all ON memberships FOR ALL TO hotelops_app
USING (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
)
WITH CHECK (
    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
);
"""

# --- MEMBERSHIP-VENUES (indirectly scoped via membership) ---
_SQL_ENABLE_RLS_MEMBERSHIP_VENUES = "ALTER TABLE membership_venues ENABLE ROW LEVEL SECURITY;"
_SQL_RLS_MEMBERSHIP_VENUES = """
CREATE POLICY membership_venues_all ON membership_venues FOR ALL TO hotelops_app
USING (
    membership_id IN (
        SELECT membership_id FROM memberships
        WHERE tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
    )
)
WITH CHECK (
    membership_id IN (
        SELECT membership_id FROM memberships
        WHERE tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '00000000-0000-0000-0000-000000000000')::uuid
    )
);
"""

# --- USERS, ROLES, PERMISSIONS (not tenant-scoped — no RLS) ---
# These tables contain global data. Tenant isolation for users happens
# through the membership relationship, not RLS.


def upgrade() -> None:
    """Enable RLS on tenant-scoped tables."""

    # Step 1: Create application runtime role
    op.execute(_SQL_CREATE_APP_ROLE)
    op.execute(_SQL_GRANT_USAGE)
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    # Step 2: Enable RLS and create policies

    # Tenants
    op.execute(_SQL_ENABLE_RLS_TENANTS)
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY;")
    op.execute(_SQL_RLS_TENANTS_SELECT)
    op.execute(_SQL_RLS_TENANTS_INSERT)
    op.execute(_SQL_RLS_TENANTS_UPDATE)
    op.execute(_SQL_RLS_TENANTS_DELETE)

    # Venues
    op.execute(_SQL_ENABLE_RLS_VENUES)
    op.execute("ALTER TABLE venues FORCE ROW LEVEL SECURITY;")
    op.execute(_SQL_RLS_VENUES)

    # Memberships
    op.execute(_SQL_ENABLE_RLS_MEMBERSHIPS)
    op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY;")
    op.execute(_SQL_RLS_MEMBERSHIPS)

    # Membership-Venues
    op.execute(_SQL_ENABLE_RLS_MEMBERSHIP_VENUES)
    op.execute("ALTER TABLE membership_venues FORCE ROW LEVEL SECURITY;")
    op.execute(_SQL_RLS_MEMBERSHIP_VENUES)


def downgrade() -> None:
    """Remove RLS and clean up."""

    # Drop policies (order: children before parents)
    op.execute("DROP POLICY IF EXISTS membership_venues_all ON membership_venues;")
    op.execute("DROP POLICY IF EXISTS memberships_all ON memberships;")
    op.execute("DROP POLICY IF EXISTS venues_all ON venues;")
    op.execute("DROP POLICY IF EXISTS tenants_select ON tenants;")
    op.execute("DROP POLICY IF EXISTS tenants_insert ON tenants;")
    op.execute("DROP POLICY IF EXISTS tenants_update ON tenants;")
    op.execute("DROP POLICY IF EXISTS tenants_delete ON tenants;")

    # Disable RLS
    op.execute("ALTER TABLE membership_venues DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE membership_venues NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE memberships DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE memberships NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE venues DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE venues NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY;")

    # Drop app role and revoke privileges
    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)
    op.execute("DROP ROLE IF EXISTS hotelops_app;")
