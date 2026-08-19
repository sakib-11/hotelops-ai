"""Enable PostgreSQL Row-Level Security for the video domain (Task 6.4).

Every video table carries a direct, FK-enforced tenant_id (migration 005),
so the RLS policy is the same simple fail-closed shape used for tenants in
migration 002:

    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''),
                         '00000000-0000-0000-0000-000000000000')::uuid

Tables: cameras, video_streams, video_assets, video_sessions.

Revision ID: 006_video_rls
Revises: 005_video_domain_schema
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_video_rls"
down_revision: str | None = "005_video_domain_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON cameras TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON video_streams TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON video_assets TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON video_sessions TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON cameras FROM hotelops_app;",
    "REVOKE ALL ON video_streams FROM hotelops_app;",
    "REVOKE ALL ON video_assets FROM hotelops_app;",
    "REVOKE ALL ON video_sessions FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)


def _table_statements(table: str) -> tuple[str, str, str, str]:
    """(enable, force, create_policy, drop_policy) — each a single statement.

    asyncpg rejects multi-statement prepared SQL, so every statement is
    executed separately (see migration 002).
    """
    enable = f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"
    force = f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"
    policy = (
        f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
        f"USING (tenant_id = {_CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
    )
    drop = f"DROP POLICY IF EXISTS {table}_all ON {table};"
    return enable, force, policy, drop


def upgrade() -> None:
    """Grant app-role access and enable RLS on the video tables."""
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    for table in ("cameras", "video_streams", "video_assets", "video_sessions"):
        enable, force, policy, _drop = _table_statements(table)
        op.execute(enable)
        op.execute(force)
        op.execute(policy)


def downgrade() -> None:
    """Drop video RLS policies, disable RLS, and revoke app-role grants."""
    for table in ("video_sessions", "video_assets", "video_streams", "cameras"):
        _enable, _force, _policy, drop = _table_statements(table)
        op.execute(drop)
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)
