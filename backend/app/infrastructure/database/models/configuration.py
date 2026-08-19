"""SQLAlchemy ORM models for the configuration domain (Task 10).

Implements the versioned Camera/Venue physical model consumed by CV:

  configurations        — logical aggregate (one per tenant+venue)
  configuration_versions — immutable snapshots with lifecycle status
                           DRAFT -> VALIDATING -> VALIDATED -> PUBLISHED
  config_camera_profiles, config_zones, config_tables, config_entrances,
  config_queue_areas, config_service_areas, config_privacy_rois,
  config_exclusion_rois — version-owned entities with strong FK
                           ownership and same-version reference
                           guarantees.

Design decisions (matching repository conventions):
  - DIRECT tenant ownership: every table carries ``tenant_id`` NOT NULL
    plus composite FKs (venue_id, tenant_id) -> venues and
    (configuration_version_id, tenant_id) -> configuration_versions —
    the pattern established in migrations 003/005/007/017. Cross-tenant
    references are rejected by composite FKs.
  - Geometry is stored as canonical JSONB (the contracts.geometry
    GeometryModel serialization). PostGIS spatial queries in production
    use ST_GeomFromGeoJSON(geometry) expression indexes (GIST); the
    deterministic validation engine works offline via the pure-Python
    spatial engine behind the same protocol.
  - Status/lifecycle enums are DB-enforced via CHECK constraints so the
    state machine cannot be bypassed by direct SQL.
  - PUBLISHED versions are immutable at the application layer; the DB
    enforces ownership/structure, and the publish service enforces the
    state machine transactionally.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.identity import VenueModel
from backend.app.infrastructure.database.models.video import CameraModel

_CONFIG_STATUSES = ("draft", "validating", "validated", "published")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# Configuration aggregate
# =============================================================================


class ConfigurationModel(Base):
    __tablename__ = "configurations"

    __table_args__ = (
        UniqueConstraint("configuration_id", "tenant_id", name="uq_configurations_config_tenant"),
        # One logical configuration per tenant+venue (domain invariant).
        UniqueConstraint("venue_id", "tenant_id", name="uq_configurations_venue_tenant"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_configurations_name_not_empty"),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_configurations_venue_tenant",
        ),
        Index("ix_configurations_tenant_id", "tenant_id"),
        Index("ix_configurations_venue_id", "venue_id"),
    )

    configuration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Current active published version — updated atomically by the
    # publish service. FK is added in the migration AFTER
    # configuration_versions exists (circular reference).
    current_published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=func.now(),
    )

    venue: Mapped[VenueModel] = relationship()
    versions: Mapped[list[ConfigurationVersionModel]] = relationship(
        back_populates="configuration",
        cascade="all, delete-orphan",
        foreign_keys="ConfigurationVersionModel.configuration_id",
    )

    def __repr__(self) -> str:
        return f"<ConfigurationModel({self.configuration_id}) venue={self.venue_id}>"


# =============================================================================
# Configuration version
# =============================================================================


class ConfigurationVersionModel(Base):
    __tablename__ = "configuration_versions"

    __table_args__ = (
        UniqueConstraint(
            "configuration_version_id",
            "tenant_id",
            name="uq_config_versions_version_tenant",
        ),
        # Monotonic version numbers per configuration, never reused.
        UniqueConstraint("configuration_id", "version", name="uq_config_versions_config_version"),
        CheckConstraint("version >= 1", name="ck_config_versions_version_positive"),
        # Lifecycle invariants at the DB layer.
        CheckConstraint(
            "status IN ('draft', 'validating', 'validated', 'published')",
            name="ck_config_versions_status",
        ),
        CheckConstraint(
            "status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL)",
            name="ck_config_versions_published_complete",
        ),
        CheckConstraint(
            "status <> 'validated' OR validated_at IS NOT NULL",
            name="ck_config_versions_validated_complete",
        ),
        CheckConstraint(
            "status <> 'published' OR replaced_version_id IS DISTINCT FROM configuration_version_id",
            name="ck_config_versions_no_self_replace",
        ),
        ForeignKeyConstraint(
            ["configuration_id", "tenant_id"],
            ["configurations.configuration_id", "configurations.tenant_id"],
            ondelete="CASCADE",
            name="fk_config_versions_configuration_tenant",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_config_versions_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["replaced_version_id", "tenant_id"],
            ["configuration_versions.configuration_version_id", "configuration_versions.tenant_id"],
            ondelete="SET NULL",
            name="fk_config_versions_replaced",
        ),
        Index("ix_config_versions_tenant_id", "tenant_id"),
        Index("ix_config_versions_venue_id", "venue_id"),
        Index("ix_config_versions_configuration_id", "configuration_id"),
        Index("ix_config_versions_status", "status"),
    )

    configuration_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_CONFIG_STATUSES, name="config_version_status"),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    # Structured ValidationResultModel (JSONB) bound to content_revision.
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    replaced_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1.0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=func.now(),
    )

    configuration: Mapped[ConfigurationModel] = relationship(
        back_populates="versions",
        foreign_keys=[configuration_id],
    )

    def __repr__(self) -> str:
        return (
            f"<ConfigurationVersionModel({self.configuration_version_id}) "
            f"v{self.version} [{self.status}]>"
        )


# =============================================================================
# Version-owned entity tables
# =============================================================================

# Common constraint/column shape for the 8 version-owned entity tables.
# Each entity belongs to EXACTLY one configuration version and one
# tenant/venue; cross-tenant or cross-version references are rejected by
# composite FKs.


def _entity_table_args(
    table_name: str,
    *,
    unique_profile: bool = True,
    geometry_required: bool = True,
) -> tuple[Any, ...]:
    args: list[Any] = [
        # Same-version ownership: entity -> configuration_version.
        ForeignKeyConstraint(
            ["configuration_version_id", "tenant_id"],
            ["configuration_versions.configuration_version_id", "configuration_versions.tenant_id"],
            ondelete="CASCADE",
            name=f"fk_{table_name}_version_tenant",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name=f"fk_{table_name}_venue_tenant",
        ),
        CheckConstraint(
            "length(btrim(profile_id)) > 0", name=f"ck_{table_name}_profile_id_not_empty"
        ),
        CheckConstraint(
            "coordinate_space IN ('image_normalized', 'venue_local')",
            name=f"ck_{table_name}_coordinate_space",
        ),
        CheckConstraint(
            "geometry_type IN ('point', 'linestring', 'polygon')",
            name=f"ck_{table_name}_geometry_type",
        ),
        Index(f"ix_{table_name}_tenant_id", "tenant_id"),
        Index(f"ix_{table_name}_venue_id", "venue_id"),
        Index(f"ix_{table_name}_version_id", "configuration_version_id"),
    ]
    if geometry_required:
        args.append(
            CheckConstraint("geometry IS NOT NULL", name=f"ck_{table_name}_geometry_required")
        )
    if unique_profile:
        args.append(
            UniqueConstraint(
                "configuration_version_id",
                "profile_id",
                name=f"uq_{table_name}_version_profile",
            )
        )
    return tuple(args)


class CameraProfileEntityModel(Base):
    __tablename__ = "config_camera_profiles"
    # Camera physical placement (venue-local POINT) is OPTIONAL per the
    # contract, so geometry is nullable and no geometry-required check.
    __table_args__ = (
        *_entity_table_args("config_camera_profiles", geometry_required=False),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_config_camera_profiles_camera_tenant",
        ),
        CheckConstraint(
            "resolution_width > 0 AND resolution_height > 0",
            name="ck_config_camera_profiles_resolution",
        ),
        CheckConstraint("fps IS NULL OR fps > 0", name="ck_config_camera_profiles_fps_positive"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    camera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    camera_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    mount_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ceiling")
    mount_height_meters: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    tilt_degrees: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    pan_degrees: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    roll_degrees: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    resolution_width: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_height: Mapped[int] = mapped_column(Integer, nullable=False)
    fps: Mapped[Decimal | None] = mapped_column(Numeric(7, 3), nullable=True)
    codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_orientation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analysis_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detection_zones: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    privacy_rois: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    exclusion_rois: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Canonical geometry JSONB (camera physical placement point, optional).
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    camera: Mapped[CameraModel] = relationship()

    def __repr__(self) -> str:
        return f"<CameraProfileEntityModel({self.profile_id}) camera={self.camera_id}>"


class ZoneEntityModel(Base):
    __tablename__ = "config_zones"
    __table_args__ = (
        *_entity_table_args("config_zones"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_zones_name_not_empty"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_type: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    labels: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    contained_tables: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    contained_entrances: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    contained_queue_areas: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    contained_service_areas: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class TableEntityModel(Base):
    __tablename__ = "config_tables"
    __table_args__ = (
        *_entity_table_args("config_tables"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_tables_name_not_empty"),
        CheckConstraint("seat_count IS NULL OR seat_count > 0", name="ck_config_tables_seat_count"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    seat_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    table_shape: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class EntranceEntityModel(Base):
    __tablename__ = "config_entrances"
    __table_args__ = (
        *_entity_table_args("config_entrances"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_entrances_name_not_empty"),
        CheckConstraint(
            "direction IN ('entrance', 'exit', 'bidirectional')",
            name="ck_config_entrances_direction",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    direction: Mapped[str] = mapped_column(String(24), nullable=False, default="bidirectional")
    zone_profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    camera_profiles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class QueueAreaEntityModel(Base):
    __tablename__ = "config_queue_areas"
    __table_args__ = (
        *_entity_table_args("config_queue_areas"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_queue_areas_name_not_empty"),
        CheckConstraint(
            "max_queue_length IS NULL OR max_queue_length > 0",
            name="ck_config_queue_areas_max_length",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    queue_direction: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    max_queue_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    zone_profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    camera_profiles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ServiceAreaEntityModel(Base):
    __tablename__ = "config_service_areas"
    __table_args__ = (
        *_entity_table_args("config_service_areas"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_service_areas_name_not_empty"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    service_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zone_profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    camera_profiles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class PrivacyROIEntityModel(Base):
    __tablename__ = "config_privacy_rois"
    __table_args__ = (
        *_entity_table_args("config_privacy_rois"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_privacy_rois_name_not_empty"),
        CheckConstraint(
            "privacy_action IN ('blur', 'mask', 'exclude', 'redact')",
            name="ck_config_privacy_rois_action",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    privacy_action: Mapped[str] = mapped_column(String(24), nullable=False, default="blur")
    policy_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    camera_profiles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ExclusionROIEntityModel(Base):
    __tablename__ = "config_exclusion_rois"
    __table_args__ = (
        *_entity_table_args("config_exclusion_rois"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_config_exclusion_rois_name_not_empty"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    configuration_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coordinate_space: Mapped[str] = mapped_column(String(24), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    excluded_tasks: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    camera_profiles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


__all__ = [
    "CameraProfileEntityModel",
    "ConfigurationModel",
    "ConfigurationVersionModel",
    "EntranceEntityModel",
    "ExclusionROIEntityModel",
    "PrivacyROIEntityModel",
    "QueueAreaEntityModel",
    "ServiceAreaEntityModel",
    "TableEntityModel",
    "ZoneEntityModel",
]
