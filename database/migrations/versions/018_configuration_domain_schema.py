"""Create the configuration domain schema (Task 10).

Versioned Camera/Venue physical model consumed by CV. PostgreSQL is the
source of truth for configuration identity, ownership, lifecycle,
validation results, and session pinning; geometry is stored as canonical
JSONB and queried through PostGIS expression indexes (ST_GeomFromGeoJSON)
so PostGIS remains the authoritative spatial engine.

Tables:
  configurations           — logical aggregate (one per tenant+venue)
  configuration_versions   — immutable snapshots, DRAFT -> VALIDATING ->
                             VALIDATED -> PUBLISHED
  config_camera_profiles, config_zones, config_tables, config_entrances,
  config_queue_areas, config_service_areas, config_privacy_rois,
  config_exclusion_rois    — version-owned entities with composite
                             tenant FKs and same-version ownership

Also adds video_sessions.configuration_version_id (session pinning).

Governance:
  - PostGIS is enabled ONLY here, guarded with CREATE EXTENSION IF NOT
    EXISTS postgis (no-op when already installed).
  - Direct tenant ownership (tenant_id NOT NULL) + composite FKs —
    the established 003/005/007/017 pattern. Cross-tenant references
    are rejected by composite FKs.
  - RLS + grants ship in the SAME migration (Section 10.4 rule 5).
  - Lifecycle/status enums are CHECK-constrained so the state machine
    cannot be bypassed by direct SQL.

Revision ID: 018_configuration_domain_schema
Revises: 017_media_metadata_schema
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

# revision identifiers, used by Alembic.
revision: str = "018_configuration_domain_schema"
down_revision: str | None = "017_media_metadata_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)

# Tables with RLS, in dependency order (child -> parent on downgrade).
_RLS_TABLES = (
    "config_exclusion_rois",
    "config_privacy_rois",
    "config_service_areas",
    "config_queue_areas",
    "config_entrances",
    "config_tables",
    "config_zones",
    "config_camera_profiles",
    "configuration_versions",
    "configurations",
)

_CONFIG_STATUS = (
    "CREATE TYPE config_version_status AS ENUM ('draft', 'validating', 'validated', 'published')"
)


def _grant(table: str) -> str:
    return f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO hotelops_app;"


def _revoke(table: str) -> str:
    return f"REVOKE ALL ON {table} FROM hotelops_app;"


# =============================================================================
# Entity table shape (shared columns for the 8 version-owned tables)
# =============================================================================

_ENTITY_COMMON_COLUMNS = [
    sa.Column("entity_id", sa.UUID(), primary_key=True),
    sa.Column("configuration_version_id", sa.UUID(), nullable=False),
    sa.Column("venue_id", sa.UUID(), nullable=False),
    sa.Column("tenant_id", sa.UUID(), nullable=False),
    sa.Column("profile_id", sa.String(128), nullable=False),
    sa.Column("geometry", JSONB(), nullable=False),
    sa.Column("coordinate_space", sa.String(24), nullable=False),
    sa.Column("geometry_type", sa.String(24), nullable=False),
    sa.Column("metadata", JSONB(), nullable=True),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
]

# Camera physical placement (venue-local POINT) is OPTIONAL per the
# contract — geometry/space/type columns are nullable for cameras.
_CAMERA_GEOMETRY_COLUMNS = [
    sa.Column("geometry", JSONB(), nullable=True),
    sa.Column("coordinate_space", sa.String(24), nullable=True),
    sa.Column("geometry_type", sa.String(24), nullable=True),
]


def _entity_fks(table: str) -> list[sa.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(
            ["configuration_version_id", "tenant_id"],
            ["configuration_versions.configuration_version_id", "configuration_versions.tenant_id"],
            ondelete="CASCADE",
            name=f"fk_{table}_version_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name=f"fk_{table}_venue_tenant",
        ),
    ]


def _entity_checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(
            "length(btrim(profile_id)) > 0", name=f"ck_{table}_profile_id_not_empty"
        ),
        sa.CheckConstraint(
            "coordinate_space IN ('image_normalized', 'venue_local')",
            name=f"ck_{table}_coordinate_space",
        ),
        sa.CheckConstraint(
            "geometry_type IN ('point', 'linestring', 'polygon')",
            name=f"ck_{table}_geometry_type",
        ),
    ]


def _entity_indexes(table: str) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    op.create_index(f"ix_{table}_venue_id", table, ["venue_id"])
    op.create_index(f"ix_{table}_version_id", table, ["configuration_version_id"])
    # PostGIS GIST expression index on the canonical JSONB geometry —
    # lets the authoritative spatial engine answer overlap/containment
    # queries without a duplicate geometry column.
    op.execute(
        f"CREATE INDEX ix_{table}_geom_gist ON {table} USING GIST (ST_GeomFromGeoJSON(geometry));"
    )
    op.create_index(
        f"uq_{table}_version_profile",
        table,
        ["configuration_version_id", "profile_id"],
        unique=True,
    )


def upgrade() -> None:
    # --- PostGIS (guarded: no-op when already installed) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")

    # --- Shared enum ---
    op.execute(_CONFIG_STATUS)

    # --- Configurations (aggregate) ---
    op.create_table(
        "configurations",
        sa.Column("configuration_id", sa.UUID(), primary_key=True),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("current_published_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "configuration_id", "tenant_id", name="uq_configurations_config_tenant"
        ),
        sa.UniqueConstraint("venue_id", "tenant_id", name="uq_configurations_venue_tenant"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_configurations_name_not_empty"),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_configurations_venue_tenant",
        ),
    )
    op.create_index("ix_configurations_tenant_id", "configurations", ["tenant_id"])
    op.create_index("ix_configurations_venue_id", "configurations", ["venue_id"])

    # --- Configuration versions ---
    op.create_table(
        "configuration_versions",
        sa.Column("configuration_version_id", sa.UUID(), primary_key=True),
        sa.Column("configuration_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            ENUM(
                "draft",
                "validating",
                "validated",
                "published",
                name="config_version_status",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("validation_result", JSONB(), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_by", sa.String(255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(255), nullable=True),
        sa.Column("replaced_version_id", sa.UUID(), nullable=True),
        sa.Column("schema_version", sa.String(32), nullable=False, server_default="1.0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "configuration_version_id",
            "tenant_id",
            name="uq_config_versions_version_tenant",
        ),
        sa.UniqueConstraint(
            "configuration_id", "version", name="uq_config_versions_config_version"
        ),
        sa.CheckConstraint("version >= 1", name="ck_config_versions_version_positive"),
        sa.CheckConstraint(
            "status IN ('draft', 'validating', 'validated', 'published')",
            name="ck_config_versions_status",
        ),
        sa.CheckConstraint(
            "status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL)",
            name="ck_config_versions_published_complete",
        ),
        sa.CheckConstraint(
            "status <> 'validated' OR validated_at IS NOT NULL",
            name="ck_config_versions_validated_complete",
        ),
        sa.CheckConstraint(
            "status <> 'published' OR replaced_version_id IS DISTINCT FROM configuration_version_id",
            name="ck_config_versions_no_self_replace",
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id", "tenant_id"],
            ["configurations.configuration_id", "configurations.tenant_id"],
            ondelete="CASCADE",
            name="fk_config_versions_configuration_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_config_versions_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_version_id", "tenant_id"],
            ["configuration_versions.configuration_version_id", "configuration_versions.tenant_id"],
            ondelete="SET NULL",
            name="fk_config_versions_replaced",
        ),
    )
    op.create_index("ix_config_versions_tenant_id", "configuration_versions", ["tenant_id"])
    op.create_index("ix_config_versions_venue_id", "configuration_versions", ["venue_id"])
    op.create_index(
        "ix_config_versions_configuration_id", "configuration_versions", ["configuration_id"]
    )
    op.create_index("ix_config_versions_status", "configuration_versions", ["status"])

    # Circular FK: configurations.current_published_version_id ->
    # configuration_versions (added after both tables exist).
    op.execute(
        "ALTER TABLE configurations ADD CONSTRAINT fk_configurations_current_version "
        "FOREIGN KEY (current_published_version_id, tenant_id) "
        "REFERENCES configuration_versions (configuration_version_id, tenant_id) "
        "ON DELETE SET NULL;"
    )

    # --- Camera profiles (version-owned) ---
    op.create_table(
        "config_camera_profiles",
        *_ENTITY_COMMON_COLUMNS[:4],
        sa.Column("profile_id", sa.String(128), nullable=False),
        *_CAMERA_GEOMETRY_COLUMNS,
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("camera_id", sa.UUID(), nullable=False),
        sa.Column("camera_reference", sa.String(255), nullable=False),
        sa.Column("mount_type", sa.String(32), nullable=False, server_default="ceiling"),
        sa.Column("mount_height_meters", sa.Numeric(8, 3), nullable=True),
        sa.Column("tilt_degrees", sa.Numeric(6, 2), nullable=True),
        sa.Column("pan_degrees", sa.Numeric(7, 2), nullable=True),
        sa.Column("roll_degrees", sa.Numeric(7, 2), nullable=True),
        sa.Column("resolution_width", sa.Integer(), nullable=False),
        sa.Column("resolution_height", sa.Integer(), nullable=False),
        sa.Column("fps", sa.Numeric(7, 3), nullable=True),
        sa.Column("codec", sa.String(64), nullable=True),
        sa.Column("image_orientation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("analysis_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("detection_zones", JSONB(), nullable=True),
        sa.Column("privacy_rois", JSONB(), nullable=True),
        sa.Column("exclusion_rois", JSONB(), nullable=True),
        *_entity_fks("config_camera_profiles"),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_config_camera_profiles_camera_tenant",
        ),
        *_entity_checks("config_camera_profiles"),
        sa.CheckConstraint(
            "resolution_width > 0 AND resolution_height > 0",
            name="ck_config_camera_profiles_resolution",
        ),
        sa.CheckConstraint("fps IS NULL OR fps > 0", name="ck_config_camera_profiles_fps_positive"),
    )
    _entity_indexes("config_camera_profiles")

    # --- Zones ---
    op.create_table(
        "config_zones",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("zone_type", sa.String(32), nullable=False, server_default="custom"),
        sa.Column("labels", JSONB(), nullable=True),
        sa.Column("contained_tables", JSONB(), nullable=True),
        sa.Column("contained_entrances", JSONB(), nullable=True),
        sa.Column("contained_queue_areas", JSONB(), nullable=True),
        sa.Column("contained_service_areas", JSONB(), nullable=True),
        *_entity_fks("config_zones"),
        *_entity_checks("config_zones"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_config_zones_name_not_empty"),
    )
    _entity_indexes("config_zones")

    # --- Tables ---
    op.create_table(
        "config_tables",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("seat_count", sa.Integer(), nullable=True),
        sa.Column("table_shape", sa.String(64), nullable=True),
        *_entity_fks("config_tables"),
        *_entity_checks("config_tables"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_config_tables_name_not_empty"),
        sa.CheckConstraint(
            "seat_count IS NULL OR seat_count > 0", name="ck_config_tables_seat_count"
        ),
    )
    _entity_indexes("config_tables")

    # --- Entrances ---
    op.create_table(
        "config_entrances",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("direction", sa.String(24), nullable=False, server_default="bidirectional"),
        sa.Column("zone_profile_id", sa.String(128), nullable=True),
        sa.Column("camera_profiles", JSONB(), nullable=True),
        *_entity_fks("config_entrances"),
        *_entity_checks("config_entrances"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_config_entrances_name_not_empty"),
        sa.CheckConstraint(
            "direction IN ('entrance', 'exit', 'bidirectional')",
            name="ck_config_entrances_direction",
        ),
    )
    _entity_indexes("config_entrances")

    # --- Queue areas ---
    op.create_table(
        "config_queue_areas",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("queue_direction", JSONB(), nullable=True),
        sa.Column("max_queue_length", sa.Integer(), nullable=True),
        sa.Column("zone_profile_id", sa.String(128), nullable=True),
        sa.Column("camera_profiles", JSONB(), nullable=True),
        *_entity_fks("config_queue_areas"),
        *_entity_checks("config_queue_areas"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_config_queue_areas_name_not_empty"),
        sa.CheckConstraint(
            "max_queue_length IS NULL OR max_queue_length > 0",
            name="ck_config_queue_areas_max_length",
        ),
    )
    _entity_indexes("config_queue_areas")

    # --- Service areas ---
    op.create_table(
        "config_service_areas",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("service_type", sa.String(64), nullable=True),
        sa.Column("zone_profile_id", sa.String(128), nullable=True),
        sa.Column("camera_profiles", JSONB(), nullable=True),
        *_entity_fks("config_service_areas"),
        *_entity_checks("config_service_areas"),
        sa.CheckConstraint(
            "length(btrim(name)) > 0", name="ck_config_service_areas_name_not_empty"
        ),
    )
    _entity_indexes("config_service_areas")

    # --- Privacy ROIs ---
    op.create_table(
        "config_privacy_rois",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("privacy_action", sa.String(24), nullable=False, server_default="blur"),
        sa.Column("policy_reference", sa.String(255), nullable=True),
        sa.Column("camera_profiles", JSONB(), nullable=True),
        *_entity_fks("config_privacy_rois"),
        *_entity_checks("config_privacy_rois"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_config_privacy_rois_name_not_empty"),
        sa.CheckConstraint(
            "privacy_action IN ('blur', 'mask', 'exclude', 'redact')",
            name="ck_config_privacy_rois_action",
        ),
    )
    _entity_indexes("config_privacy_rois")

    # --- Exclusion ROIs ---
    op.create_table(
        "config_exclusion_rois",
        *_ENTITY_COMMON_COLUMNS,
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("excluded_tasks", JSONB(), nullable=True),
        sa.Column("exclusion_reason", sa.String(500), nullable=True),
        sa.Column("camera_profiles", JSONB(), nullable=True),
        *_entity_fks("config_exclusion_rois"),
        *_entity_checks("config_exclusion_rois"),
        sa.CheckConstraint(
            "length(btrim(name)) > 0", name="ck_config_exclusion_rois_name_not_empty"
        ),
    )
    _entity_indexes("config_exclusion_rois")

    # --- Session pinning (Task 10.13) ---
    op.add_column(
        "video_sessions",
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
    )
    op.execute(
        "ALTER TABLE video_sessions ADD CONSTRAINT fk_video_sessions_config_version_tenant "
        "FOREIGN KEY (configuration_version_id, tenant_id) "
        "REFERENCES configuration_versions (configuration_version_id, tenant_id) "
        "ON DELETE RESTRICT;"
    )
    op.create_index(
        "ix_video_sessions_config_version_id", "video_sessions", ["configuration_version_id"]
    )

    # --- RLS + grants (same migration, governance rule 5) ---
    for table in _RLS_TABLES:
        op.execute(_grant(table))
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
            f"USING (tenant_id = {_CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
        )


def downgrade() -> None:
    """Drop config RLS policies, tables, enum, and session FK (reversible)."""
    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(_revoke(table))

    op.drop_index("ix_video_sessions_config_version_id", table_name="video_sessions")
    op.execute(
        "ALTER TABLE video_sessions DROP CONSTRAINT fk_video_sessions_config_version_tenant;"
    )
    op.drop_column("video_sessions", "configuration_version_id")

    for table in reversed(_RLS_TABLES):
        if table in ("configurations", "configuration_versions"):
            continue
        op.drop_index(f"uq_{table}_version_profile", table_name=table)
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_geom_gist;")
        op.drop_index(f"ix_{table}_version_id", table_name=table)
        op.drop_index(f"ix_{table}_venue_id", table_name=table)
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_table(table)

    op.execute("ALTER TABLE configurations DROP CONSTRAINT fk_configurations_current_version;")
    op.drop_index("ix_config_versions_status", table_name="configuration_versions")
    op.drop_index("ix_config_versions_configuration_id", table_name="configuration_versions")
    op.drop_index("ix_config_versions_venue_id", table_name="configuration_versions")
    op.drop_index("ix_config_versions_tenant_id", table_name="configuration_versions")
    op.drop_table("configuration_versions")
    op.drop_index("ix_configurations_venue_id", table_name="configurations")
    op.drop_index("ix_configurations_tenant_id", table_name="configurations")
    op.drop_table("configurations")

    op.execute("DROP TYPE config_version_status")
