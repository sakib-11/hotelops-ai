"""Exception taxonomy for the deterministic geometry boundary (Task 14 Step 2).

Mirrors the project's provider-isolation convention (detectors,
``tracking``, ``sources``): downstream business logic depends only on
these types, never on raw ``ValueError``/math errors leaking from
predicate internals.

Semantics:

- ``GeometryError`` is the base for every geometry failure.  It is the
  direct analog of ``TrackingError`` in the Task 13 boundary.
- ``InvalidBoundingBoxError`` — a canonical ``BoundingBox`` failed the
  layer's boundary validation (non-finite values, out-of-range
  coordinates, inverted axes).  Malformed boxes are never silently
  repaired or clamped.
- ``InvalidCoordinateError`` — a standalone query point is non-finite
  or (for IMAGE_NORMALIZED space) lies outside the unit square.
- ``InvalidPolygonError`` — a polygon ring failed structural validation
  (too few coordinates, non-finite values, fewer than three distinct
  vertices, degenerate zero-area ring, or out-of-range coordinates for
  an IMAGE_NORMALIZED polygon).
- ``InvalidLineError`` — a LINESTRING failed structural validation
  (wrong geometry type, fewer than two vertices, non-finite
  coordinates, or out-of-range coordinates for an IMAGE_NORMALIZED
  line).

All failures are deterministic: identical input always produces the
same typed error.
"""

from __future__ import annotations


class GeometryError(Exception):
    """Base exception for all geometry-layer errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class InvalidBoundingBoxError(GeometryError):
    """The canonical bounding box is malformed (never repaired)."""


class InvalidCoordinateError(GeometryError):
    """A query point is non-finite or outside its coordinate-space bounds."""


class InvalidPolygonError(GeometryError):
    """A polygon ring is malformed or degenerate (never repaired)."""


class InvalidLineError(GeometryError):
    """A LINESTRING is malformed or degenerate (never repaired)."""


__all__ = [
    "GeometryError",
    "InvalidBoundingBoxError",
    "InvalidCoordinateError",
    "InvalidLineError",
    "InvalidPolygonError",
]
