"""Create the foundational video domain schema (Task 6.4).

Implements the minimum relational persistence entities for the Task 4
video contracts (contracts/video/models.py):

    cameras        — a camera device that belongs to exactly one venue
    video_streams  — live ingestion stream metadata of a camera
    video_assets   — immutable reference to a source video (live or
                     recorded); recorded assets reference object storage
    video_sessions — processing session over a live (camera) or recorded
                     (asset) source at a venue

PostgreSQL stores metadata and authoritative operational state only.
Large video binaries live in object storage; PG holds the reference
(video_assets.storage_uri).

Tenancy: DIRECT tenant_id on every table, FK-enforced consistent with the
venue via the composite FK (venue_id, tenant_id) REFERENCES venues
(venue_id, tenant_id) — the pattern established in migration 003. Cross-
tenant camera/asset references are likewise prevented by composite FKs.

ENUM lifecycle note: `video_source_type` is shared by video_assets and
video_sessions, so it is created explicitly once below and the columns
use create_type=False (a duplicate CREATE TYPE would fail). The remaining
enums (camera_status, camera_protocol, stream_status, video_session_status)
are each used by exactly one table and are auto-created by op.create_table.

Revision ID: 005_video_domain_schema
Revises: 004_tenancy_check_constraints
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

# revision identifiers, used by Alembic.
revision: str = "005_video_domain_schema"
down_revision: str | None = "004_tenancy_check_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the video domain tables (metadata only — bytes stay in object storage)."""

    # Shared enum: source_type appears on video_assets AND video_sessions.
    op.execute("CREATE TYPE video_source_type AS ENUM ('live', 'recorded')")

    # --- CAMERAS ---
    op.create_table(
        "cameras",
        sa.Column("camera_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="camera_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "protocol",
            sa.Enum("rtsp", "onvif", name="camera_protocol"),
            nullable=False,
            server_default="rtsp",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("camera_id"),
        sa.UniqueConstraint("camera_id", "tenant_id", name="uq_cameras_camera_tenant"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_cameras_name_not_empty"),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_cameras_venue_tenant",
        ),
    )
    op.create_index("ix_cameras_tenant_id", "cameras", ["tenant_id"])
    op.create_index("ix_cameras_venue_id", "cameras", ["venue_id"])

    # --- VIDEO STREAMS (live ingestion stream of a camera) ---
    op.create_table(
        "video_streams",
        sa.Column("stream_id", sa.UUID(), nullable=False),
        sa.Column("camera_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="stream_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("source_url", sa.String(1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("stream_id"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_video_streams_name_not_empty"),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_streams_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_streams_camera_tenant",
        ),
    )
    op.create_index("ix_video_streams_tenant_id", "video_streams", ["tenant_id"])
    op.create_index("ix_video_streams_camera_id", "video_streams", ["camera_id"])
    op.create_index("ix_video_streams_venue_id", "video_streams", ["venue_id"])

    # --- VIDEO ASSETS (immutable reference to a source video) ---
    op.create_table(
        "video_assets",
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "source_type",
            ENUM(
                "live",
                "recorded",
                name="video_source_type",
                create_type=False,  # created explicitly above (shared type)
            ),
            nullable=False,
        ),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        sa.Column("evidence_ref", sa.UUID(), nullable=True),
        sa.Column("capture_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(12, 3), nullable=True),
        sa.Column("storage_uri", sa.String(1024), nullable=True),
        sa.Column("media_metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("asset_id", "tenant_id", name="uq_video_assets_asset_tenant"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_video_assets_name_not_empty"),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_video_assets_duration_non_negative",
        ),
        sa.CheckConstraint(
            "(source_type = 'live' AND camera_id IS NOT NULL AND storage_uri IS NULL) "
            "OR (source_type = 'recorded' AND storage_uri IS NOT NULL)",
            name="ck_video_assets_source_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_assets_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_assets_camera_tenant",
        ),
    )
    op.create_index("ix_video_assets_tenant_id", "video_assets", ["tenant_id"])
    op.create_index("ix_video_assets_venue_id", "video_assets", ["venue_id"])
    op.create_index("ix_video_assets_camera_id", "video_assets", ["camera_id"])

    # --- VIDEO SESSIONS (processing session over live or recorded source) ---
    op.create_table(
        "video_sessions",
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "source_type",
            ENUM(
                "live",
                "recorded",
                name="video_source_type",
                create_type=False,  # shared type — already created
            ),
            nullable=False,
        ),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        sa.Column("asset_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "ended", "failed", name="video_session_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.CheckConstraint(
            "(source_type = 'live' AND camera_id IS NOT NULL) "
            "OR (source_type = 'recorded' AND asset_id IS NOT NULL)",
            name="ck_video_sessions_source_consistent",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_video_sessions_ended_after_started",
        ),
        # A session that is no longer active (ended/failed) must carry an
        # end time; an active session must not (status <-> ended_at link).
        sa.CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) "
            "OR (status IN ('ended', 'failed') AND ended_at IS NOT NULL)",
            name="ck_video_sessions_status_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_sessions_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_sessions_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "tenant_id"],
            ["video_assets.asset_id", "video_assets.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_sessions_asset_tenant",
        ),
    )
    op.create_index("ix_video_sessions_tenant_id", "video_sessions", ["tenant_id"])
    op.create_index("ix_video_sessions_venue_id", "video_sessions", ["venue_id"])
    op.create_index("ix_video_sessions_camera_id", "video_sessions", ["camera_id"])
    op.create_index("ix_video_sessions_asset_id", "video_sessions", ["asset_id"])


def downgrade() -> None:
    """Drop the video domain schema in reverse dependency order.

    The four single-use enum types (camera_status, camera_protocol,
    stream_status, video_session_status) are created by op.create_table
    as a side effect; drop them explicitly here (IF EXISTS — they may
    have been dropped already) so a downgrade fully reverses the upgrade.
    """
    op.drop_table("video_sessions")
    op.drop_table("video_assets")
    op.drop_table("video_streams")
    op.drop_table("cameras")
    op.execute("DROP TYPE video_source_type")
    op.execute("DROP TYPE IF EXISTS camera_status")
    op.execute("DROP TYPE IF EXISTS camera_protocol")
    op.execute("DROP TYPE IF EXISTS stream_status")
    op.execute("DROP TYPE IF EXISTS video_session_status")
