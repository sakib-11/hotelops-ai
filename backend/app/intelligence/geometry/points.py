"""Canonical point extraction from the canonical bounding box (Task 14 Step 2).

Answers "what spatial point represents a TrackObservation?" at the
geometric level: a tracked object's bounding box is reduced to ONE
canonical point under an EXPLICIT policy.

  CENTROID  — x = (x_min + x_max) / 2, y = (y_min + y_max) / 2
  FOOTPOINT — x = (x_min + x_max) / 2, y = y_max (floor contact)

The bounding box is the canonical ``contracts.vision.BoundingBox``
(IMAGE_NORMALIZED [0, 1] x [0, 1]); the output is the canonical
``contracts.spatial.SpatialPointModel``.  No duplicate Point or
BoundingBox models exist — the centroid/footpoint arithmetic lives HERE
exactly once.

The layer validates the box at its own boundary before extracting:
the pydantic contract already rejects inverted/out-of-range boxes at
construction, but it permits non-finite values under default settings,
and the layer must never trust data that reached it from an
unvalidated path.  Malformed boxes raise ``InvalidBoundingBoxError``
and are never silently repaired or clamped.  Because a validated box
lies entirely in [0, 1], every extracted point is provably in [0, 1]
— the ``SpatialPointModel`` unit-square invariant cannot fail for
valid input.
"""

from __future__ import annotations

from math import isfinite

from backend.app.intelligence.geometry.exceptions import (
    InvalidBoundingBoxError,
    InvalidCoordinateError,
)
from contracts.geometry import CoordinateSpace
from contracts.spatial import SpatialPointModel, SpatialPointPolicy
from contracts.vision import BoundingBox


def validate_bounding_box(box: BoundingBox) -> None:
    """Validate a canonical bounding box at the geometry boundary.

    Rejects (deterministic ``InvalidBoundingBoxError``): non-finite
    values (NaN/infinity), coordinates outside [0, 1], and inverted
    axes (``x_max < x_min`` or ``y_max < y_min``).  The pydantic
    contract re-asserts the same rules at construction; this function
    is the defensive re-assertion for boxes reaching the layer from
    unvalidated paths.
    """
    fields = {
        "x_min": box.x_min,
        "y_min": box.y_min,
        "x_max": box.x_max,
        "y_max": box.y_max,
    }
    for name, value in fields.items():
        if not isinstance(value, (int, float)) or not isfinite(value):
            msg = f"bounding box {name} must be finite, got {value!r}"
            raise InvalidBoundingBoxError(msg)
        if not (0.0 <= value <= 1.0):
            msg = f"bounding box {name} must lie in [0, 1], got {value!r}"
            raise InvalidBoundingBoxError(msg)
    if box.x_max < box.x_min or box.y_max < box.y_min:
        raise InvalidBoundingBoxError("bounding box is inverted (x_max < x_min or y_max < y_min)")


def validate_coordinate(
    x: float,
    y: float,
    *,
    coordinate_space: CoordinateSpace = CoordinateSpace.IMAGE_NORMALIZED,
) -> None:
    """Validate a standalone query point under the canonical convention.

    IMAGE_NORMALIZED points are bounded to the unit square [0, 1] x
    [0, 1] (INV-GEO-03, identical to the canonical ``BoundingBox`` and
    ``SpatialPointModel`` rules); VENUE_LOCAL points are unbounded
    metric positions.  Non-finite values are always rejected.
    """
    for name, value in (("x", x), ("y", y)):
        if not isinstance(value, (int, float)) or not isfinite(value):
            msg = f"coordinate {name} must be finite, got {value!r}"
            raise InvalidCoordinateError(msg)
    if coordinate_space == CoordinateSpace.IMAGE_NORMALIZED and not (
        0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
    ):
        msg = f"IMAGE_NORMALIZED coordinates ({x}, {y}) must lie in [0, 1] x [0, 1]"
        raise InvalidCoordinateError(msg)


def extract_point(box: BoundingBox, policy: SpatialPointPolicy) -> SpatialPointModel:
    """Reduce a canonical bounding box to one canonical spatial point.

    The point is always IMAGE_NORMALIZED (the canonical camera-relative
    track space); the camera->venue transformation is the spatial
    engine's concern and is deliberately NOT part of this layer.
    """
    validate_bounding_box(box)

    x = (box.x_min + box.x_max) / 2.0
    if policy is SpatialPointPolicy.CENTROID:
        y = (box.y_min + box.y_max) / 2.0
    elif policy is SpatialPointPolicy.FOOTPOINT:
        y = box.y_max
    else:
        # Unreachable for the two canonical members; defensive branch so
        # a future policy can never silently fall through. A bare
        # ValueError is intentional here (not a GeometryError): an
        # unsupported policy is an API/typing contract violation, not a
        # geometry-input failure.
        msg = f"unsupported spatial point policy: {policy!r}"
        raise ValueError(msg)

    # Invariant: a validated box lies in [0, 1], so x/y are in [0, 1]
    # by construction and SpatialPointModel cannot reject them.
    return SpatialPointModel(
        x=x,
        y=y,
        coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
        policy=policy,
    )


__all__ = [
    "extract_point",
    "validate_bounding_box",
    "validate_coordinate",
]
