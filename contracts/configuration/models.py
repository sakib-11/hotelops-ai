"""Configuration domain models (Task 10).

Canonical contracts for the versioned Camera/Venue Configuration domain.

The configuration is a DOMAIN SNAPSHOT used to interpret CV observations.
It is NOT merely a UI configuration table.

Architecture:
    Tenant
      │
      └── Venue
           │
           └── Configuration
                  │
                  ├── Draft Version
                  ├── Validated Version
                  └── Published Version
                           │
                           ▼
                     Video Session (pins to published version)
                           │
                           ▼
                     CV Processing (uses pinned version)
                           │
                           ▼
                     Events / Evidence / Results

Fundamental Rule:
    Published configuration versions are IMMUTABLE.
    A physical change never modifies an existing published version.
    Instead: Published V1 -> create new Draft V2 -> modify -> validate -> publish

Geometry is the single authoritative model from contracts.geometry
(ADR-010); this package never defines its own geometry model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationId,
    ConfigurationVersionId,
    TenantId,
    VenueId,
    validate_schema_version,
    validate_utc,
)
from contracts.geometry.models import (
    CoordinateSpace,
    GeometryModel,
    GeometryScope,
    GeometryType,
    OverlapPolicy,
)

# =============================================================================
# Enums
# =============================================================================


class ConfigurationStatus(StrEnum):
    """Configuration version lifecycle state.

    Authoritative states per Task 10.3:
    DRAFT -> VALIDATING -> VALIDATED -> PUBLISHED

    No ARCHIVED, FAILED, REJECTED, DELETED states.
    Historical versions remain PUBLISHED; currentness is tracked via
    ConfigurationModel.current_published_version_id.
    """

    DRAFT = "draft"
    VALIDATING = "validating"
    VALIDATED = "validated"
    PUBLISHED = "published"


class ZoneType(StrEnum):
    """Semantic types of zones."""

    LOBBY = "lobby"
    RECEPTION = "reception"
    RESTAURANT = "restaurant"
    POOL_AREA = "pool_area"
    CORRIDOR = "corridor"
    PARKING = "parking"
    WAITING_AREA = "waiting_area"
    CUSTOM = "custom"


class EntranceDirection(StrEnum):
    """Direction semantics for entrances."""

    ENTRANCE = "entrance"
    EXIT = "exit"
    BIDIRECTIONAL = "bidirectional"


class CameraMountType(StrEnum):
    """Camera mounting types."""

    CEILING = "ceiling"
    WALL = "wall"
    POLE = "pole"
    CORNER = "corner"
    RECESSED = "recessed"


# =============================================================================
# Camera Profile
# =============================================================================


class CameraProfileModel(BaseModel, frozen=True):
    """Camera profile from the CV perspective.

    Represents a physical camera's configuration at a specific point in time.
    Belongs to a ConfigurationVersion (version-owned, not globally identifiable).
    """

    model_config = {"extra": "forbid"}

    # Identity within the configuration version
    profile_id: str = Field(..., description="Unique within configuration version")
    # Reference to the physical camera (globally identifiable)
    camera_id: CameraId
    # Camera reference for display/lookup
    camera_reference: str = Field(..., min_length=1)

    # Physical/installation metadata
    mount_type: CameraMountType = CameraMountType.CEILING
    mount_height_meters: Decimal | None = Field(default=None, ge=0)
    tilt_degrees: Decimal | None = Field(default=None, ge=-90, le=90)
    pan_degrees: Decimal | None = Field(default=None, ge=-180, le=180)
    roll_degrees: Decimal | None = Field(default=None, ge=-180, le=180)

    # Image characteristics
    resolution_width: int = Field(..., ge=1)
    resolution_height: int = Field(..., ge=1)
    fps: Decimal | None = Field(default=None, gt=0)
    codec: str | None = None

    # Orientation (which way is "up" in the image)
    # Used for coordinate transformations
    image_orientation: int = Field(default=0, ge=0, le=3)  # 0, 90, 180, 270 degrees

    # CV-relevant configuration
    analysis_enabled: bool = True
    detection_zones: list[str] = Field(default_factory=list)  # Zone profile_ids this camera covers
    privacy_rois: list[str] = Field(default_factory=list)  # PrivacyROI profile_ids
    exclusion_rois: list[str] = Field(default_factory=list)  # ExclusionROI profile_ids

    # Camera physical placement (venue-local POINT) — optional. When
    # present it must be a VENUE-scoped point in VENUE_LOCAL space.
    physical_placement: GeometryModel | None = None

    # Metadata
    metadata: dict[str, Any] | None = None


# =============================================================================
# Zone
# =============================================================================


class ZoneModel(BaseModel, frozen=True):
    """Semantic region in the venue.

    Examples: Lobby, Reception, Restaurant, Pool Area, Corridor, Parking, Waiting Area.
    Contains geometry defining the zone boundary (valid POLYGON in VENUE_LOCAL).
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=255)
    zone_type: ZoneType = ZoneType.CUSTOM
    geometry: GeometryModel
    # Semantic labels for CV interpretation
    labels: list[str] = Field(default_factory=list)
    # Child objects contained in this zone (by profile_id)
    contained_tables: list[str] = Field(default_factory=list)
    contained_entrances: list[str] = Field(default_factory=list)
    contained_queue_areas: list[str] = Field(default_factory=list)
    contained_service_areas: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# =============================================================================
# Table
# =============================================================================


class TableModel(BaseModel, frozen=True):
    """Physical table/seat grouping relevant to CV.

    Tables must be versioned because their physical positions can change.
    Geometry is a valid POLYGON in VENUE_LOCAL space.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=100)
    geometry: GeometryModel
    # Seat count for occupancy estimation
    seat_count: int | None = Field(default=None, ge=1)
    # Table shape/type for CV processing
    table_shape: str | None = None
    metadata: dict[str, Any] | None = None


# =============================================================================
# Entrance
# =============================================================================


class EntranceModel(BaseModel, frozen=True):
    """Entrance/exit region used by CV.

    Examples: Main Entrance, Staff Entrance, Restaurant Entrance, Emergency Exit.
    Geometry may be a LINESTRING (threshold) or POLYGON (door zone) in
    VENUE_LOCAL space per the entity geometry contract.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=255)
    geometry: GeometryModel
    direction: EntranceDirection = EntranceDirection.BIDIRECTIONAL
    # Zone this entrance belongs to (by profile_id)
    zone_profile_id: str | None = None
    # Camera profiles that cover this entrance (by profile_id)
    camera_profiles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# =============================================================================
# Queue Area
# =============================================================================


class QueueAreaModel(BaseModel, frozen=True):
    """Region where people may form a queue.

    Examples: Reception Queue, Restaurant Queue, Check-in Queue.
    Geometry is a valid POLYGON in VENUE_LOCAL space.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=255)
    geometry: GeometryModel
    # Direction of queue formation (for CV tracking)
    queue_direction: list[float] | None = Field(default=None, min_length=2, max_length=2)
    # Maximum expected queue length (for capacity alerts)
    max_queue_length: int | None = Field(default=None, ge=1)
    # Zone this queue belongs to (by profile_id)
    zone_profile_id: str | None = None
    # Camera profiles that cover this queue (by profile_id)
    camera_profiles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# =============================================================================
# Service Area
# =============================================================================


class ServiceAreaModel(BaseModel, frozen=True):
    """Region where service interaction occurs.

    Examples: Reception Desk, Restaurant Service Counter, Concierge Desk.
    Geometry is a valid POLYGON in VENUE_LOCAL space.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=255)
    geometry: GeometryModel
    # Service type for CV interpretation
    service_type: str | None = None
    # Zone this service area belongs to (by profile_id)
    zone_profile_id: str | None = None
    # Camera profiles that cover this service area (by profile_id)
    camera_profiles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# =============================================================================
# Privacy ROI
# =============================================================================


class PrivacyROIModel(BaseModel, frozen=True):
    """Area where processing must be restricted/redacted per privacy policy.

    Represents a POLICY BOUNDARY - not merely a flag on a zone.
    PrivacyROIs are camera-relative (IMAGE_NORMALIZED, scope=CAMERA) or
    venue-relative (VENUE_LOCAL, scope=VENUE); camera-relative privacy
    geometry MUST reference a camera profile in the same version.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=255)
    geometry: GeometryModel
    # Privacy action: blur, mask, exclude, redact
    privacy_action: str = "blur"  # Literal["blur", "mask", "exclude", "redact"]
    # Regulation/policy reference
    policy_reference: str | None = None
    # Camera profiles this ROI applies to (by profile_id) - empty = all cameras
    camera_profiles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# =============================================================================
# Exclusion ROI
# =============================================================================


class ExclusionROIModel(BaseModel, frozen=True):
    """Area excluded from specific CV processing.

    Represents a POLICY BOUNDARY - different from Privacy ROI.
    Privacy = data protection; Exclusion = processing optimization/accuracy.
    Camera-relative exclusion geometry MUST reference a camera profile in
    the same configuration version.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(..., description="Unique within configuration version")
    name: str = Field(..., min_length=1, max_length=255)
    geometry: GeometryModel
    # Which CV tasks to exclude from this ROI
    excluded_tasks: list[str] = Field(
        default_factory=list
    )  # e.g., ["detection", "tracking", "counting"]
    # Reason for exclusion
    exclusion_reason: str | None = None
    # Camera profiles this ROI applies to (by profile_id) - empty = all cameras
    camera_profiles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# =============================================================================
# Validation result (structured, versioned, deterministic)
# =============================================================================


class ValidationFindingModel(BaseModel, frozen=True):
    """A single structured validation finding (stable rule code)."""

    model_config = {"extra": "forbid"}

    code: str  # stable rule code, e.g. "GEOMETRY_INVALID"
    severity: str  # "error" | "warning"
    message: str
    entity_type: str | None = None
    entity_id: str | None = None
    related_entity_id: str | None = None


class ValidationResultModel(BaseModel, frozen=True):
    """Structured result of a deterministic configuration validation.

    ``content_revision`` is the exact revision/snapshot identity of the
    configuration content that was validated. A validation result is
    STALE the moment the configuration content changes: the publish
    service compares ``content_revision`` against the current revision
    and rejects publication on mismatch.
    """

    model_config = {"extra": "forbid"}

    valid: bool
    validator_version: str
    content_revision: str
    configuration_version_id: ConfigurationVersionId
    configuration_id: ConfigurationId
    tenant_id: TenantId
    venue_id: VenueId
    validated_at: datetime
    validated_by: str
    errors: list[ValidationFindingModel] = Field(default_factory=list)
    warnings: list[ValidationFindingModel] = Field(default_factory=list)
    checks_performed: int = 0

    _validate_validated_at = field_validator("validated_at")(validate_utc)

    @property
    def blocking_errors(self) -> list[ValidationFindingModel]:
        """Errors that prevent VALIDATED/PUBLISHED."""
        return self.errors


# =============================================================================
# Configuration Version
# =============================================================================


class ConfigurationVersionModel(BaseModel, frozen=True):
    """Immutable physical snapshot of venue configuration.

    A ConfigurationVersion represents an EXACT immutable physical snapshot.
    Configuration ≠ ConfigurationVersion - this distinction is critical.

    Lifecycle: DRAFT -> VALIDATING -> VALIDATED -> PUBLISHED
    Once PUBLISHED, the entire version becomes IMMUTABLE.
    PUBLISHED is terminal - no transitions out.
    """

    model_config = {"extra": "forbid"}

    # Identity
    configuration_version_id: ConfigurationVersionId
    configuration_id: ConfigurationId
    venue_id: VenueId
    tenant_id: TenantId

    # Versioning
    version: int = Field(..., ge=1)
    status: ConfigurationStatus = ConfigurationStatus.DRAFT

    # Physical model - all version-owned (not globally identifiable)
    cameras: list[CameraProfileModel] = Field(default_factory=list)
    zones: list[ZoneModel] = Field(default_factory=list)
    tables: list[TableModel] = Field(default_factory=list)
    entrances: list[EntranceModel] = Field(default_factory=list)
    queue_areas: list[QueueAreaModel] = Field(default_factory=list)
    service_areas: list[ServiceAreaModel] = Field(default_factory=list)
    privacy_rois: list[PrivacyROIModel] = Field(default_factory=list)
    exclusion_rois: list[ExclusionROIModel] = Field(default_factory=list)

    # Validation — structured result of the LAST validation pass.
    validation_result: ValidationResultModel | None = None
    # Backward-compatible flattened error list (legacy field).
    validation_errors: list[str] = Field(default_factory=list)
    validated_at: datetime | None = None
    validated_by: str | None = None

    # Publication
    published_at: datetime | None = None
    published_by: str | None = None
    # The version this replaced (for audit trail)
    replaced_version_id: ConfigurationVersionId | None = None

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)
    _validate_updated = field_validator("updated_at")(validate_utc)
    _validate_validated = field_validator("validated_at")(validate_utc)
    _validate_published = field_validator("published_at")(validate_utc)

    @model_validator(mode="after")
    def _validate_profile_ids_unique(self) -> ConfigurationVersionModel:
        """Ensure all profile_ids are unique within their category."""
        all_ids = set()
        # Every item is a version-owned profile model exposing .profile_id;
        # the tuple list is heterogeneous so the element type is explicit.
        categories: list[tuple[str, list[Any]]] = [
            ("camera", self.cameras),
            ("zone", self.zones),
            ("table", self.tables),
            ("entrance", self.entrances),
            ("queue_area", self.queue_areas),
            ("service_area", self.service_areas),
            ("privacy_roi", self.privacy_rois),
            ("exclusion_roi", self.exclusion_rois),
        ]
        for _category, items in categories:
            for item in items:
                if item.profile_id in all_ids:
                    raise ValueError(f"Duplicate profile_id '{item.profile_id}' across categories")
                all_ids.add(item.profile_id)
        return self

    @model_validator(mode="after")
    def _validate_published_immutable(self) -> ConfigurationVersionModel:
        """Published versions must have all required fields and be complete."""
        if self.status == ConfigurationStatus.PUBLISHED:
            if not self.cameras:
                raise ValueError("Published configuration must have at least one camera")
            if self.validated_at is None:
                raise ValueError("Published configuration must have been validated")
            if self.validated_by is None:
                raise ValueError("Published configuration must have validator")
            if self.published_at is None:
                raise ValueError("Published configuration must have publication timestamp")
            if self.published_by is None:
                raise ValueError("Published configuration must have publisher")
        return self

    def content_revision(self) -> str:
        """Deterministic revision identity of this version's content.

        ANY change to cameras/zones/tables/entrances/queue_areas/
        service_areas/privacy_rois/exclusion_rois changes the revision.
        A validation result is bound to the revision computed at the
        time of validation; publication re-checks it.
        """
        import hashlib
        import json

        payload = {
            "version": self.version,
            "cameras": [c.model_dump(mode="json", exclude_none=True) for c in self.cameras],
            "zones": [z.model_dump(mode="json", exclude_none=True) for z in self.zones],
            "tables": [t.model_dump(mode="json", exclude_none=True) for t in self.tables],
            "entrances": [e.model_dump(mode="json", exclude_none=True) for e in self.entrances],
            "queue_areas": [q.model_dump(mode="json", exclude_none=True) for q in self.queue_areas],
            "service_areas": [
                s.model_dump(mode="json", exclude_none=True) for s in self.service_areas
            ],
            "privacy_rois": [
                p.model_dump(mode="json", exclude_none=True) for p in self.privacy_rois
            ],
            "exclusion_rois": [
                x.model_dump(mode="json", exclude_none=True) for x in self.exclusion_rois
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# =============================================================================
# Configuration (the logical aggregate)
# =============================================================================


class ConfigurationModel(BaseModel, frozen=True):
    """Logical configuration belonging to a venue.

    A Configuration represents the logical configuration belonging to a venue.
    It contains multiple ConfigurationVersions over time.

    Configuration ≠ ConfigurationVersion - this distinction is critical.
    """

    model_config = {"extra": "forbid"}

    configuration_id: ConfigurationId
    venue_id: VenueId
    tenant_id: TenantId
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    # Current active published version (set when a version is published)
    current_published_version_id: ConfigurationVersionId | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)
    _validate_updated = field_validator("updated_at")(validate_utc)


__all__ = [
    "CameraMountType",
    "CameraProfileModel",
    "ConfigurationModel",
    "ConfigurationStatus",
    "ConfigurationVersionModel",
    "CoordinateSpace",
    "EntranceDirection",
    "EntranceModel",
    "ExclusionROIModel",
    "GeometryModel",
    "GeometryScope",
    "GeometryType",
    "OverlapPolicy",
    "PrivacyROIModel",
    "QueueAreaModel",
    "ServiceAreaModel",
    "TableModel",
    "ValidationFindingModel",
    "ValidationResultModel",
    "ZoneModel",
    "ZoneType",
]
