"""Create media metadata schema and RLS (revision 017).

Establishes the authoritative PostgreSQL media metadata store for Task 9
(Object Storage & Media Lifecycle). Large binary media files reside strictly
in object storage; PostgreSQL maintains ownership, lifecycle state machine,
checksums, size, and provenance.

Tables:
  media_assets — centralized media metadata, lifecycle, and object storage pointers.

Governance:
  - Direct tenant ownership (tenant_id NOT NULL)
  - Composite FK to venues(venue_id, tenant_id)
  - Row Level Security (RLS) enabled and forced
  - Short-lived presigned URLs are NEVER persisted
  - Checksum validation constraint (64-char lowercase hex sha256)

Revision ID: 017_media_metadata_schema
Revises: 016_outbox_retry_idempotency
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

# revision identifiers, used by Alembic.
revision: str = "017_media_metadata_schema"
down_revision: str | None = "016_outbox_retry_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MEDIA_CATEGORIES = ("recordings", "evidence", "reports", "analytics", "temporary")
_MEDIA_LIFECYCLE_STATES = (
    "initiated",
    "uploading",
    "uploaded",
    "validating",
    "available",
    "failed",
    "expired",
    "deletion_pending",
    "deleted",
)

_SQL_GRANT = "GRANT SELECT, INSERT, UPDATE, DELETE ON media_assets TO hotelops_app;"
_SQL_REVOKE = "REVOKE ALL ON media_assets FROM hotelops_app;"

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)


def upgrade() -> None:
    # 1. Enums
    category_enum = ENUM(*_MEDIA_CATEGORIES, name="media_category", create_type=False)
    category_enum.create(op.get_bind(), checkfirst=True)

    lifecycle_enum = ENUM(*_MEDIA_LIFECYCLE_STATES, name="media_lifecycle_state", create_type=False)
    lifecycle_enum.create(op.get_bind(), checkfirst=True)

    # 2. Table
    op.create_table(
        "media_assets",
        sa.Column("media_id", sa.UUID(), primary_key=True),
        sa.Column("schema_version", sa.String(32), nullable=False, server_default="1.0"),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("category", category_enum, nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("storage_bucket", sa.String(128), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column(
            "lifecycle_state",
            lifecycle_enum,
            nullable=False,
            server_default="initiated",
        ),
        sa.Column("retention_class", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
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
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # Unique constraints
        sa.UniqueConstraint("media_id", "tenant_id", name="uq_media_assets_media_tenant"),
        sa.UniqueConstraint("object_key", name="uq_media_assets_object_key"),
        # Checks
        sa.CheckConstraint("length(btrim(object_key)) > 0", name="ck_media_assets_key_not_empty"),
        sa.CheckConstraint("length(btrim(storage_uri)) > 0", name="ck_media_assets_uri_not_empty"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_media_assets_size_non_negative"),
        sa.CheckConstraint(
            "checksum_sha256 IS NULL OR checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_media_assets_checksum_sha256",
        ),
        sa.CheckConstraint(
            "(event_id IS NULL AND event_time IS NULL) "
            "OR (event_id IS NOT NULL AND event_time IS NOT NULL)",
            name="ck_media_assets_event_pair",
        ),
        # Foreign Keys
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_media_assets_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="SET NULL",
            name="fk_media_assets_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="SET NULL",
            name="fk_media_assets_session_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["event_time", "event_id"],
            ["operational_events.event_time", "operational_events.event_id"],
            ondelete="SET NULL",
            name="fk_media_assets_event",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.user_id"],
            ondelete="SET NULL",
            name="fk_media_assets_created_by_user",
        ),
    )

    # 3. Indexes
    op.create_index("ix_media_assets_tenant_id", "media_assets", ["tenant_id"])
    op.create_index("ix_media_assets_venue_id", "media_assets", ["venue_id"])
    op.create_index(
        "ix_media_assets_tenant_category_state",
        "media_assets",
        ["tenant_id", "category", "lifecycle_state"],
    )
    op.create_index("ix_media_assets_camera_id", "media_assets", ["camera_id"])
    op.create_index("ix_media_assets_session_id", "media_assets", ["session_id"])
    op.create_index("ix_media_assets_event_id", "media_assets", ["event_id"])
    op.create_index("ix_media_assets_created_at", "media_assets", ["created_at"])
    op.create_index(
        "ix_media_assets_retention_sweep",
        "media_assets",
        ["expires_at"],
        postgresql_where=sa.text("lifecycle_state = 'available' AND expires_at IS NOT NULL"),
    )

    # 4. Grants & RLS
    op.execute(_SQL_GRANT)
    op.execute("ALTER TABLE media_assets ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE media_assets FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY media_assets_all ON media_assets FOR ALL TO hotelops_app "
        f"USING (tenant_id = {_CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS media_assets_all ON media_assets;")
    op.execute("ALTER TABLE media_assets NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE media_assets DISABLE ROW LEVEL SECURITY;")
    op.execute(_SQL_REVOKE)

    op.drop_index("ix_media_assets_retention_sweep", table_name="media_assets")
    op.drop_index("ix_media_assets_created_at", table_name="media_assets")
    op.drop_index("ix_media_assets_event_id", table_name="media_assets")
    op.drop_index("ix_media_assets_session_id", table_name="media_assets")
    op.drop_index("ix_media_assets_camera_id", table_name="media_assets")
    op.drop_index("ix_media_assets_tenant_category_state", table_name="media_assets")
    op.drop_index("ix_media_assets_venue_id", table_name="media_assets")
    op.drop_index("ix_media_assets_tenant_id", table_name="media_assets")

    op.drop_table("media_assets")

    op.execute("DROP TYPE IF EXISTS media_lifecycle_state;")
    op.execute("DROP TYPE IF EXISTS media_category;")
