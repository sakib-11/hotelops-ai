"""Tests for the deterministic geometry foundation (Task 14 Step 2).

Covers the full Step 2 scope:

- point extraction: centroid/footpoint of normal and boundary boxes;
- bounding-box validation: non-finite, inverted, out-of-range boxes;
- coordinate validation: [0, 1] unit-square bounds, VENUE_LOCAL
  unbounded, non-finite rejection;
- polygon validation: valid rings, too-few vertices, non-finite
  coordinates, degenerate zero-area rings, non-POLYGON geometry,
  IMAGE_NORMALIZED bounds;
- point-in-polygon: INSIDE / OUTSIDE / BOUNDARY (edge, vertex),
  points very close to the boundary on both sides, and points within
  the documented tolerance;
- determinism: repeated classification/extraction is stable;
- invalid geometry fails predictably (typed errors, never repairs);
- integration: the REAL Task 13 -> Task 4 -> Task 14 flow
  (``TrackObservation -> DetectionObservation.bounding_box ->
  extract_point -> SpatialPointModel``);
- isolation: the geometry package contains no hotel business
  vocabulary.

No property-based framework is used (the repository does not ship
one); determinism and no-exception sweeps provide the equivalent
guarantees with plain pytest.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest import approx

from backend.app.intelligence.geometry import (
    GEOMETRY_TOLERANCE,
    GeometryError,
    InvalidBoundingBoxError,
    InvalidCoordinateError,
    InvalidPolygonError,
    PointLocation,
    classify_point_in_polygon,
    extract_point,
    validate_bounding_box,
    validate_coordinate,
    validate_polygon,
    validate_polygon_ring,
)
from contracts.common import DetectionId, FrameId, TrackId, VideoSessionId, new_uuid
from contracts.geometry import (
    CoordinateSpace,
    GeometryModel,
    GeometryScope,
    GeometryType,
)
from contracts.spatial import SpatialPointModel, SpatialPointPolicy
from contracts.video import FramePacket
from contracts.vision import BoundingBox, DetectionObservation, TrackObservation, TrackState

# ---------------------------------------------------------------------------
# Canonical rings (implicitly closed — the canonical POLYGON convention)
# ---------------------------------------------------------------------------

# Unit square: [(0,0), (1,0), (1,1), (0,1)] with implicit closing edge.
UNIT_SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

# Closed triangle: 3 corners + closing vertex.
TRIANGLE = [(0.0, 0.0), (1.0, 0.0), (0.5, 1.0), (0.0, 0.0)]

# L-shape (concave): 6 corners, implicit closure.
L_SHAPE = [(0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (0.5, 0.5), (0.5, 1.0), (0.0, 1.0)]


def make_polygon(
    ring: list[list[float]],
    *,
    space: CoordinateSpace = CoordinateSpace.VENUE_LOCAL,
    reference_camera_profile_id: str | None = None,
) -> GeometryModel:
    """Build a canonical POLYGON GeometryModel from a raw ring."""
    scope = (
        GeometryScope.CAMERA if space == CoordinateSpace.IMAGE_NORMALIZED else GeometryScope.VENUE
    )
    return GeometryModel(
        geometry_id="test-polygon",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=space,
        geometry_scope=scope,
        coordinates=ring,
        reference_camera_profile_id=reference_camera_profile_id,
    )


# ---------------------------------------------------------------------------
# Helpers / canonical builders
# ---------------------------------------------------------------------------


def make_session() -> VideoSessionId:
    return VideoSessionId(new_uuid())


def make_frame(session: VideoSessionId, *, index: int = 0) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=session,
        frame_index=index,
        event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        width=640,
        height=480,
    )


def make_detection(
    frame: FramePacket,
    *,
    box: tuple[float, float, float, float] = (0.1, 0.1, 0.5, 0.5),
    class_name: str = "person",
    confidence: float = 0.9,
) -> DetectionObservation:
    x_min, y_min, x_max, y_max = box
    return DetectionObservation(
        detection_id=DetectionId(new_uuid()),
        frame_id=frame.frame_id,
        class_name=class_name,
        confidence=confidence,
        bounding_box=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        event_time=frame.event_time,
        session_id=frame.session_id,
        source_ref=frame.source_ref,
        frame_index=frame.frame_index,
    )


# ===========================================================================
# Point extraction (CENTROID / FOOTPOINT)
# ===========================================================================


class TestPointExtraction:
    """Point policies against the canonical BoundingBox."""

    def test_centroid_of_normal_box(self) -> None:
        point = extract_point(
            BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
            SpatialPointPolicy.CENTROID,
        )
        assert isinstance(point, SpatialPointModel)
        assert point.policy == SpatialPointPolicy.CENTROID
        assert point.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED
        assert point.x == approx(0.3)
        assert point.y == approx(0.5)

    def test_footpoint_of_normal_box(self) -> None:
        point = extract_point(
            BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
            SpatialPointPolicy.FOOTPOINT,
        )
        assert point.x == approx(0.3)
        assert point.y == approx(0.8)  # bottom edge = floor contact

    def test_full_frame_box(self) -> None:
        box = BoundingBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)
        assert extract_point(box, SpatialPointPolicy.CENTROID).x == approx(0.5)
        assert extract_point(box, SpatialPointPolicy.CENTROID).y == approx(0.5)
        footpoint = extract_point(box, SpatialPointPolicy.FOOTPOINT)
        assert footpoint.x == approx(0.5)
        assert footpoint.y == approx(1.0)

    def test_boundary_corner_box(self) -> None:
        box = BoundingBox(x_min=0.0, y_min=0.0, x_max=0.5, y_max=0.5)
        assert extract_point(box, SpatialPointPolicy.CENTROID).x == approx(0.25)
        assert extract_point(box, SpatialPointPolicy.CENTROID).y == approx(0.25)

    def test_invalid_box_rejected(self) -> None:
        box = BoundingBox.model_construct(  # bypass pydantic: inject NaN
            x_min=float("nan"), y_min=0.1, x_max=0.5, y_max=0.5
        )
        with pytest.raises(InvalidBoundingBoxError, match="finite"):
            extract_point(box, SpatialPointPolicy.CENTROID)

    def test_infinite_box_rejected(self) -> None:
        box = BoundingBox.model_construct(x_min=0.1, y_min=0.1, x_max=float("inf"), y_max=0.5)
        with pytest.raises(InvalidBoundingBoxError, match="finite"):
            extract_point(box, SpatialPointPolicy.FOOTPOINT)

    def test_inverted_box_rejected(self) -> None:
        box = BoundingBox.model_construct(x_min=0.6, y_min=0.2, x_max=0.3, y_max=0.8)
        with pytest.raises(InvalidBoundingBoxError, match="inverted"):
            validate_bounding_box(box)

    def test_out_of_range_box_rejected(self) -> None:
        box = BoundingBox.model_construct(x_min=0.1, y_min=0.1, x_max=1.5, y_max=0.9)
        with pytest.raises(InvalidBoundingBoxError, match=r"\[0, 1\]"):
            validate_bounding_box(box)


# ===========================================================================
# Coordinate validation
# ===========================================================================


class TestCoordinateValidation:
    """Unit-square bounds per the canonical IMAGE_NORMALIZED convention."""

    def test_origin_valid(self) -> None:
        validate_coordinate(0.0, 0.0)  # must not raise

    def test_unit_corner_valid(self) -> None:
        validate_coordinate(1.0, 1.0)  # must not raise

    def test_negative_coordinate_rejected(self) -> None:
        with pytest.raises(InvalidCoordinateError, match=r"\[0, 1\]"):
            validate_coordinate(-0.1, 0.5)

    def test_coordinate_above_one_rejected(self) -> None:
        with pytest.raises(InvalidCoordinateError, match=r"\[0, 1\]"):
            validate_coordinate(0.5, 1.5)

    def test_venue_local_unbounded(self) -> None:
        validate_coordinate(12.5, 8.25, coordinate_space=CoordinateSpace.VENUE_LOCAL)

    def test_nan_rejected(self) -> None:
        with pytest.raises(InvalidCoordinateError, match="finite"):
            validate_coordinate(float("nan"), 0.5)


# ===========================================================================
# Polygon validation
# ===========================================================================


class TestPolygonValidation:
    """Deterministic structural validation — never repairs."""

    def test_valid_polygon(self) -> None:
        ring = validate_polygon(make_polygon([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]))
        assert ring == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]

    def test_too_few_vertices_rejected(self) -> None:
        with pytest.raises(InvalidPolygonError, match="at least 4"):
            validate_polygon_ring([(0.0, 0.0), (1.0, 0.0)])

    def test_unclosed_triangle_rejected(self) -> None:
        # 3 coordinates is below the closed-ring minimum (3 corners +
        # closing vertex).
        with pytest.raises(InvalidPolygonError, match="at least 4"):
            validate_polygon_ring([(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)])

    def test_non_finite_coordinate_rejected(self) -> None:
        with pytest.raises(InvalidPolygonError, match="non-finite"):
            validate_polygon_ring([(0.0, 0.0), (float("nan"), 0.0), (1.0, 1.0), (0.0, 1.0)])

    def test_degenerate_zero_area_ring_rejected(self) -> None:
        with pytest.raises(InvalidPolygonError, match="zero-area"):
            validate_polygon_ring([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (0.5, 0.0)])

    def test_non_polygon_geometry_rejected(self) -> None:
        line = GeometryModel(
            geometry_id="line",
            geometry_type=GeometryType.LINESTRING,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            geometry_scope=GeometryScope.VENUE,
            coordinates=[[0, 0], [1, 1]],
        )
        with pytest.raises(InvalidPolygonError, match="POLYGON"):
            validate_polygon(line)

    def test_image_normalized_polygon_bounds_enforced(self) -> None:
        valid = make_polygon(
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
            space=CoordinateSpace.IMAGE_NORMALIZED,
            reference_camera_profile_id="cam-1",
        )
        assert validate_polygon(valid)  # must not raise

        # Bypass pydantic contract validation (model_construct) so the
        # layer's own IMAGE_NORMALIZED bound re-assertion is exercised.
        invalid = GeometryModel.model_construct(
            geometry_id="test-polygon",
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
            geometry_scope=GeometryScope.CAMERA,
            coordinates=[[0, 0], [1.5, 0], [1, 1], [0, 1], [0, 0]],
            reference_camera_profile_id="cam-1",
        )
        with pytest.raises(InvalidPolygonError, match=r"\[0, 1\]"):
            validate_polygon(invalid)


# ===========================================================================
# Point-in-polygon (INSIDE / OUTSIDE / BOUNDARY)
# ===========================================================================


class TestPointInPolygon:
    """Tri-state classification on the unit square, triangle, L-shape."""

    def test_clearly_inside(self) -> None:
        assert classify_point_in_polygon((0.5, 0.5), UNIT_SQUARE) == PointLocation.INSIDE

    def test_clearly_outside(self) -> None:
        assert classify_point_in_polygon((1.5, 0.5), UNIT_SQUARE) == PointLocation.OUTSIDE
        assert classify_point_in_polygon((-0.5, 0.5), UNIT_SQUARE) == PointLocation.OUTSIDE
        assert classify_point_in_polygon((0.5, 1.5), UNIT_SQUARE) == PointLocation.OUTSIDE

    def test_point_on_edge(self) -> None:
        assert classify_point_in_polygon((0.5, 0.0), UNIT_SQUARE) == PointLocation.BOUNDARY
        assert classify_point_in_polygon((0.5, 1.0), UNIT_SQUARE) == PointLocation.BOUNDARY
        assert classify_point_in_polygon((0.0, 0.5), UNIT_SQUARE) == PointLocation.BOUNDARY

    def test_point_on_vertex(self) -> None:
        assert classify_point_in_polygon((0.0, 0.0), UNIT_SQUARE) == PointLocation.BOUNDARY
        assert classify_point_in_polygon((1.0, 1.0), UNIT_SQUARE) == PointLocation.BOUNDARY

    def test_very_close_inside(self) -> None:
        # 10x beyond tolerance (GEOMETRY_TOLERANCE * 10) — still inside.
        offset = GEOMETRY_TOLERANCE * 10
        assert classify_point_in_polygon((0.5, offset), UNIT_SQUARE) == PointLocation.INSIDE

    def test_very_close_outside(self) -> None:
        offset = GEOMETRY_TOLERANCE * 10
        assert classify_point_in_polygon((0.5, -offset), UNIT_SQUARE) == PointLocation.OUTSIDE

    def test_within_tolerance_is_boundary(self) -> None:
        # 0.1x tolerance from the edge -> BOUNDARY.
        offset = GEOMETRY_TOLERANCE * 0.1
        assert classify_point_in_polygon((0.5, offset), UNIT_SQUARE) == PointLocation.BOUNDARY
        assert classify_point_in_polygon((0.5, -offset), UNIT_SQUARE) == PointLocation.BOUNDARY

    def test_triangle_inside_and_apex(self) -> None:
        assert classify_point_in_polygon((0.5, 0.3), TRIANGLE) == PointLocation.INSIDE
        assert classify_point_in_polygon((0.5, 0.9999), TRIANGLE) == PointLocation.INSIDE
        assert classify_point_in_polygon((0.5, 1.5), TRIANGLE) == PointLocation.OUTSIDE
        assert classify_point_in_polygon((0.5, 0.0), TRIANGLE) == PointLocation.BOUNDARY

    def test_concave_polygon(self) -> None:
        assert classify_point_in_polygon((0.75, 0.25), L_SHAPE) == PointLocation.INSIDE
        assert classify_point_in_polygon((0.25, 0.75), L_SHAPE) == PointLocation.INSIDE
        assert classify_point_in_polygon((0.75, 0.75), L_SHAPE) == PointLocation.OUTSIDE  # notch
        assert classify_point_in_polygon((0.75, 0.5), L_SHAPE) == PointLocation.BOUNDARY

    def test_point_aligned_with_vertex_ray(self) -> None:
        # (0.25, 0.5) shares the row of L_SHAPE's vertex (0.5, 0.5) — the
        # classic ray-casting pitfall; the half-open rule must classify
        # it INSIDE without double-toggling.
        assert classify_point_in_polygon((0.25, 0.5), L_SHAPE) == PointLocation.INSIDE

    def test_custom_tolerance_used(self) -> None:
        # A caller-supplied tolerance replaces the documented default.
        offset = GEOMETRY_TOLERANCE * 10
        assert (
            classify_point_in_polygon((0.5, offset), UNIT_SQUARE, tolerance=offset * 2)
            == PointLocation.BOUNDARY
        )
        assert (
            classify_point_in_polygon((0.5, offset), UNIT_SQUARE, tolerance=offset / 2)
            == PointLocation.INSIDE
        )

    def test_non_finite_query_point_rejected(self) -> None:
        with pytest.raises(InvalidCoordinateError, match="finite"):
            classify_point_in_polygon((float("nan"), 0.5), UNIT_SQUARE)

    def test_malformed_ring_rejected(self) -> None:
        with pytest.raises(InvalidPolygonError):
            classify_point_in_polygon((0.5, 0.5), [(0.0, 0.0), (1.0, 0.0)])


# ===========================================================================
# Determinism / no-exception sweeps
# ===========================================================================


class TestDeterminism:
    """Same inputs always produce the same result — no randomness or state."""

    def test_classification_is_deterministic(self) -> None:
        first = classify_point_in_polygon((0.5, 0.5), UNIT_SQUARE)
        for _ in range(1000):
            assert classify_point_in_polygon((0.5, 0.5), UNIT_SQUARE) == first

    def test_extraction_is_deterministic(self) -> None:
        box = BoundingBox(x_min=0.1, y_min=0.2, x_max=0.6, y_max=0.9)
        first = extract_point(box, SpatialPointPolicy.FOOTPOINT)
        for _ in range(500):
            assert extract_point(box, SpatialPointPolicy.FOOTPOINT) == first

    def test_valid_geometry_never_raises(self) -> None:
        """Grid sweep: valid inputs only ever produce valid outcomes."""
        for x in (i / 10 for i in range(-2, 13)):
            for y in (j / 10 for j in range(-2, 13)):
                result = classify_point_in_polygon((x, y), UNIT_SQUARE)
                assert result in (
                    PointLocation.INSIDE,
                    PointLocation.OUTSIDE,
                    PointLocation.BOUNDARY,
                )

    def test_invalid_geometry_fails_predictably(self) -> None:
        """Invalid inputs always raise a typed GeometryError subclass."""
        with pytest.raises(GeometryError):
            validate_polygon_ring([(0.0, 0.0)])
        with pytest.raises(GeometryError):
            classify_point_in_polygon((0.5, 0.5), [(0.0, 0.0), (1.0, 0.0), (0.5, 0.0)])
        with pytest.raises(GeometryError):
            validate_coordinate(0.5, -2.0)


# ===========================================================================
# Integration with existing contracts
# ===========================================================================


class TestContractIntegration:
    """Real Task 13 -> Task 4 -> Task 14 flow, no bypass."""

    def test_track_observation_to_spatial_point(self) -> None:
        session = make_session()
        frame = make_frame(session, index=7)
        det = make_detection(frame, box=(0.1, 0.2, 0.6, 0.9))
        track = TrackObservation(
            track_id=TrackId(new_uuid()),
            detection_id=det.detection_id,
            frame_id=frame.frame_id,
            session_id=session,
            event_time=frame.event_time,
            track_state=TrackState.ACTIVE,
        )

        point = extract_point(det.bounding_box, SpatialPointPolicy.FOOTPOINT)

        assert point.policy == SpatialPointPolicy.FOOTPOINT
        assert point.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED
        assert point.x == approx(0.35)
        assert point.y == approx(0.9)
        # The provenance chain is real: the track names the detection
        # whose canonical bounding box produced the point.
        assert track.detection_id == det.detection_id
        assert track.session_id == session
        assert track.frame_id == frame.frame_id

    def test_point_is_round_trip_serializable(self) -> None:
        point = extract_point(
            BoundingBox(x_min=0.1, y_min=0.2, x_max=0.6, y_max=0.9),
            SpatialPointPolicy.CENTROID,
        )
        restored = SpatialPointModel.model_validate(point.model_dump(mode="json"))
        assert restored == point


# ===========================================================================
# Architectural isolation (Task 14 Step 2 §10)
# ===========================================================================


class TestBusinessIsolation:
    """The geometry layer must not know hotel business concepts."""

    def test_no_business_vocabulary_in_geometry_package(self) -> None:
        geometry_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "geometry"
        )
        # Full vocabulary from Task 14 Step 2 §10. "queue"/"service"
        # are deliberately included: the layer must not name ANY business
        # concept, and the current files contain neither word.
        forbidden = [
            "guest",
            "staff",
            "queue",
            "service",
            "occupancy",
            "alert",
            "opportunity",
            "recommendation",
        ]
        for path in sorted(geometry_dir.glob("*.py")):
            text = path.read_text()
            for word in forbidden:
                assert not re.search(rf"\b{word}\b", text, re.IGNORECASE), (
                    f"business term {word!r} leaked into {path.name}"
                )
