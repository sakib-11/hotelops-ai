"""SQLAlchemy ORM model for centralized media metadata tracking (Task 9.6).

PostgreSQL stores authoritative metadata, lifecycle state, ownership,
provenance, and checksums. Actual media binaries reside in object storage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION

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


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MediaAssetModel(Base):
    """Centralized metadata record for media stored in object storage."""

    __tablename__ = "media_assets"

    __table_args__ = (
        # Composite unique for tenant isolation & foreign key targets
        UniqueConstraint("media_id", "tenant_id", name="uq_media_assets_media_tenant"),
        UniqueConstraint("object_key", name="uq_media_assets_object_key"),
        # Constraints
        CheckConstraint("length(btrim(object_key)) > 0", name="ck_media_assets_key_not_empty"),
        CheckConstraint("length(btrim(storage_uri)) > 0", name="ck_media_assets_uri_not_empty"),
        CheckConstraint("size_bytes >= 0", name="ck_media_assets_size_non_negative"),
        CheckConstraint(
            "checksum_sha256 IS NULL OR checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_media_assets_checksum_sha256",
        ),
        # Atomic event link pair
        CheckConstraint(
            "(event_id IS NULL AND event_time IS NULL) "
            "OR (event_id IS NOT NULL AND event_time IS NOT NULL)",
            name="ck_media_assets_event_pair",
        ),
        # Tenancy & Venue Composite Foreign Key
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_media_assets_venue_tenant",
        ),
        # Provenance FKs
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="SET NULL",
            name="fk_media_assets_camera_tenant",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="SET NULL",
            name="fk_media_assets_session_tenant",
        ),
        ForeignKeyConstraint(
            ["event_time", "event_id"],
            ["operational_events.event_time", "operational_events.event_id"],
            ondelete="SET NULL",
            name="fk_media_assets_event",
        ),
        # Indexes for query patterns
        Index("ix_media_assets_tenant_id", "tenant_id"),
        Index("ix_media_assets_venue_id", "venue_id"),
        Index(
            "ix_media_assets_tenant_category_state",
            "tenant_id",
            "category",
            "lifecycle_state",
        ),
        Index("ix_media_assets_camera_id", "camera_id"),
        Index("ix_media_assets_session_id", "session_id"),
        Index("ix_media_assets_event_id", "event_id"),
        Index("ix_media_assets_created_at", "created_at"),
    )

    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schema_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SCHEMA_VERSION,
        server_default=SCHEMA_VERSION,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(
        Enum(*_MEDIA_CATEGORIES, name="media_category"),
        nullable=False,
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    lifecycle_state: Mapped[str] = mapped_column(
        Enum(*_MEDIA_LIFECYCLE_STATES, name="media_lifecycle_state"),
        nullable=False,
        default="initiated",
        server_default="initiated",
    )
    retention_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Provenance references
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL", name="fk_media_assets_created_by_user"),
        nullable=True,
    )

    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        default=None,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<MediaAssetModel({self.media_id}) category={self.category!r} "
            f"state={self.lifecycle_state!r} key={self.object_key!r}>"
        )
