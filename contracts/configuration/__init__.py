"""Camera/Venue Configuration Domain Contracts (Task 10).

This package defines the canonical contracts for the Camera/Venue Configuration
domain - the versioned physical model used by the computer-vision pipeline.

The domain guarantees historical reproducibility of CV results even when the
physical venue changes (cameras moved, zones changed, tables relocated, etc.).

Core invariants:
- Published configuration versions are IMMUTABLE
- Physical changes create NEW versions (Draft -> Validated -> Published)
- Video sessions PIN to a specific published configuration version
- Historical sessions ALWAYS resolve through their pinned version

Geometry comes from contracts.geometry (the single authoritative model).
"""

from contracts.configuration.models import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationModel,
    ConfigurationStatus,
    ConfigurationVersionModel,
    CoordinateSpace,
    EntranceDirection,
    EntranceModel,
    ExclusionROIModel,
    GeometryModel,
    GeometryScope,
    GeometryType,
    OverlapPolicy,
    PrivacyROIModel,
    QueueAreaModel,
    ServiceAreaModel,
    TableModel,
    ValidationFindingModel,
    ValidationResultModel,
    ZoneModel,
    ZoneType,
)

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
