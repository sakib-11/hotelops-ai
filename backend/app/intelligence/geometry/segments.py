"""Deterministic line-segment primitives (Task 14 Step 4).

Pure, side-effect-free geometry helpers for the line-crossing engine:

  - ``signed_side``  — the signed cross-product of a point relative to a
    directed segment (``a → b``). Positive = left, negative = right.
  - ``side_of_line`` — tri-state classification LEFT / RIGHT / ON_LINE
    using the single documented tolerance policy
    (``GEOMETRY_TOLERANCE``).
  - ``distance_point_to_segment`` — Euclidean distance from a point to
    a segment (extent-aware; the single shared implementation used by
    both this module and the polygon layer).
  - ``validate_linestring`` — validate a canonical
    ``contracts.geometry.GeometryModel`` LINESTRING and return its
    vertices as ``(x, y)`` tuples.

No I/O, no randomness, no mutable global state, and no hotel business
logic: the same inputs always produce the same classification.  The
crossing POLICY (boundary cases, direction semantics) lives in the
spatial transition engine — this module provides the pure predicates it
composes, so the segment math exists exactly once.

Tolerance: only ``GEOMETRY_TOLERANCE`` (defined in ``polygon.py``) is
used — no ad-hoc thresholds.  A side magnitude at or below the
tolerance is ``ON_LINE``, a distinct state that is never silently
folded into LEFT or RIGHT.

Degenerate segments: a zero-length segment (``a == b``) classifies every
point ``ON_LINE`` and therefore never contributes a crossing — this is
deterministic and documented, never repaired.
"""

from __future__ import annotations

from enum import StrEnum
from math import hypot, isfinite

from backend.app.intelligence.geometry.exceptions import InvalidLineError
from backend.app.intelligence.geometry.primitives import (
    GEOMETRY_TOLERANCE,
    Point2D,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryType

__all__ = [
    "LineSide",
    "distance_point_to_segment",
    "side_of_line",
    "signed_side",
    "validate_linestring",
]


class LineSide(StrEnum):
    """Tri-state classification of a point relative to a directed segment."""

    LEFT = "left"
    RIGHT = "right"
    ON_LINE = "on_line"


def distance_point_to_segment(point: Point2D, a: Point2D, b: Point2D) -> float:
    """Euclidean distance from ``point`` to the segment ``a``-``b``.

    Extent-aware: a point beyond either endpoint measures distance to
    that endpoint, so a point near the supporting line's EXTENSION (but
    far from the segment itself) is NOT ``ON_LINE`` — only the segment's
    own extent can classify it.  This is the single shared
    implementation used by both this module and ``polygon.py`` (Step 2
    boundary classification and Step 4 endpoint-on-line checks), so the
    segment math exists exactly once.
    """
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    # A segment at or below the boundary tolerance is effectively a
    # point (duplicate/near-duplicate vertices) — distance to ``a``
    # avoids a division-by-zero class and stays on the single tolerance.
    if length_sq <= GEOMETRY_TOLERANCE**2:
        return hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return hypot(px - (ax + t * dx), py - (ay + t * dy))


def signed_side(a: Point2D, b: Point2D, p: Point2D) -> float:
    """Signed side of ``p`` relative to the directed segment ``a → b``.

    Cross product ``(b - a) x (p - a)``.  Positive = left of the
    directed segment, negative = right, ~0 = collinear.  Deterministic
    and scale-invariant (no normalization is applied).
    """
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def side_of_line(
    a: Point2D,
    b: Point2D,
    p: Point2D,
    *,
    tolerance: float = GEOMETRY_TOLERANCE,
) -> LineSide:
    """Tri-state side classification of ``p`` vs the directed segment a→b.

    Side magnitudes at or below ``tolerance`` classify as ``ON_LINE`` —
    a distinct outcome, never silently converted to LEFT or RIGHT, so
    floating-point noise can never flip a result.
    """
    side = signed_side(a, b, p)
    if side > tolerance:
        return LineSide.LEFT
    if side < -tolerance:
        return LineSide.RIGHT
    return LineSide.ON_LINE


def validate_linestring(geometry: GeometryModel) -> list[Point2D]:
    """Validate a canonical ``GeometryModel`` and return its vertices.

    Returns the vertices as ``(x, y)`` tuples in stored order, ready for
    repeated ``side_of_line`` calls.  Rejects non-LINESTRING geometry,
    fewer than two vertices, non-finite coordinates, and
    IMAGE_NORMALIZED vertices outside the unit square (INV-GEO-03
    re-asserted at this boundary).  The input is never repaired or
    reordered.
    """
    if geometry.geometry_type != GeometryType.LINESTRING:
        msg = (
            f"validate_linestring requires LINESTRING geometry, got {geometry.geometry_type.value}"
        )
        raise InvalidLineError(msg)

    vertices = [(float(p[0]), float(p[1])) for p in geometry.coordinates]
    if len(vertices) < 2:
        msg = f"LINESTRING requires at least 2 vertices, got {len(vertices)}"
        raise InvalidLineError(msg)
    for index, (x, y) in enumerate(vertices):
        if not isfinite(x) or not isfinite(y):
            msg = f"LINESTRING vertex {index} contains a non-finite coordinate"
            raise InvalidLineError(msg)

    if geometry.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED:
        for x, y in vertices:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                msg = f"IMAGE_NORMALIZED vertex ({x}, {y}) must lie in [0, 1]"
                raise InvalidLineError(msg)
    return vertices
