"""Normalized detection output (Task 12, Phase 7).

The single source of truth for the project's bounding-box coordinate
convention and its explicit geometry validation:

    x1, y1  = top-left corner
    x2, y2  = bottom-right corner
    0 <= x1 <= x2 <= 1
    0 <= y1 <= y2 <= 1

The Task 4 ``BoundingBox`` contract stores exactly these values under
``x_min / y_min / x_max / y_max``.  Normalization divides pixel-space
coordinates by the image dimensions the model actually saw.

Malformed model output is NEVER silently hidden.  ``normalize_xyxy``
and ``validate_bounding_box`` raise ``InvalidGeometryError`` for:

- coordinates outside [0, 1] beyond a tiny float32 boundary tolerance
  (e.g. a box half outside the frame);
- inverted corners (``x2 < x1`` or ``y2 < y1``);
- zero-size boxes (``x2 == x1`` or ``y2 == y1`` — no spatial area);
- invalid image dimensions.

The tolerance exists only to snap float32 boundary noise (e.g.
``640.0px / 640px``) to the exact edge — it is not a license to hide
malformed output.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.app.intelligence.detectors.exceptions import InvalidGeometryError
from contracts.vision import BoundingBox, DetectionObservation

__all__ = [
    "NORMALIZATION_EPSILON",
    "normalize_xyxy",
    "validate_bounding_box",
    "validate_detections_geometry",
]

#: Float32 boundary tolerance: coordinates within this distance of the
#: [0, 1] range are treated as boundary noise and snapped to the edge;
#: anything beyond is malformed model output and rejected.
NORMALIZATION_EPSILON = 1e-6


def _normalized_coord(value: float) -> float:
    """Validate one normalized coordinate; snap boundary noise to [0, 1]."""
    if -NORMALIZATION_EPSILON <= value <= 1.0 + NORMALIZATION_EPSILON:
        return min(1.0, max(0.0, value))
    msg = f"normalized coordinate {value!r} is outside [0, 1]"
    raise InvalidGeometryError(msg)


def _require_positive_area(nx1: float, ny1: float, nx2: float, ny2: float) -> None:
    if nx2 <= nx1 or ny2 <= ny1:
        msg = (
            f"invalid geometry: box has zero size or inverted corners "
            f"({nx1:.6f}, {ny1:.6f}, {nx2:.6f}, {ny2:.6f})"
        )
        raise InvalidGeometryError(msg)


def normalize_xyxy(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    width: int,
    height: int,
) -> BoundingBox:
    """Normalize a pixel-space ``(x1, y1, x2, y2)`` box to the contract.

    Args:
        x1, y1, x2, y2: pixel-space top-left / bottom-right corners as
            emitted by the model.
        width, height: image dimensions the box is relative to.

    Returns:
        A normalized ``BoundingBox`` satisfying ``0 <= x1 <= x2 <= 1``
        and ``0 <= y1 <= y2 <= 1`` with positive area.

    Raises:
        InvalidGeometryError: the image dimensions are invalid, a
            normalized coordinate is out of range, the box is inverted,
            or the box has zero size.  Malformed output is never hidden.
    """
    if width < 1 or height < 1:
        msg = f"image dimensions must be >= 1, got {width}x{height}"
        raise InvalidGeometryError(msg)
    nx1 = _normalized_coord(x1 / width)
    ny1 = _normalized_coord(y1 / height)
    nx2 = _normalized_coord(x2 / width)
    ny2 = _normalized_coord(y2 / height)
    _require_positive_area(nx1, ny1, nx2, ny2)
    return BoundingBox(x_min=nx1, y_min=ny1, x_max=nx2, y_max=ny2)


def validate_bounding_box(box: BoundingBox) -> None:
    """Explicitly verify a normalized bounding box.

    Enforces the project convention: coordinates within [0, 1] (the
    contract guarantees this at construction), correct corner ordering,
    and positive area — a zero-size box that the contract would
    otherwise tolerate is flagged here.
    """
    nx1 = _normalized_coord(box.x_min)
    ny1 = _normalized_coord(box.y_min)
    nx2 = _normalized_coord(box.x_max)
    ny2 = _normalized_coord(box.y_max)
    _require_positive_area(nx1, ny1, nx2, ny2)


def validate_detections_geometry(detections: Sequence[DetectionObservation]) -> None:
    """Verify the normalized geometry of every detection.

    A no-op for an empty sequence; raises ``InvalidGeometryError``
    identifying the offending detection on the first malformed box.
    """
    for detection in detections:
        try:
            validate_bounding_box(detection.bounding_box)
        except InvalidGeometryError as exc:
            msg = f"detection {detection.detection_id} has invalid geometry"
            raise InvalidGeometryError(msg, cause=exc) from exc
