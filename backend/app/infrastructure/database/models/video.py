"""SQLAlchemy ORM models for the video domain (Task 6.4).

Implements the foundational video persistence schema from the Task 4
contracts (contracts/video/models.py) and the Task 6.4 requirements:

  Camera ── belongs to exactly one Venue
  VideoStream ── live ingestion stream of a Camera (at a Venue)
  VideoAsset ── immutable reference to a source video (live or recorded);
                recorded assets reference object storage, never store bytes
  VideoSession ── processing session over a live (camera) or recorded
                  (asset) source, at a Venue

Tenant ownership is DIRECT and DB-enforced: every table carries
`tenant_id` NOT NULL and a composite foreign key
(venue_id, tenant_id) REFERENCES venues(venue_id, tenant_id) — the same
pattern migration 003 established for membership_venues. Cross-tenant
references to cameras/assets are likewise prevented by composite FKs
((camera_id, tenant_id) -> cameras, (asset_id, tenant_id) -> video_assets).

Large video binaries never live here: PostgreSQL stores identity,
ownership, metadata, object-storage references (storage_uri), timestamps,
status, and processing state. Bytes live in object storage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.identity import VenueModel

# =============================================================================
# Enum values — mirror contracts/video/models.py (SourceType) and domain state
# =============================================================================

_VIDEO_SOURCE_TYPES = ("live", "recorded")
_CAMERA_STATUSES = ("active", "inactive")
_CAMERA_PROTOCOLS = ("rtsp", "onvif")
_STREAM_STATUSES = ("active", "inactive")
# VideoSession status: recorded sessions use active/ended/failed
# Live sessions use the extended LiveVideoSessionStatus enum
_VIDEO_SESSION_STATUSES = (
    "active",
    "ended",
    "failed",
    "connecting",
    "degraded",
    "reconnecting",
    "stopped",
)
# Live-only operational statuses (subset of _VIDEO_SESSION_STATUSES)
_LIVE_SESSION_STATUSES = (
    "connecting",
    "active",
    "degraded",
    "reconnecting",
    "stopped",
    "failed",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# Camera
# =============================================================================


class CameraModel(Base):
    __tablename__ = "cameras"

    __table_args__ = (
        UniqueConstraint("camera_id", "tenant_id", name="uq_cameras_camera_tenant"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_cameras_name_not_empty"),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_cameras_venue_tenant",
        ),
        Index("ix_cameras_tenant_id", "tenant_id"),
        Index("ix_cameras_venue_id", "venue_id"),
    )

    camera_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_CAMERA_STATUSES, name="camera_status"),
        nullable=False,
        default="active",
        server_default="active",
    )
    protocol: Mapped[str] = mapped_column(
        Enum(*_CAMERA_PROTOCOLS, name="camera_protocol"),
        nullable=False,
        default="rtsp",
        server_default="rtsp",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )

    # One-directional relationships (schema is the deliverable)
    venue: Mapped[VenueModel] = relationship()
    streams: Mapped[list[VideoStreamModel]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<CameraModel({self.camera_id}) {self.name!r} [{self.status}]>"


# =============================================================================
# VideoStream — live ingestion stream metadata
# =============================================================================


class VideoStreamModel(Base):
    __tablename__ = "video_streams"

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="ck_video_streams_name_not_empty"),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_streams_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_streams_camera_tenant",
        ),
        Index("ix_video_streams_tenant_id", "tenant_id"),
        Index("ix_video_streams_camera_id", "camera_id"),
        Index("ix_video_streams_venue_id", "venue_id"),
    )

    stream_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_STREAM_STATUSES, name="stream_status"),
        nullable=False,
        default="active",
        server_default="active",
    )
    # Ingest endpoint (e.g. RTSP URL) provisioned by the camera adapter.
    # May embed credentials — never stored in JSONB; see governance Section 7.
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    camera: Mapped[CameraModel] = relationship(back_populates="streams", overlaps="venue")
    venue: Mapped[VenueModel] = relationship(overlaps="camera,streams")

    def __repr__(self) -> str:
        return f"<VideoStreamModel({self.stream_id}) camera={self.camera_id} [{self.status}]>"


# =============================================================================
# VideoAsset — immutable reference to a source video
# =============================================================================


class VideoAssetModel(Base):
    __tablename__ = "video_assets"

    __table_args__ = (
        UniqueConstraint("asset_id", "tenant_id", name="uq_video_assets_asset_tenant"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_video_assets_name_not_empty"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_video_assets_duration_non_negative",
        ),
        # A live asset is a camera stream (no stored bytes); a recorded
        # asset must reference object storage — bytes never live in PG.
        CheckConstraint(
            "(source_type = 'live' AND camera_id IS NOT NULL AND storage_uri IS NULL) "
            "OR (source_type = 'recorded' AND storage_uri IS NOT NULL)",
            name="ck_video_assets_source_consistent",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_assets_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_assets_camera_tenant",
        ),
        Index("ix_video_assets_tenant_id", "tenant_id"),
        Index("ix_video_assets_venue_id", "venue_id"),
        Index("ix_video_assets_camera_id", "camera_id"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(
        Enum(*_VIDEO_SOURCE_TYPES, name="video_source_type"),
        nullable=False,
    )
    # For live assets: the camera providing the stream.
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Forward reference to the evidence domain (contracts.EvidenceId).
    # Intentionally NOT a real FK: wiring one would create a dependency
    # cycle (evidence_refs -> video_sessions -> video_assets -> evidence_refs)
    # that SQLAlchemy cannot sort (see migration 009 note). Evidence->video
    # provenance flows through evidence_refs.session_id / camera_id instead.
    evidence_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    capture_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    # Object-storage reference (recorded assets) — bytes live in object storage.
    storage_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    media_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    venue: Mapped[VenueModel] = relationship(overlaps="camera")
    camera: Mapped[CameraModel | None] = relationship(overlaps="venue")
    sessions: Mapped[list[VideoSessionModel]] = relationship(back_populates="asset")

    def __repr__(self) -> str:
        return f"<VideoAssetModel({self.asset_id}) {self.name!r} [{self.source_type}]>"


# =============================================================================
# VideoSession — processing session over a live or recorded source
# =============================================================================


class VideoSessionModel(Base):
    __tablename__ = "video_sessions"

    __table_args__ = (
        CheckConstraint(
            "(source_type = 'live' AND camera_id IS NOT NULL) "
            "OR (source_type = 'recorded' AND asset_id IS NOT NULL)",
            name="ck_video_sessions_source_consistent",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_video_sessions_ended_after_started",
        ),
        # A session that is no longer active (ended/failed) must carry an
        # end time; an active session must not (status <-> ended_at link).
        CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) "
            "OR (status IN ('ended', 'failed') AND ended_at IS NOT NULL)",
            name="ck_video_sessions_status_consistent",
        ),
        # Composite FK target for operational_events.session_id (migration
        # 008) and evidence_refs.session_id (migration 009): a session can
        # only be referenced by events/evidence of its own tenant.
        UniqueConstraint("session_id", "tenant_id", name="uq_video_sessions_session_tenant"),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_sessions_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_sessions_camera_tenant",
        ),
        ForeignKeyConstraint(
            ["asset_id", "tenant_id"],
            ["video_assets.asset_id", "video_assets.tenant_id"],
            ondelete="CASCADE",
            name="fk_video_sessions_asset_tenant",
        ),
        # Session pinning (Task 10.13): a session references exactly one
        # configuration version. The FK is composite (version_id,
        # tenant_id) so a session can never pin another tenant's version.
        # Publication-only enforcement (status='published') is performed by
        # the session service — the DB enforces ownership/structure.
        ForeignKeyConstraint(
            ["configuration_version_id", "tenant_id"],
            ["configuration_versions.configuration_version_id", "configuration_versions.tenant_id"],
            ondelete="RESTRICT",
            name="fk_video_sessions_config_version_tenant",
        ),
        Index("ix_video_sessions_tenant_id", "tenant_id"),
        Index("ix_video_sessions_venue_id", "venue_id"),
        Index("ix_video_sessions_camera_id", "camera_id"),
        Index("ix_video_sessions_asset_id", "asset_id"),
        Index("ix_video_sessions_config_version_id", "configuration_version_id"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(
        Enum(*_VIDEO_SOURCE_TYPES, name="video_source_type"),
        nullable=False,
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Task 10.13 — pinned configuration version (published, immutable).
    configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Enum(*_VIDEO_SESSION_STATUSES, name="video_session_status"),
        nullable=False,
        default="active",
        server_default="active",
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    venue: Mapped[VenueModel] = relationship(overlaps="sessions")
    camera: Mapped[CameraModel | None] = relationship(overlaps="sessions,venue")
    asset: Mapped[VideoAssetModel | None] = relationship(
        back_populates="sessions", overlaps="camera,venue"
    )

    def __repr__(self) -> str:
        return (
            f"<VideoSessionModel({self.session_id}) {self.source_type} "
            f"venue={self.venue_id} [{self.status}]>"
        )


# =============================================================================
# LiveSessionTransitionLog — audit trail for live session FSM (Task 19.2)
# =============================================================================


class LiveSessionTransitionLogModel(Base):
    """Immutable audit record of every live video session state transition.

    Uses system/processing time (transition_time) — NOT event-time.
    Event-time is carried by FramePacket; this log tracks operational health.
    """

    __tablename__ = "live_session_transitions"

    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_live_session_transitions_session_tenant",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_live_session_transitions_venue_tenant",
        ),
        Index("ix_live_session_transitions_session_id", "session_id"),
        Index("ix_live_session_transitions_tenant_id", "tenant_id"),
        Index("ix_live_session_transitions_venue_id", "venue_id"),
        Index("ix_live_session_transitions_transition_time", "transition_time"),
        Index(
            "ix_live_session_transitions_session_time",
            "session_id",
            "transition_time",
        ),
    )

    transition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    previous_state: Mapped[str] = mapped_column(
        Enum(*_LIVE_SESSION_STATUSES, name="live_session_status"),
        nullable=False,
    )
    new_state: Mapped[str] = mapped_column(
        Enum(*_LIVE_SESSION_STATUSES, name="live_session_status"),
        nullable=False,
    )
    transition_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(1024), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # "system" | "actor"
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<LiveSessionTransitionLogModel({self.transition_id}) "
            f"session={self.session_id} {self.previous_state}->{self.new_state}>"
        )
