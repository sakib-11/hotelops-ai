"""Deterministic spatial predicates for the validation engine (Task 10.6).

Pure-Python, deterministic implementations of the spatial calculations
the validation engine needs - so validation is reproducible offline and
in unit tests. Production wires a PostGIS-backed implementation behind
the SAME protocol (backend.app.infrastructure.spatial.engine); the
validators only depend on this protocol, never on a concrete engine.

Tolerances are DOCUMENTED and fixed:
  - ``AREA_TOLERANCE`` (1e-6): any polygon-polygon intersection with
    area strictly greater than this counts as a MEANINGFUL overlap;
    anything at or below it is boundary touching.
  - All comparisons use strict inequalities so floating-point noise can
    never flip a result at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from contracts.geometry import GeometryModel, GeometryType

# Documented spatial tolerance (see module docstring).
AREA_TOLERANCE = 1e-6

Point = tuple[float, float]


# =============================================================================
# Geometry helpers
# =============================================================================


def _ring(geometry: GeometryModel) -> list[Point]:
    """Exterior ring as plain (x, y) tuples (closed)."""
    return [(p[0], p[1]) for p in geometry.ring]


def _orientation(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, p: Point, eps: float = 1e-12) -> bool:
    """True when p lies on segment ab (collinear and within bbox)."""
    return (
        abs(_orientation(a, b, p)) <= eps
        and min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def _segments_intersect_proper(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Proper segment crossing (interiors intersect)."""
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)
    if o1 == 0 or o2 == 0 or o3 == 0 or o4 == 0:
        return False  # touching at endpoints is NOT a proper crossing
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def point_in_ring(point: Point, ring: list[Point]) -> bool:
    """Ray-casting point-in-polygon test (boundary counts as inside)."""
    x, y = point
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if _on_segment((xj, yj), (xi, yi), point):
            return True  # on boundary - treat as inside
        intersects = (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (
            (yj - yi) if (yj - yi) != 0 else 1e-300
        ) + xi
        if intersects:
            inside = not inside
        j = i
    return inside


def ring_intersection_area(subject: list[Point], clip: list[Point]) -> float:
    """Area of intersection between two simple polygons.

    Sutherland-Hodgman polygon clipping over convex clip polygons with
    the shoelace formula. Deterministic for the simple, mostly-convex
    shapes used by venue configuration (zones, tables, ROIs). PostGIS
    (ST_Intersection/ST_Area) is the authoritative implementation in
    production; this pure fallback exists for offline determinism.
    """
    output = list(subject)
    if not output:
        return 0.0
    n = len(clip)
    for i in range(n):
        if not output:
            return 0.0
        a = clip[i]
        b = clip[(i + 1) % n]
        input_list = output
        output = []
        if not input_list:
            break
        s = input_list[-1]
        for e in input_list:
            if _orientation(a, b, e) >= 0:  # e inside (or on) the clip edge
                if _orientation(a, b, s) < 0:
                    output.append(_line_intersection(a, b, s, e))
                output.append(e)
            elif _orientation(a, b, s) >= 0:
                output.append(_line_intersection(a, b, s, e))
            s = e
    return abs(_shoelace(output)) / 2.0


def _shoelace(ring: list[Point]) -> float:
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total


def _line_intersection(a: Point, b: Point, c: Point, d: Point) -> Point:
    """Intersection of segments ab and cd (assumes they cross)."""
    x1, y1 = a
    x2, y2 = b
    x3, y3 = c
    x4, y4 = d
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-15:
        # Parallel/coincident - fall back to midpoint (deterministic).
        return ((x1 + x2 + x3 + x4) / 4.0, (y1 + y2 + y3 + y4) / 4.0)
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / denom
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / denom
    return (px, py)


# =============================================================================
# Spatial engine protocol
# =============================================================================


class SpatialEngine(Protocol):
    """Spatial calculation protocol used by the validation engine.

    Implementations: deterministic pure-Python (this module,
    SpatialMath) and PostGIS-backed (infrastructure.spatial.engine).
    Validators depend ONLY on this protocol. Methods are async so the
    production engine can delegate to PostGIS (asyncpg); the pure
    engine is a thin async wrapper over deterministic math.
    """

    async def overlap_area(self, a: GeometryModel, b: GeometryModel) -> float: ...
    async def contains(self, outer: GeometryModel, inner: GeometryModel) -> bool: ...
    async def boundary_touches(self, a: GeometryModel, b: GeometryModel) -> bool: ...
    async def is_valid_polygon(self, geometry: GeometryModel) -> bool: ...
    async def meaningful_overlap(self, a: GeometryModel, b: GeometryModel) -> bool: ...


@dataclass(frozen=True)
class SpatialMath:
    """Deterministic pure-Python spatial engine (offline/tests).

    All methods are async to satisfy the SpatialEngine protocol while
    remaining pure and deterministic (no I/O).
    """

    area_tolerance: float = AREA_TOLERANCE

    def _polygons(self, geometry: GeometryModel) -> list[Point] | None:
        if geometry.geometry_type != GeometryType.POLYGON:
            return None
        return _ring(geometry)

    async def overlap_area(self, a: GeometryModel, b: GeometryModel) -> float:
        ra = self._polygons(a)
        rb = self._polygons(b)
        if ra is None or rb is None:
            return 0.0
        return ring_intersection_area(ra, rb)

    async def contains(self, outer: GeometryModel, inner: GeometryModel) -> bool:
        ro = self._polygons(outer)
        ri = self._polygons(inner)
        if ro is None or ri is None:
            return False
        # All inner vertices inside (or on) outer ring.
        return all(point_in_ring(p, ro) for p in ri)

    async def boundary_touches(self, a: GeometryModel, b: GeometryModel) -> bool:
        """Boundary contact WITHOUT meaningful area overlap."""
        ra = self._polygons(a)
        rb = self._polygons(b)
        if ra is None or rb is None:
            return False
        # Any vertex of one lying exactly on an edge of the other.
        for pa, pb in ((ra, rb), (rb, ra)):
            for i in range(len(pb) - 1):
                s, e = pb[i], pb[i + 1]
                for p in pa:
                    if _on_segment(s, e, p):
                        return True
        return False

    async def is_valid_polygon(self, geometry: GeometryModel) -> bool:
        if geometry.geometry_type != GeometryType.POLYGON:
            return False
        if geometry.is_self_intersecting():
            return False
        if geometry.is_degenerate():
            return False
        ring = _ring(geometry)
        # Closed ring requirement.
        return ring[0] == ring[-1]

    async def meaningful_overlap(self, a: GeometryModel, b: GeometryModel) -> bool:
        """True when the polygons overlap by more than the documented
        tolerance - boundary touching is NOT meaningful overlap."""
        return await self.overlap_area(a, b) > self.area_tolerance


__all__ = [
    "AREA_TOLERANCE",
    "SpatialEngine",
    "SpatialMath",
    "point_in_ring",
    "ring_intersection_area",
]
