"""Geometry domain contracts (Task 10.4).

Canonical contracts for the Geometry Model & Spatial Semantics.
Defines the immutable contract governing all spatial reasoning
within the HotelOps Computer Vision (CV) Engine.

Core Principles:
- Geometry is Versioned CV State (not frontend drawing data)
- Immutability: Once persisted against a version, geometry records MUST NOT be mutated
- Coordinate Space Fidelity: Cross-space operations are FORBIDDEN
- Privacy Precedence: PRIVACY_MASK > EXCLUSION_ZONE > Standard CV Zones
- Policy-Driven Validity: Overlap is not intrinsic error; validity from Overlap Policy Matrix
"""

from contracts.geometry.models import (
    CoordinateSpace,
    EntityGeometryContract,
    GeometryErrorCode,
    GeometryModel,
    GeometryScope,
    GeometryType,
    OverlapPolicy,
    SpatialValidationError,
    SpatialValidationResult,
)

__all__ = [
    "CoordinateSpace",
    "EntityGeometryContract",
    "GeometryErrorCode",
    "GeometryModel",
    "GeometryScope",
    "GeometryType",
    "OverlapPolicy",
    "SpatialValidationError",
    "SpatialValidationResult",
]
