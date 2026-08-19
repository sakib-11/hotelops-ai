"""Deterministic pure-Python polygon predicates (Task 14 Step 2).

A small, side-effect-free geometry library that answers exactly three
questions:

  1. Is a point inside a polygon ring?   -> PointLocation.INSIDE
  2. Is a point outside a polygon ring?  -> PointLocation.OUTSIDE
  3. Is a point ON a polygon boundary?   -> PointLocation.BOUNDARY

No I/O, no randomness, no mutable global state, and no hotel business
logic: the same inputs always produce the same classification
(determinism is pinned by tests).  Malformed input is rejected with the
typed ``InvalidPolygonError``/``InvalidCoordinateError`` — never
silently repaired, clamped, or reordered.

Coordinate spaces: the predicates are space-agnostic and operate on raw
rings; bounds enforcement for IMAGE_NORMALIZED points/polygons happens
at the canonical contract boundary (``contracts.geometry`` INV-GEO-03,
``contracts.spatial.SpatialPointModel``) and at the layer's own
``points.validate_coordinate``.  Polygons are consumed from the
canonical ``contracts.geometry.GeometryModel`` (``validate_polygon``) —
no Polygon model exists here or anywhere else.

Ring convention: a polygon ring is IMPLICITLY CLOSED — the closing edge
(last -> first) is always part of the polygon.  This is the canonical
``contracts.geometry`` POLYGON convention (closed rings, canonicalized
at construction); the layer never mutates the input ring.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from math import hypot, isfinite

from backend.app.intelligence.geometry.exceptions import (
    InvalidCoordinateError,
    InvalidPolygonError,
)
from backend.app.intelligence.geometry.primitives import (
    GEOMETRY_TOLERANCE,
    Point2D,
)
from backend.app.intelligence.geometry.segments import distance_point_to_segment
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryType

# The single tolerance policy (``GEOMETRY_TOLERANCE``) and the canonical
# ``Point2D`` type are shared with the ``segments`` layer and live in
# ``primitives`` (this module re-exports them for its public surface).


class PointLocation(StrEnum):
    """Deterministic point/polygon classification outcome."""

    INSIDE = "inside"
    OUTSIDE = "outside"
    BOUNDARY = "boundary"


# =============================================================================
# Polygon ring validation
# =============================================================================


def _require_finite(value: float, label: str) -> float:
    """Return ``value`` as float or raise a structural polygon error."""
    if not isinstance(value, (int, float)) or not isfinite(value):
        raise InvalidPolygonError(
            f"polygon ring contains a non-finite coordinate {label}={value!r}"
        )
    return float(value)


def validate_polygon_ring(
    ring: Sequence[Point2D],
    *,
    tolerance: float = GEOMETRY_TOLERANCE,
) -> None:
    """Validate a polygon ring without mutating or repairing it.

    Rejects (deterministic ``InvalidPolygonError``):

    - fewer than 4 coordinates (the canonical closed-ring minimum:
      a triangle contributes 3 corners plus its closing vertex);
    - non-finite coordinates (NaN/infinity);
    - fewer than 3 distinct vertices (measured with ``tolerance``);
    - zero-area rings (collinear/self-canceling) at or below tolerance.

    The ring is treated as implicitly closed (canonical POLYGON
    convention); no closing vertex is appended to the input.
    """
    if len(ring) < 4:
        msg = f"polygon ring requires at least 4 coordinates (closed ring), got {len(ring)}"
        raise InvalidPolygonError(msg)

    points: list[Point2D] = []
    for index, point in enumerate(ring):
        if len(point) < 2:
            msg = f"polygon ring coordinate {index} must have at least x, y"
            raise InvalidPolygonError(msg)
        points.append((
            _require_finite(point[0], f"[{index}].x"),
            _require_finite(point[1], f"[{index}].y"),
        ))

    distinct: list[Point2D] = []
    for point in points:
        if all(hypot(point[0] - u, point[1] - v) > tolerance for u, v in distinct):
            distinct.append(point)
    if len(distinct) < 3:
        msg = f"degenerate polygon: fewer than 3 distinct vertices (got {len(distinct)})"
        raise InvalidPolygonError(msg)

    if abs(_ring_area(points)) <= tolerance:
        raise InvalidPolygonError("degenerate polygon: zero-area ring (collinear/self-canceling)")


def validate_polygon(geometry: GeometryModel) -> list[Point2D]:
    """Validate a canonical ``GeometryModel`` and return its ring.

    Returns the canonical ring (closed, precision-rounded per
    ``contracts.geometry``) as ``(x, y)`` tuples, ready for repeated
    ``classify_point_in_polygon`` calls.  Rejects non-POLYGON geometry,
    structurally invalid rings, and IMAGE_NORMALIZED rings with
    coordinates outside the unit square (INV-GEO-03 re-asserted at this
    boundary).
    """
    if geometry.geometry_type != GeometryType.POLYGON:
        msg = f"validate_polygon requires POLYGON geometry, got {geometry.geometry_type.value}"
        raise InvalidPolygonError(msg)

    ring = [(float(p[0]), float(p[1])) for p in geometry.ring]
    validate_polygon_ring(ring)

    if geometry.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED:
        for x, y in ring:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                msg = f"IMAGE_NORMALIZED polygon coordinate ({x}, {y}) must lie in [0, 1]"
                raise InvalidPolygonError(msg)
    return ring


def _ring_area(points: Sequence[Point2D]) -> float:
    """Signed shoelace area over the implicitly closed ring."""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


# =============================================================================
# Point-in-polygon classification
# =============================================================================


def classify_point_in_polygon(
    point: Point2D,
    ring: Sequence[Point2D],
    *,
    tolerance: float = GEOMETRY_TOLERANCE,
) -> PointLocation:
    """Classify ``point`` against an implicitly closed polygon ``ring``.

    Order of evaluation (deterministic):

    1. Boundary: the point lies within ``tolerance`` of any edge
       (including vertices) -> ``BOUNDARY``.  Distance-to-segment is
       scale-invariant, unlike raw cross-product checks.
    2. Interior/exterior: half-open ray casting (the same vertex-safe
       rule the Task 10.6 validation engine uses) -> ``INSIDE`` or
       ``OUTSIDE``.

    Malformed rings and non-finite query points raise the typed errors;
    "on the boundary" is a distinct outcome, never folded into INSIDE.
    """
    if len(point) < 2 or not isfinite(point[0]) or not isfinite(point[1]):
        msg = f"query point {point!r} must be finite with x, y coordinates"
        raise InvalidCoordinateError(msg)
    validate_polygon_ring(ring, tolerance=tolerance)

    query: Point2D = (float(point[0]), float(point[1]))
    points = [(float(p[0]), float(p[1])) for p in ring]
    n = len(points)

    for i in range(n):
        if distance_point_to_segment(query, points[i], points[(i + 1) % n]) <= tolerance:
            return PointLocation.BOUNDARY

    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        # Half-open rule: only edges straddling the query row cast rays,
        # so rays through vertices are handled consistently.
        if (yi > query[1]) != (yj > query[1]):
            x_intersect = (xj - xi) * (query[1] - yi) / (yj - yi) + xi
            if query[0] < x_intersect:
                inside = not inside
        j = i

    return PointLocation.INSIDE if inside else PointLocation.OUTSIDE


__all__ = [
    "GEOMETRY_TOLERANCE",
    "Point2D",
    "PointLocation",
    "classify_point_in_polygon",
    "validate_polygon",
    "validate_polygon_ring",
]
