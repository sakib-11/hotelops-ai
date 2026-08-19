"""SQLAlchemy ORM models for the operational configuration domain (Task 6.5).

Implements typed configuration persistence for CCTV/video analysis —
explicitly NOT a generic key-value store (governance doc Section 3.3).
Only configuration required by the current architecture is defined:

  CameraConfigModel   — per-camera analysis configuration: analysis
                        enabled, frame rate, resolution, detection
                        sensitivity; versioned with effective state
  AnalysisConfigModel — per-venue analysis profile with typed thresholds
                        (occupancy, dwell, queue length, wait time)

Design decisions:

  - Typed columns with CHECK constraints instead of key-value rows.
  - JSONB (`parameters`) only for genuinely variable data
    (adapter-specific camera tuning, zone geometry) — Section 7.
  - Tenant ownership is DIRECT and DB-enforced: `tenant_id` NOT NULL
    plus composite FKs (camera/venue_id, tenant_id) -> cameras/venues —
    the pattern established in migrations 003/005. Cross-tenant
    references are rejected by composite FKs.
  - Version/effective-state semantics: `status` (draft/active/archived)
    marks the currently-effective row; `version` keeps relational
    change history ("Config change history is relational").
  - Unique active configuration rule: partial unique indexes
    (WHERE status = 'active') guarantee at most one active config per
    camera and per (venue, name) analysis profile.
  - created_at + updated_at (timestamptz, UTC). `updated_at` uses the
    ORM onupdate so application writes stamp the modification time;
    the database server default covers direct SQL inserts.
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
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.identity import VenueModel
from backend.app.infrastructure.database.models.video import CameraModel

# =============================================================================
# Enum values — effective-state semantics for configuration rows
# =============================================================================

_CONFIG_STATUSES = ("draft", "active", "archived")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# CameraConfig — per-camera analysis configuration
# =============================================================================


class CameraConfigModel(Base):
    __tablename__ = "camera_configs"

    __table_args__ = (
        UniqueConstraint("config_id", "tenant_id", name="uq_camera_configs_config_tenant"),
        # Versioned change history per camera (relational, not key-value).
        UniqueConstraint("camera_id", "version", name="uq_camera_configs_version"),
        CheckConstraint("version >= 1", name="ck_camera_configs_version_positive"),
        CheckConstraint(
            "frame_rate IS NULL OR frame_rate > 0",
            name="ck_camera_configs_frame_rate_positive",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="ck_camera_configs_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_camera_configs_height_positive"),
        CheckConstraint(
            "detection_sensitivity IS NULL OR "
            "(detection_sensitivity >= 0 AND detection_sensitivity <= 1)",
            name="ck_camera_configs_sensitivity_range",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_camera_configs_camera_tenant",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_camera_configs_venue_tenant",
        ),
        Index("ix_camera_configs_tenant_id", "tenant_id"),
        Index("ix_camera_configs_venue_id", "venue_id"),
        # NOTE (Task 6.13 review): no single-column index on camera_id —
        # camera_id-only lookups are served by uq_camera_configs_version
        # (camera_id, version), whose leftmost column is camera_id.
        # Unique active configuration rule: at most one active config per camera.
        Index(
            "uq_camera_configs_active",
            "camera_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_CONFIG_STATUSES, name="config_status"),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    analysis_enabled: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    frame_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 3), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detection_sensitivity: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Adapter-specific flexible parameters only (JSONB policy, governance Section 7).
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
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
        onupdate=func.now(),
    )

    # One-directional relationships (schema is the deliverable). overlaps:
    # both composite FKs copy tenant_id to this table, so the ORM cannot
    # resolve a single write path — reads are what matters here.
    camera: Mapped[CameraModel] = relationship(overlaps="venue")
    venue: Mapped[VenueModel] = relationship(overlaps="camera")

    def __repr__(self) -> str:
        return (
            f"<CameraConfigModel({self.config_id}) camera={self.camera_id} "
            f"v{self.version} [{self.status}]>"
        )


# =============================================================================
# AnalysisConfig — per-venue typed analysis profile / thresholds
# =============================================================================


class AnalysisConfigModel(Base):
    __tablename__ = "analysis_configs"

    __table_args__ = (
        UniqueConstraint("config_id", "tenant_id", name="uq_analysis_configs_config_tenant"),
        # Versioned change history per (venue, name) profile.
        UniqueConstraint("venue_id", "name", "version", name="uq_analysis_configs_version"),
        CheckConstraint("version >= 1", name="ck_analysis_configs_version_positive"),
        CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_analysis_configs_name_not_empty",
        ),
        CheckConstraint(
            "confidence_threshold IS NULL OR "
            "(confidence_threshold >= 0 AND confidence_threshold <= 1)",
            name="ck_analysis_configs_confidence_range",
        ),
        CheckConstraint(
            "frame_rate IS NULL OR frame_rate > 0",
            name="ck_analysis_configs_frame_rate_positive",
        ),
        CheckConstraint(
            "occupancy_threshold IS NULL OR "
            "(occupancy_threshold >= 0 AND occupancy_threshold <= 100)",
            name="ck_analysis_configs_occupancy_range",
        ),
        CheckConstraint(
            "dwell_time_seconds IS NULL OR dwell_time_seconds >= 0",
            name="ck_analysis_configs_dwell_non_negative",
        ),
        CheckConstraint(
            "queue_length_threshold IS NULL OR queue_length_threshold >= 0",
            name="ck_analysis_configs_queue_non_negative",
        ),
        CheckConstraint(
            "wait_time_seconds IS NULL OR wait_time_seconds >= 0",
            name="ck_analysis_configs_wait_non_negative",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_analysis_configs_venue_tenant",
        ),
        Index("ix_analysis_configs_tenant_id", "tenant_id"),
        # NOTE (Task 6.13 review): no single-column index on venue_id —
        # venue_id-only lookups are served by uq_analysis_configs_version
        # (venue_id, name, version) and the partial uq_analysis_configs_active.
        # Unique active configuration rule: at most one active profile
        # per (venue, name).
        Index(
            "uq_analysis_configs_active",
            "venue_id",
            "name",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_CONFIG_STATUSES, name="config_status"),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_threshold: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    frame_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 3), nullable=True)
    occupancy_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dwell_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_length_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wait_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Genuinely variable zone/geometry definitions only (JSONB policy).
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
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
        onupdate=func.now(),
    )

    # One-directional relationships (schema is the deliverable)
    venue: Mapped[VenueModel] = relationship()

    def __repr__(self) -> str:
        return (
            f"<AnalysisConfigModel({self.config_id}) venue={self.venue_id} "
            f"{self.name!r} v{self.version} [{self.status}]>"
        )
