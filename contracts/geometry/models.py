"""Geometry domain contracts (Task 10.4) — authoritative per ADR-010.

This module is the SINGLE source of truth for the geometry model and
spatial semantics consumed by the CV engine. The configuration domain
(contracts/configuration) imports from here — a second geometry model
must never exist.

Core principles (ADR-010):
  - Geometry is versioned CV state, never frontend drawing data.
  - Every geometry MUST declare exactly one ``coordinate_space`` and one
    ``geometry_scope``; cross-space mixing is forbidden.
  - ``IMAGE_NORMALIZED`` coordinates are camera-relative and bounded to
    the unit square [0, 1] by [0, 1].
  - ``VENUE_LOCAL`` coordinates are venue-relative metric positions.
  - Supported primitives are POINT, LINESTRING and POLYGON only.
  - Privacy precedence: PRIVACY_MASK > EXCLUSION_ZONE > standard zones.
  - Overlap is not an intrinsic error — validity comes from the
    SpatialPolicyRegistry (Task 10.5).
"""

from __future__ import annotations

from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# =============================================================================
# Enums
# =============================================================================


class CoordinateSpace(StrEnum):
    """Authoritative coordinate reference systems (ADR-010 §3).

    IMAGE_NORMALIZED — camera-relative unit square, x/y ∈ [0, 1].
    VENUE_LOCAL      — venue-relative metric coordinates.
    """

    IMAGE_NORMALIZED = "image_normalized"
    VENUE_LOCAL = "venue_local"


class GeometryType(StrEnum):
    """Supported geometric primitives (ADR-010 §4)."""

    POINT = "point"
    LINESTRING = "linestring"
    POLYGON = "polygon"


class GeometryScope(StrEnum):
    """What a geometry is relative to.

    CAMERA — geometry anchored to a camera frame (requires
             ``reference_camera_profile_id`` within the same
             configuration version).
    VENUE  — geometry anchored to the venue floor plan.
    """

    CAMERA = "camera"
    VENUE = "venue"


class OverlapPolicy(StrEnum):
    """Policy for geometric overlaps between configuration objects.

    ALLOW    — overlap permitted (semantically valid sharing).
    REJECT   — overlap forbidden (blocking error).
    VALIDATE — overlap permitted but must satisfy declared conditions.
    """

    ALLOW = "allow"
    REJECT = "reject"
    VALIDATE = "validate"


class GeometryErrorCode(StrEnum):
    """Stable, machine-readable geometry/spatial rule codes.

    These codes are used by the deterministic validation engine so that
    identical content + identical validator version always produce the
    same result. Severity is classified by the engine, not the code.
    """

    GEOMETRY_INVALID = "GEOMETRY_INVALID"
    GEOMETRY_EMPTY = "GEOMETRY_EMPTY"
    GEOMETRY_OUT_OF_RANGE = "GEOMETRY_OUT_OF_RANGE"
    GEOMETRY_SELF_INTERSECTION = "GEOMETRY_SELF_INTERSECTION"
    GEOMETRY_ZERO_AREA = "GEOMETRY_ZERO_AREA"
    GEOMETRY_TYPE_INVALID = "GEOMETRY_TYPE_INVALID"
    GEOMETRY_NOT_CLOSED = "GEOMETRY_NOT_CLOSED"
    COORDINATE_SPACE_INVALID = "COORDINATE_SPACE_INVALID"
    COORDINATE_MIXED_SPACES = "COORDINATE_MIXED_SPACES"
    MISSING_REFERENCE = "MISSING_REFERENCE"
    CROSS_VERSION_REFERENCE = "CROSS_VERSION_REFERENCE"
    INVALID_CONTAINMENT = "INVALID_CONTAINMENT"
    TABLE_OVERLAP = "TABLE_OVERLAP"
    INVALID_SPATIAL_RELATIONSHIP = "INVALID_SPATIAL_RELATIONSHIP"
    CAMERA_REFERENCE_INVALID = "CAMERA_REFERENCE_INVALID"
    CAMERA_RETIRED = "CAMERA_RETIRED"
    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"
    CAMERA_PROFILE_INCOMPATIBLE = "CAMERA_PROFILE_INCOMPATIBLE"
    PRIVACY_POLICY_CONFLICT = "PRIVACY_POLICY_CONFLICT"
    EXCLUSION_POLICY_CONFLICT = "EXCLUSION_POLICY_CONFLICT"
    DUPLICATE_IDENTIFIER = "DUPLICATE_IDENTIFIER"
    ZONE_UNCOVERED = "ZONE_UNCOVERED"
    CAMERA_NO_CONFIGURED_COVERAGE = "CAMERA_NO_CONFIGURED_COVERAGE"
    ENTITY_GEOMETRY_CONTRACT_VIOLATION = "ENTITY_GEOMETRY_CONTRACT_VIOLATION"


# =============================================================================
# Geometry model
# =============================================================================

# Polygon exterior-ring closure tolerance (documented, deterministic).
_CLOSURE_EPSILON = 1e-6
# Coordinate rounding precision per coordinate space (ADR-010 §3).
_PRECISION: dict[CoordinateSpace, int] = {
    CoordinateSpace.IMAGE_NORMALIZED: 6,
    CoordinateSpace.VENUE_LOCAL: 3,
}

Point2D = list[float]  # [x, y] (optionally [x, y, z])


class GeometryModel(BaseModel, frozen=True):
    """Canonical geometry with explicit space/scope/type declaration.

    Coordinates are stored in canonical GeoJSON-style form:

      POINT:      coordinates = [[x, y]]
      LINESTRING: coordinates = [[x, y], [x, y], ...]           (N ≥ 2)
      POLYGON:    coordinates = [[x, y], [x, y], ...]           (ring,
                  closed — first point equals last within epsilon)

    Every geometry is canonicalized at construction: coordinates are
    rounded to the documented precision for their coordinate space and
    polygon rings are closed deterministically. NaN/inf and empty
    geometries are rejected.
    """

    model_config = {"extra": "forbid"}

    geometry_id: str = Field(
        ..., min_length=1, description="Stable id within the configuration version"
    )
    geometry_type: GeometryType
    coordinate_space: CoordinateSpace
    geometry_scope: GeometryScope
    coordinates: list[Point2D] = Field(..., min_length=1)
    # Camera-relative (IMAGE_NORMALIZED + scope=CAMERA) geometry MUST
    # reference a camera profile in the SAME configuration version.
    reference_camera_profile_id: str | None = None
    # Optional reference frame size for camera-relative geometry.
    reference_width: int | None = Field(default=None, ge=1)
    reference_height: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] | None = None

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinates(cls, v: list[Point2D]) -> list[Point2D]:
        if not v:
            raise ValueError("Geometry must not be empty")
        for point in v:
            if len(point) not in (2, 3):
                raise ValueError("Coordinate points must have exactly 2 or 3 values")
            for coord in point:
                if not isinstance(coord, (int, float)):
                    raise ValueError("Coordinates must be numbers")
                if not isfinite(coord):
                    raise ValueError("Coordinates must be finite (no NaN or infinity)")
        return v

    @model_validator(mode="after")
    def _validate_geometry_consistency(self) -> GeometryModel:
        n = len(self.coordinates)
        if self.geometry_type == GeometryType.POINT and n != 1:
            raise ValueError("POINT requires exactly one coordinate")
        if self.geometry_type == GeometryType.LINESTRING and n < 2:
            raise ValueError("LINESTRING requires at least 2 coordinates")
        if self.geometry_type == GeometryType.POLYGON and n < 4:
            raise ValueError("POLYGON requires a closed ring of at least 4 coordinates")
        # Scope <-> space coupling.
        if self.geometry_scope == GeometryScope.CAMERA:
            if self.coordinate_space != CoordinateSpace.IMAGE_NORMALIZED:
                raise ValueError("CAMERA-scoped geometry must use IMAGE_NORMALIZED coordinates")
            if not self.reference_camera_profile_id:
                raise ValueError(
                    "CAMERA-scoped geometry must reference a camera profile "
                    "(reference_camera_profile_id)"
                )
        if self.geometry_scope == GeometryScope.VENUE and (
            self.coordinate_space != CoordinateSpace.VENUE_LOCAL
        ):
            raise ValueError(
                "VENUE-scoped geometry must use VENUE_LOCAL coordinates"
            )  # IMAGE_NORMALIZED coordinates are bounded to the unit square
        # (ADR-010 INV-GEO-03) — enforced at the contract layer.
        if self.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED:
            for point in self.coordinates:
                for value in point[:2]:
                    if not (0.0 <= value <= 1.0):
                        raise ValueError("IMAGE_NORMALIZED coordinates must lie in [0, 1]")
        return self

    # =========================================================================
    # Canonicalization
    # =========================================================================

    def canonicalize(self) -> GeometryModel:
        """Return a deterministically normalized copy of this geometry.

        - rounds coordinates to the documented precision per space
        - closes POLYGON rings (first point == last point)
        - preserves stable identity fields

        Canonicalization happens BEFORE storage and validation so that
        floating-point noise can never produce inconsistent results.
        """
        precision = _PRECISION[self.coordinate_space]
        coords = [[round(c, precision) for c in point] for point in self.coordinates]
        if self.geometry_type == GeometryType.POLYGON:
            first = coords[0]
            last = coords[-1]
            closed = all(abs(a - b) <= _CLOSURE_EPSILON for a, b in zip(first, last, strict=True))
            if not closed:
                coords = [*coords, list(first)]
        return self.model_copy(update={"coordinates": coords})

    # =========================================================================
    # Convenience accessors
    # =========================================================================

    @property
    def is_camera_relative(self) -> bool:
        """True when the geometry is anchored to a camera frame."""
        return self.geometry_scope == GeometryScope.CAMERA

    @property
    def ring(self) -> list[Point2D]:
        """Exterior ring for POLYGON geometries (raises for non-polygons)."""
        if self.geometry_type != GeometryType.POLYGON:
            msg = f"ring() requires POLYGON, got {self.geometry_type.value}"
            raise ValueError(msg)
        return self.canonicalize().coordinates

    @property
    def area(self) -> float:
        """Signed area via the shoelace formula (absolute value).

        The authoritative production area computation is delegated to
        PostGIS (ST_Area); this pure-Python fallback exists so the
        deterministic validation engine is testable without a database.
        """
        ring = self.ring
        total = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            total += x1 * y2 - x2 * y1
        return abs(total) / 2.0

    def is_degenerate(self, min_area: float = 1e-9) -> bool:
        """True for zero/negative area polygons (blocking)."""
        if self.geometry_type != GeometryType.POLYGON:
            return False
        return self.area <= min_area

    def is_self_intersecting(self) -> bool:
        """Detect proper self-intersection of a polygon ring.

        Only non-adjacent segment pairs are tested; adjacent segments
        share a vertex by construction and are excluded. Deterministic
        pure-Python check (PostGIS ST_IsValid is authoritative in
        production).
        """
        if self.geometry_type != GeometryType.POLYGON:
            return False
        ring = self.ring
        segments = [(ring[i], ring[i + 1]) for i in range(len(ring) - 1)]
        for i in range(len(segments)):
            for j in range(i + 1, len(segments)):
                if j == i + 1 or (i == 0 and j == len(segments) - 1):
                    continue  # adjacent — shares a vertex
                if _segments_properly_intersect(
                    segments[i][0], segments[i][1], segments[j][0], segments[j][1]
                ):
                    return True
        return False


def _orientation(a: Point2D, b: Point2D, c: Point2D) -> float:
    """Cross product of (b - a) x (c - a) — sign of the turn."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point2D, b: Point2D, c: Point2D) -> bool:
    """True when point c lies on segment ab (collinear and within bbox)."""
    return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])


def _segments_properly_intersect(a: Point2D, b: Point2D, c: Point2D, d: Point2D) -> bool:
    """Proper intersection (crossing through interiors), not mere touching."""
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)
    if o1 == 0 and _on_segment(a, b, c):
        return False  # collinear touch — boundary contact, not crossing
    if o2 == 0 and _on_segment(a, b, d):
        return False
    if o3 == 0 and _on_segment(c, d, a):
        return False
    if o4 == 0 and _on_segment(c, d, b):
        return False
    return (o1 * o2 < 0) and (o3 * o4 < 0)


class EntityGeometryContract(BaseModel, frozen=True):
    """Authoritative geometry contract for a configuration entity type.

    Declares which primitives, coordinate spaces and scopes an entity
    may use, plus topology constraints (ADR-010 §5). The validation
    engine enforces this contract (rule ENTITY_GEOMETRY_CONTRACT_VIOLATION).
    """

    model_config = {"extra": "forbid"}

    entity_type: str
    allowed_geometry_types: frozenset[GeometryType]
    allowed_coordinate_spaces: frozenset[CoordinateSpace]
    allowed_scopes: frozenset[GeometryScope]
    # Polygon minimum area (venue-local square meters / normalized units²).
    min_area: float | None = None
    # If True, camera-relative geometry must reference the same version.
    requires_camera_reference: bool = False

    def permits(self, geometry: GeometryModel) -> bool:
        return (
            geometry.geometry_type in self.allowed_geometry_types
            and geometry.coordinate_space in self.allowed_coordinate_spaces
            and geometry.geometry_scope in self.allowed_scopes
        )


# =============================================================================
# Spatial validation result types
# =============================================================================


class SpatialValidationIssue(BaseModel, frozen=True):
    """A single structured spatial validation finding."""

    model_config = {"extra": "forbid"}

    code: GeometryErrorCode
    severity: Literal["error", "warning"]
    message: str
    entity_type: str | None = None
    entity_id: str | None = None
    related_entity_id: str | None = None


class SpatialValidationResult(BaseModel, frozen=True):
    """Aggregate result of a spatial validation pass."""

    model_config = {"extra": "forbid"}

    valid: bool
    issues: list[SpatialValidationIssue] = Field(default_factory=list)
    geometry_count: int = 0
    checks_performed: int = 0

    @property
    def errors(self) -> list[SpatialValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[SpatialValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class SpatialValidationError(Exception):
    """Raised when geometry fails contract-level validation."""

    def __init__(self, message: str, code: GeometryErrorCode = GeometryErrorCode.GEOMETRY_INVALID):
        self.code = code
        super().__init__(message)


__all__ = [
    "CoordinateSpace",
    "EntityGeometryContract",
    "GeometryErrorCode",
    "GeometryModel",
    "GeometryScope",
    "GeometryType",
    "OverlapPolicy",
    "SpatialValidationError",
    "SpatialValidationIssue",
    "SpatialValidationResult",
]
