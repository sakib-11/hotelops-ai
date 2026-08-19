"""Unit tests for the authoritative geometry contract (Task 10.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.geometry import (
    CoordinateSpace,
    EntityGeometryContract,
    GeometryModel,
    GeometryScope,
    GeometryType,
)


def _polygon(coords, space=CoordinateSpace.VENUE_LOCAL, scope=GeometryScope.VENUE):
    return GeometryModel(
        geometry_id="g1",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=space,
        geometry_scope=scope,
        coordinates=coords,
    )


class TestGeometryConstruction:
    def test_valid_venue_polygon(self) -> None:
        g = _polygon([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        assert g.geometry_type == GeometryType.POLYGON
        assert g.coordinate_space == CoordinateSpace.VENUE_LOCAL
        assert g.is_camera_relative is False

    def test_valid_point(self) -> None:
        g = GeometryModel(
            geometry_id="p1",
            geometry_type=GeometryType.POINT,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            geometry_scope=GeometryScope.VENUE,
            coordinates=[[5, 5]],
        )
        assert g.geometry_type == GeometryType.POINT

    def test_valid_linestring(self) -> None:
        g = GeometryModel(
            geometry_id="l1",
            geometry_type=GeometryType.LINESTRING,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            geometry_scope=GeometryScope.VENUE,
            coordinates=[[0, 0], [10, 10]],
        )
        assert g.geometry_type == GeometryType.LINESTRING

    def test_image_normalized_camera_roi_requires_camera_ref(self) -> None:
        with pytest.raises(ValidationError, match="reference a camera profile"):
            GeometryModel(
                geometry_id="r1",
                geometry_type=GeometryType.POLYGON,
                coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
                geometry_scope=GeometryScope.CAMERA,
                coordinates=[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5], [0, 0]],
            )

    def test_image_normalized_camera_roi_with_ref(self) -> None:
        g = GeometryModel(
            geometry_id="r1",
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
            geometry_scope=GeometryScope.CAMERA,
            reference_camera_profile_id="cam-1",
            coordinates=[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5], [0, 0]],
        )
        assert g.is_camera_relative is True
        assert g.reference_camera_profile_id == "cam-1"

    def test_scope_space_coupling(self) -> None:
        with pytest.raises(ValidationError):
            GeometryModel(
                geometry_id="x",
                geometry_type=GeometryType.POINT,
                coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[0.5, 0.5]],
            )

    def test_polygon_min_vertices(self) -> None:
        with pytest.raises(ValidationError, match="closed ring of at least 4"):
            _polygon([[0, 0], [1, 0], [0, 0]])

    def test_linestring_min_vertices(self) -> None:
        with pytest.raises(ValidationError, match="at least 2"):
            GeometryModel(
                geometry_id="l",
                geometry_type=GeometryType.LINESTRING,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[0, 0]],
            )

    def test_rejects_nan_and_infinity(self) -> None:
        with pytest.raises(ValidationError, match="finite"):
            _polygon([[0, 0], [10, float("nan")], [10, 10], [0, 10], [0, 0]])
        with pytest.raises(ValidationError, match="finite"):
            _polygon([[0, 0], [10, 0], [10, float("inf")], [0, 10], [0, 0]])

    def test_rejects_empty_geometry(self) -> None:
        with pytest.raises(ValidationError, match="at least 1 item"):
            GeometryModel(
                geometry_id="e",
                geometry_type=GeometryType.POINT,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[],
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            GeometryModel.model_validate({
                "geometry_id": "g1",
                "geometry_type": GeometryType.POLYGON,
                "coordinate_space": CoordinateSpace.VENUE_LOCAL,
                "geometry_scope": GeometryScope.VENUE,
                "coordinates": [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
                "unexpected": 1,
            })


class TestImageNormalizedBounds:
    def test_out_of_range_coordinate_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometryModel(
                geometry_id="r",
                geometry_type=GeometryType.POLYGON,
                coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
                geometry_scope=GeometryScope.CAMERA,
                reference_camera_profile_id="cam-1",
                coordinates=[[0, 0], [1.5, 0], [1.5, 1], [0, 1], [0, 0]],
            )

    def test_unit_square_valid(self) -> None:
        g = GeometryModel(
            geometry_id="r",
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
            geometry_scope=GeometryScope.CAMERA,
            reference_camera_profile_id="cam-1",
            coordinates=[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
        )
        assert g.area == pytest.approx(1.0)


class TestCanonicalization:
    def test_rounds_to_precision(self) -> None:
        g = _polygon([[0.0, 0.0], [10.123456789, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]])
        canonical = g.canonicalize()
        # VENUE_LOCAL precision is 3 decimals.
        assert canonical.coordinates[1][0] == pytest.approx(10.123)

    def test_image_normalized_precision_six(self) -> None:
        g = GeometryModel(
            geometry_id="r",
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
            geometry_scope=GeometryScope.CAMERA,
            reference_camera_profile_id="cam-1",
            coordinates=[[0.0, 0.0], [0.123456789, 0.0], [0.5, 0.5], [0.0, 0.5], [0.0, 0.0]],
        )
        assert g.canonicalize().coordinates[1][0] == pytest.approx(0.123457)

    def test_polygon_ring_closed_by_canonicalization(self) -> None:
        g = _polygon([[0, 0], [10, 0], [10, 10], [0, 10]])  # unclosed
        closed = g.canonicalize()
        assert closed.coordinates[-1] == closed.coordinates[0]
        # Original is untouched (immutability).
        assert g.coordinates[-1] != g.coordinates[0]


class TestGeometryValidity:
    def test_self_intersecting_bowtie(self) -> None:
        g = _polygon([[0, 0], [4, 4], [0, 4], [4, 0], [0, 0]])
        assert g.is_self_intersecting() is True

    def test_valid_rectangle_not_self_intersecting(self) -> None:
        g = _polygon([[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]])
        assert g.is_self_intersecting() is False

    def test_zero_area_degenerate(self) -> None:
        g = _polygon([[0, 0], [4, 0], [8, 0], [0, 0]])  # collinear
        assert g.is_degenerate() is True

    def test_positive_area(self) -> None:
        g = _polygon([[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]])
        assert g.is_degenerate() is False
        assert abs(g.area - 16.0) < 1e-9


class TestEntityGeometryContract:
    def test_zone_contract(self) -> None:
        contract = EntityGeometryContract(
            entity_type="zone",
            allowed_geometry_types=frozenset({GeometryType.POLYGON}),
            allowed_coordinate_spaces=frozenset({CoordinateSpace.VENUE_LOCAL}),
            allowed_scopes=frozenset({GeometryScope.VENUE}),
            min_area=1e-9,
        )
        zone = _polygon([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        assert contract.permits(zone)

        # A camera-relative zone must not pass a venue-only contract.
        cam_zone = GeometryModel(
            geometry_id="z",
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
            geometry_scope=GeometryScope.CAMERA,
            reference_camera_profile_id="cam-1",
            coordinates=[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5], [0, 0]],
        )
        assert not contract.permits(cam_zone)
