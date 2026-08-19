"""Deterministic geometry foundation (Task 14 Step 2).

A small, pure, deterministic geometry library — no I/O, no randomness,
no business logic.  It answers the four geometric questions the
Spatial Intelligence Engine needs before any interpretation logic:

  - ``extract_point``  — what spatial point represents a bounding box?
  - ``classify_point_in_polygon`` — is a point inside/outside/on a
    polygon ring?
  - ``validate_polygon`` / ``validate_polygon_ring`` — is a ring a
    valid polygon?
  - ``validate_coordinate`` / ``validate_bounding_box`` — is a point or
    box valid under the canonical coordinate convention?

Module layout (mirrors the sibling ``detectors``/``tracking``
packages):

  - ``exceptions`` — the typed error taxonomy (``GeometryError`` and
    subclasses); malformed geometry is never repaired or clamped.
  - ``points`` — canonical point extraction (CENTROID/FOOTPOINT) from
    ``contracts.vision.BoundingBox`` into
    ``contracts.spatial.SpatialPointModel``, plus coordinate and
    bounding-box validation.
  - ``polygon`` — the single tolerance policy (``GEOMETRY_TOLERANCE``),
    tri-state point-in-polygon (INSIDE/OUTSIDE/BOUNDARY), and polygon
    validation over the canonical ``contracts.geometry.GeometryModel``.
  - ``segments`` — line-segment primitives (Task 14 Step 4): signed
    side, LEFT/RIGHT/ON_LINE classification, and LINESTRING validation
    over the canonical ``contracts.geometry.GeometryModel``.

Canonical contracts are consumed, never duplicated: BoundingBox
(contracts.vision), GeometryModel (contracts.geometry), SpatialPointModel/
SpatialPointPolicy (contracts.spatial).  No hotel business concepts
exist in this package.
"""

from backend.app.intelligence.geometry.exceptions import (
    GeometryError,
    InvalidBoundingBoxError,
    InvalidCoordinateError,
    InvalidLineError,
    InvalidPolygonError,
)
from backend.app.intelligence.geometry.points import (
    extract_point,
    validate_bounding_box,
    validate_coordinate,
)
from backend.app.intelligence.geometry.polygon import (
    GEOMETRY_TOLERANCE,
    Point2D,
    PointLocation,
    classify_point_in_polygon,
    validate_polygon,
    validate_polygon_ring,
)
from backend.app.intelligence.geometry.segments import (
    LineSide,
    distance_point_to_segment,
    side_of_line,
    signed_side,
    validate_linestring,
)

__all__ = [
    "GEOMETRY_TOLERANCE",
    "GeometryError",
    "InvalidBoundingBoxError",
    "InvalidCoordinateError",
    "InvalidLineError",
    "InvalidPolygonError",
    "LineSide",
    "Point2D",
    "PointLocation",
    "classify_point_in_polygon",
    "distance_point_to_segment",
    "extract_point",
    "side_of_line",
    "signed_side",
    "validate_bounding_box",
    "validate_coordinate",
    "validate_linestring",
    "validate_polygon",
    "validate_polygon_ring",
]
