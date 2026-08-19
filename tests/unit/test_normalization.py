"""Tests for normalized detection output (Task 12, Phase 7).

Covers the project's bounding-box coordinate convention and its
explicit geometry handling:

- ``normalize_xyxy`` — pixel-space ``(x1, y1, x2, y2)`` -> normalized
  ``BoundingBox`` in ``x_min/y_min/x_max/y_max`` (top-left/bottom-right);
- ``validate_bounding_box`` / ``validate_detections_geometry`` — the
  explicit validation layer that rejects malformed geometry;
- the invariant ``0 <= x1 <= x2 <= 1`` and ``0 <= y1 <= y2 <= 1`` with
  positive area.

Malformed model output is NEVER silently hidden: out-of-range
coordinates, inverted corners and zero-size boxes raise
``InvalidGeometryError``.  Float32 boundary noise (within
``NORMALIZATION_EPSILON`` of [0, 1]) is snapped to the exact edge.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.app.intelligence.detectors import (
    Device,
    InvalidGeometryError,
    ModelSpec,
    normalize_xyxy,
    validate_bounding_box,
    validate_detections_geometry,
)
from contracts.common import DetectionId, FrameId, VideoSessionId, new_uuid
from contracts.vision import BoundingBox, DetectionObservation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_spec() -> ModelSpec:
    return ModelSpec(
        model_id="yolov8n",
        model_name="yolov8n",
        model_version="8.1.0",
        artifact_uri="memory://yolov8n.pt",
        artifact_sha256="a" * 64,
        device=Device.CPU,
        class_names=("person", "bag"),
    )


def make_detection(box: BoundingBox) -> DetectionObservation:
    return DetectionObservation(
        detection_id=DetectionId(new_uuid()),
        frame_id=FrameId(new_uuid()),
        session_id=VideoSessionId(new_uuid()),
        class_name="person",
        confidence=0.95,
        bounding_box=box,
        event_time=datetime.now(UTC),
        image_width=640,
        image_height=480,
    )


def assert_normalized(box: BoundingBox, *, x1: float, y1: float, x2: float, y2: float) -> None:
    assert box.x_min == pytest.approx(x1)
    assert box.y_min == pytest.approx(y1)
    assert box.x_max == pytest.approx(x2)
    assert box.y_max == pytest.approx(y2)


# ---------------------------------------------------------------------------
# Coordinate convention: pixel -> normalized (x1, y1, x2, y2)
# ---------------------------------------------------------------------------


class TestNormalizeXyxy:
    def test_normal_box_relative_to_image_dimensions(self) -> None:
        # 640x480 frame; box (160, 120) -> (480, 360).
        box = normalize_xyxy(160, 120, 480, 360, width=640, height=480)
        assert_normalized(box, x1=0.25, y1=0.25, x2=0.75, y2=0.75)

    def test_convention_is_top_left_bottom_right(self) -> None:
        # x1,y1 = top-left; x2,y2 = bottom-right (project convention).
        box = normalize_xyxy(10, 20, 30, 40, width=100, height=100)
        assert box.x_min == pytest.approx(0.1)
        assert box.y_min == pytest.approx(0.2)
        assert box.x_max == pytest.approx(0.3)
        assert box.y_max == pytest.approx(0.4)

    def test_boundary_box_at_exact_edges(self) -> None:
        # A box spanning the whole frame normalizes to exactly [0, 1].
        box = normalize_xyxy(0, 0, 640, 480, width=640, height=480)
        assert box.x_min == pytest.approx(0.0, abs=1e-12)
        assert box.y_min == pytest.approx(0.0, abs=1e-12)
        assert box.x_max == pytest.approx(1.0, abs=1e-12)
        assert box.y_max == pytest.approx(1.0, abs=1e-12)

    def test_boundary_epsilon_noise_is_snapped_not_hidden(self) -> None:
        # Float32 division noise (e.g. 640.0/640 -> 1.0000001) snaps to 1.0.
        box = normalize_xyxy(0, 0, 640.000001, 480.000001, width=640, height=480)
        assert box.x_max == pytest.approx(1.0, abs=1e-12)
        assert box.y_max == pytest.approx(1.0, abs=1e-12)

    def test_pixel_perfect_division_is_exact(self) -> None:
        box = normalize_xyxy(320, 240, 640, 480, width=640, height=480)
        assert_normalized(box, x1=0.5, y1=0.5, x2=1.0, y2=1.0)

    def test_single_pixel_box_keeps_positive_area(self) -> None:
        # A one-pixel box is a legitimate minimal detection (nonzero area).
        box = normalize_xyxy(0, 0, 1, 1, width=640, height=480)
        assert box.x_max > box.x_min
        assert box.y_max > box.y_min

    def test_portrait_resolution_uses_actual_dimensions(self) -> None:
        # 480x640 portrait frame: x normalizes by width=480, y by height=640.
        box = normalize_xyxy(0, 0, 480, 640, width=480, height=640)
        assert_normalized(box, x1=0.0, y1=0.0, x2=1.0, y2=1.0)
        half = normalize_xyxy(240, 320, 480, 640, width=480, height=640)
        assert_normalized(half, x1=0.5, y1=0.5, x2=1.0, y2=1.0)

    def test_small_frame_uses_actual_dimensions(self) -> None:
        # A 32x24 frame: pixel/actual-dimension conversion — the convention
        # must never assume a fixed resolution (e.g. 1920x1080).
        box = normalize_xyxy(8, 6, 24, 18, width=32, height=24)
        assert_normalized(box, x1=0.25, y1=0.25, x2=0.75, y2=0.75)


class TestNormalizeInvalidInput:
    def test_invalid_image_dimensions_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError, match="image dimensions"):
            normalize_xyxy(0, 0, 10, 10, width=0, height=480)
        with pytest.raises(InvalidGeometryError, match="image dimensions"):
            normalize_xyxy(0, 0, 10, 10, width=640, height=0)

    def test_out_of_range_box_rejected(self) -> None:
        # Box extending beyond the frame (e.g. 10000px on a 640px frame).
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(-50, -50, 10000, 10000, width=640, height=480)

    def test_slightly_out_of_range_beyond_epsilon_rejected(self) -> None:
        # Beyond float32 boundary tolerance is malformed output, not noise.
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(0, 0, 641.0, 480, width=640, height=480)

    def test_inverted_corners_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError, match="inverted"):
            normalize_xyxy(30, 40, 10, 20, width=100, height=100)

    def test_zero_size_box_rejected(self) -> None:
        # x2 == x1 -> zero width; no spatial area.
        with pytest.raises(InvalidGeometryError, match="zero size"):
            normalize_xyxy(100, 100, 100, 200, width=640, height=480)
        # y2 == y1 -> zero height.
        with pytest.raises(InvalidGeometryError, match="zero size"):
            normalize_xyxy(100, 100, 200, 100, width=640, height=480)

    def test_nan_coordinates_rejected(self) -> None:
        # NaN never compares within [0, 1] — malformed output, not noise.
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(float("nan"), 0, 10, 10, width=640, height=480)
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(0, 0, float("nan"), 10, width=640, height=480)
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(0, float("nan"), 10, 10, width=640, height=480)

    def test_infinite_coordinates_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(0, 0, float("inf"), 10, width=640, height=480)
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(float("-inf"), 0, 10, 10, width=640, height=480)
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            normalize_xyxy(0, 0, 10, float("inf"), width=640, height=480)


# ---------------------------------------------------------------------------
# validate_bounding_box — explicit verification of a normalized box
# ---------------------------------------------------------------------------


class TestValidateBoundingBox:
    def test_valid_box_passes(self) -> None:
        validate_bounding_box(BoundingBox(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9))

    def test_boundary_box_passes(self) -> None:
        validate_bounding_box(BoundingBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0))

    def test_zero_size_box_rejected(self) -> None:
        # The contract tolerates x_max == x_min; the geometry layer does not.
        with pytest.raises(InvalidGeometryError, match="zero size"):
            validate_bounding_box(BoundingBox(x_min=0.1, y_min=0.1, x_max=0.1, y_max=0.9))

    def test_inverted_corners_rejected(self) -> None:
        # The BoundingBox contract rejects inversion at construction, so
        # build the malformed box bypassing contract validation to prove
        # the geometry layer still enforces the convention explicitly.
        inverted = BoundingBox.model_construct(x_min=0.5, y_min=0.5, x_max=0.1, y_max=0.9)
        with pytest.raises(InvalidGeometryError, match="inverted"):
            validate_bounding_box(inverted)


# ---------------------------------------------------------------------------
# validate_detections_geometry — explicit verification across detections
# ---------------------------------------------------------------------------


class TestValidateDetectionsGeometry:
    def test_empty_detections_pass(self) -> None:
        # Empty is a valid, successful result — not malformed geometry.
        validate_detections_geometry([])

    def test_multiple_valid_detections_pass(self) -> None:
        detections = [
            make_detection(BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5)),
            make_detection(BoundingBox(x_min=0.2, y_min=0.2, x_max=0.9, y_max=0.9)),
            make_detection(BoundingBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)),
        ]
        validate_detections_geometry(detections)

    def test_single_malformed_detection_flagged_with_id(self) -> None:
        bad = make_detection(BoundingBox(x_min=0.1, y_min=0.1, x_max=0.1, y_max=0.5))
        detections = [
            make_detection(BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5)),
            bad,
        ]
        with pytest.raises(InvalidGeometryError) as excinfo:
            validate_detections_geometry(detections)
        assert str(bad.detection_id) in str(excinfo.value.message)

    def test_malformed_detection_raises_typed_error(self) -> None:
        # Contract validation forbids out-of-range boxes at construction;
        # bypass it to prove the geometry layer rejects them explicitly.
        out_of_range = BoundingBox.model_construct(x_min=0.0, y_min=0.0, x_max=2.0, y_max=1.0)
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            validate_bounding_box(out_of_range)


# ---------------------------------------------------------------------------
# Provenance + normalization integration
# ---------------------------------------------------------------------------


class TestProvenancePreserved:
    def test_normalize_returns_contract_with_preserved_values(self) -> None:
        # Normalization produces a contract value; provenance fields are
        # carried by the observation, not the box.
        box = normalize_xyxy(10, 20, 330, 470, width=640, height=480)
        det = make_detection(box)
        assert det.bounding_box is box
        assert det.image_width == 640
        assert det.image_height == 480
        assert det.frame_id is not None
        assert det.session_id is not None

    def test_event_timestamp_preserved(self) -> None:
        when = datetime.now(UTC)
        det = make_detection(BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5))
        # Replace event_time to prove it is carried through untouched.
        det = det.model_copy(update={"event_time": when})
        assert det.event_time == when
        assert det.event_time.tzinfo is UTC

    def test_normalized_values_satisfy_project_invariant(self) -> None:
        box = normalize_xyxy(0, 0, 640, 480, width=640, height=480)
        assert 0.0 <= box.x_min <= box.x_max <= 1.0
        assert 0.0 <= box.y_min <= box.y_max <= 1.0
        assert box.x_max > box.x_min  # positive area
        assert box.y_max > box.y_min
        # Exact boundary values are preserved, not lost to rounding.
        assert box.x_min == pytest.approx(0.0, abs=1e-12)
        assert box.x_max == pytest.approx(1.0, abs=1e-12)

    def test_normalized_box_convention_satisfies_invariant(self) -> None:
        import random

        for _ in range(200):
            x1 = random.uniform(0, 0.9)
            y1 = random.uniform(0, 0.9)
            x2 = random.uniform(x1 + 0.001, 1.0)
            y2 = random.uniform(y1 + 0.001, 1.0)
            box = BoundingBox(x_min=x1, y_min=y1, x_max=x2, y_max=y2)
            validate_bounding_box(box)
            assert 0.0 <= box.x_min <= box.x_max <= 1.0
            assert 0.0 <= box.y_min <= box.y_max <= 1.0
