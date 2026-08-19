"""Create the operational configuration schema (Task 6.5).

Implements typed configuration persistence for CCTV/video analysis —
explicitly NOT a generic key-value store. Only configuration required
by the current architecture (governance doc Section 3.3) is defined:

    camera_configs   — per-camera analysis configuration: analysis
                       enabled flag, frame rate, resolution, detection
                       sensitivity, versioned + effective-state
    analysis_configs — per-venue analysis profile with typed thresholds
                       (occupancy, dwell, queue length, wait time) for
                       the analytics named in Production Scope

Design decisions (each maps to a governance policy):

  - Typed columns, not key-value: frame_rate, sensitivity, thresholds,
    booleans and enums are real columns with CHECK constraints.
  - JSONB (`parameters`) is reserved for genuinely variable data only
    (adapter-specific camera tuning keys, zone geometry) — Section 7.
  - Tenant ownership is DIRECT and DB-enforced: every table carries
    tenant_id NOT NULL plus composite FKs (camera/venue_id, tenant_id)
    — the pattern established in migrations 003/005. Cross-tenant
    camera/venue references are rejected by composite FKs.
  - Version/effective-state semantics: a `status` enum
    (draft/active/archived) marks the currently-effective row and a
    `version` integer keeps relational change history (Section 3.3:
    "Config change history is relational", never a hypertable).
  - Unique active configuration rule: a partial unique index
    (scope columns) WHERE status = 'active' guarantees at most one
    active config per camera (camera_configs) and per (venue, name)
    (analysis_configs).
  - created_at + updated_at (timestamptz, UTC server default).
  - RLS + grants for both tables ship in the SAME migration (Section
    10.4 rule 5); policies are the fail-closed tenant shape.

ENUM lifecycle note: `config_status` is shared by both tables, so it is
created explicitly once below and the columns use create_type=False
(a duplicate CREATE TYPE would fail).

Revision ID: 007_operational_config_schema
Revises: 006_video_rls
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

# revision identifiers, used by Alembic.
revision: str = "007_operational_config_schema"
down_revision: str | None = "006_video_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON camera_configs TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON analysis_configs TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON camera_configs FROM hotelops_app;",
    "REVOKE ALL ON analysis_configs FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)

# Shared enum: config_status appears on BOTH config tables.
_CONFIG_STATUS = "CREATE TYPE config_status AS ENUM ('draft', 'active', 'archived')"


def upgrade() -> None:
    """Create the typed configuration tables, then enable RLS on them."""

    # Shared enum (both tables use it) — created explicitly once.
    op.execute(_CONFIG_STATUS)

    # --- CAMERA CONFIGS (per-camera analysis configuration) ---
    op.create_table(
        "camera_configs",
        sa.Column("config_id", sa.UUID(), nullable=False),
        sa.Column("camera_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            ENUM("draft", "active", "archived", name="config_status", create_type=False),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("analysis_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("frame_rate", sa.Numeric(7, 3), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("detection_sensitivity", sa.Numeric(4, 3), nullable=True),
        # Adapter-specific flexible parameters only (JSONB policy, Section 7).
        sa.Column("parameters", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("config_id"),
        sa.UniqueConstraint("config_id", "tenant_id", name="uq_camera_configs_config_tenant"),
        # Versioned change history per camera.
        sa.UniqueConstraint("camera_id", "version", name="uq_camera_configs_version"),
        # Unique active configuration rule: at most one active per camera.
        sa.CheckConstraint("version >= 1", name="ck_camera_configs_version_positive"),
        sa.CheckConstraint(
            "frame_rate IS NULL OR frame_rate > 0",
            name="ck_camera_configs_frame_rate_positive",
        ),
        sa.CheckConstraint("width IS NULL OR width > 0", name="ck_camera_configs_width_positive"),
        sa.CheckConstraint(
            "height IS NULL OR height > 0",
            name="ck_camera_configs_height_positive",
        ),
        sa.CheckConstraint(
            "detection_sensitivity IS NULL OR "
            "(detection_sensitivity >= 0 AND detection_sensitivity <= 1)",
            name="ck_camera_configs_sensitivity_range",
        ),
        # NOTE: venue_id is denormalized alongside camera_id, mirroring the
        # video domain pattern (e.g. video_streams). The composite FKs enforce
        # that both the camera and the venue belong to the config's tenant;
        # matching venue_id to the camera's own venue is intentionally not
        # DB-enforced (a CHECK cannot subquery; a trigger would be required).
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_camera_configs_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_camera_configs_venue_tenant",
        ),
    )
    op.create_index("ix_camera_configs_tenant_id", "camera_configs", ["tenant_id"])
    op.create_index("ix_camera_configs_camera_id", "camera_configs", ["camera_id"])
    op.create_index("ix_camera_configs_venue_id", "camera_configs", ["venue_id"])
    op.create_index(
        "uq_camera_configs_active",
        "camera_configs",
        ["camera_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- ANALYSIS CONFIGS (per-venue typed analysis profile/thresholds) ---
    op.create_table(
        "analysis_configs",
        sa.Column("config_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "status",
            ENUM("draft", "active", "archived", name="config_status", create_type=False),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confidence_threshold", sa.Numeric(4, 3), nullable=True),
        sa.Column("frame_rate", sa.Numeric(7, 3), nullable=True),
        sa.Column("occupancy_threshold", sa.Integer(), nullable=True),
        sa.Column("dwell_time_seconds", sa.Integer(), nullable=True),
        sa.Column("queue_length_threshold", sa.Integer(), nullable=True),
        sa.Column("wait_time_seconds", sa.Integer(), nullable=True),
        # Genuinely variable geometry/zone definitions only (JSONB policy).
        sa.Column("parameters", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("config_id"),
        sa.UniqueConstraint("config_id", "tenant_id", name="uq_analysis_configs_config_tenant"),
        # Versioned change history per (venue, name) profile.
        sa.UniqueConstraint(
            "venue_id",
            "name",
            "version",
            name="uq_analysis_configs_version",
        ),
        sa.CheckConstraint("version >= 1", name="ck_analysis_configs_version_positive"),
        sa.CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_analysis_configs_name_not_empty",
        ),
        sa.CheckConstraint(
            "confidence_threshold IS NULL OR "
            "(confidence_threshold >= 0 AND confidence_threshold <= 1)",
            name="ck_analysis_configs_confidence_range",
        ),
        sa.CheckConstraint(
            "frame_rate IS NULL OR frame_rate > 0",
            name="ck_analysis_configs_frame_rate_positive",
        ),
        sa.CheckConstraint(
            "occupancy_threshold IS NULL OR "
            "(occupancy_threshold >= 0 AND occupancy_threshold <= 100)",
            name="ck_analysis_configs_occupancy_range",
        ),
        sa.CheckConstraint(
            "dwell_time_seconds IS NULL OR dwell_time_seconds >= 0",
            name="ck_analysis_configs_dwell_non_negative",
        ),
        sa.CheckConstraint(
            "queue_length_threshold IS NULL OR queue_length_threshold >= 0",
            name="ck_analysis_configs_queue_non_negative",
        ),
        sa.CheckConstraint(
            "wait_time_seconds IS NULL OR wait_time_seconds >= 0",
            name="ck_analysis_configs_wait_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_analysis_configs_venue_tenant",
        ),
    )
    op.create_index("ix_analysis_configs_tenant_id", "analysis_configs", ["tenant_id"])
    op.create_index("ix_analysis_configs_venue_id", "analysis_configs", ["venue_id"])
    op.create_index(
        "uq_analysis_configs_active",
        "analysis_configs",
        ["venue_id", "name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    for table in ("camera_configs", "analysis_configs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
            f"USING (tenant_id = {_CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
        )


def downgrade() -> None:
    """Drop the config RLS policies, tables, and the shared enum type."""
    for table in ("analysis_configs", "camera_configs"):
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.drop_table("analysis_configs")
    op.drop_table("camera_configs")
    op.execute("DROP TYPE config_status")
