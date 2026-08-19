"""Tests for table mapping and ambiguity resolution (Task 14 Step 5).

Extends the Step 3 spatial evaluation engine with deterministic table
mapping, the zone/table relationship, and combined zone/table ambiguity
resolution. Covers the full Step 5 scope:

- table membership: a canonical VENUE_LOCAL point is classified against
  every version-owned table (Task 10 ``TableModel`` — POLYGON in
  VENUE_LOCAL, venue-scoped because Task 10 defines no camera→table
  binding) via the Step 2 geometry layer; BOUNDARY follows the same
  recorded blocker policy as zones;
- zone/table relationship: the configuration-declared
  ``ZoneModel.contained_tables`` is preserved, never assumed to be
  "table == zone" (a table may be contained, overlap without
  containment, or have no zone relationship);
- exclusion precedence: EXCLUDED/PRIVACY never receive a zone/table
  identity and are never table-mapped;
- ambiguity: AMBIGUOUS is the single combined state for overlapping
  zones OR overlapping tables; explicit ``table_priority`` resolves
  table overlap deterministically;
- configuration-version provenance (V1 pinned after V2 is published
  must not change the historical result), camera isolation, and
  tenant/venue isolation through the passed configuration;
- determinism (identical inputs, table row-order independence) and the
  full provenance chain on the canonical ``SpatialObservation``.

Fixture builders are reused from ``test_spatial_evaluation`` (the REAL
Task 10 contracts) — no duplicated geometry or configuration models.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.app.intelligence.spatial import (
    SpatialEvaluationInput,
    SpatialEvaluationResult,
    evaluate_spatial,
)
from backend.app.intelligence.spatial.exceptions import (
    BoundaryPolicyUndefinedError,
    CameraNotInConfigurationError,
    ConfigurationNotPublishedError,
    InvalidSpatialInputError,
    ReferenceIntegrityError,
    VenuePointRequiredError,
)
from contracts.common import CameraId, TrackId, VideoSessionId
from contracts.configuration import (
    ConfigurationStatus,
    ConfigurationVersionModel,
    ExclusionROIModel,
    PrivacyROIModel,
    TableModel,
    ZoneModel,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryScope, GeometryType
from contracts.spatial import (
    SPATIAL_ENGINE_VERSION,
    SpatialObservation,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.vision import TrackObservation
from tests.unit.test_spatial_evaluation import (
    ZONE_RECT,
    _camera,
    _exclusion,
    _point,
    _privacy,
    _track,
    _venue_polygon,
    _version,
    _zone,
)

# Canonical table polygons (VENUE_LOCAL), strictly interior points used
# so no test point lies on a boundary unless the fixture says so.
TABLE_A = [[2, 2], [4, 2], [4, 4], [2, 4]]  # point (3, 3)
TABLE_B = [[6, 2], [8, 2], [8, 4], [6, 4]]  # point (7, 3)
# Overlaps TABLE_A in [3, 4] x [3, 4]; point (3.5, 3.5) is inside both.
TABLE_C = [[3, 3], [5, 3], [5, 5], [3, 5]]
TABLE_AWAY = [[50, 50], [52, 50], [52, 52], [50, 52]]  # no zone relationship
TABLE_OVERLAP = [[5, 9], [7, 9], [7, 11], [5, 11]]  # geometrically in zone, not contained


def _table(profile_id: str, coords: list[list[float]], *, name: str | None = None) -> TableModel:
    return TableModel(
        profile_id=profile_id,
        name=name or profile_id,
        geometry=_venue_polygon(coords),
        seat_count=4,
    )


def _zone_containing(
    profile_id: str, coords: list[list[float]], contained_tables: tuple[str, ...] = ()
) -> ZoneModel:
    return _zone(profile_id, coords).model_copy(update={"contained_tables": list(contained_tables)})


def _lobby_with_tables(
    *,
    camera_id: CameraId | None = None,
    tables: tuple[TableModel, ...] = (_table("t-a", TABLE_A),),
    contained_tables: tuple[str, ...] = ("t-a",),
    zone: ZoneModel | None = None,
    exclusion_rois: tuple[ExclusionROIModel, ...] = (),
    privacy_rois: tuple[PrivacyROIModel, ...] = (),
) -> ConfigurationVersionModel:
    camera = _camera(
        profile_id="cam-1",
        camera_id=camera_id,
        detection_zones=("z-lobby",),
        exclusion_rois=tuple(r.profile_id for r in exclusion_rois),
        privacy_rois=tuple(r.profile_id for r in privacy_rois),
    )
    return _version(
        cameras=(camera,),
        zones=(zone or _zone_containing("z-lobby", ZONE_RECT, contained_tables),),
        tables=tables,
        exclusion_rois=exclusion_rois,
        privacy_rois=privacy_rois,
    )


def evaluate(
    configuration: ConfigurationVersionModel,
    camera_id: CameraId,
    point: SpatialPointModel,
    *,
    track: TrackObservation | None = None,
    zone_priority: tuple[str, ...] = (),
    table_priority: tuple[str, ...] = (),
) -> SpatialEvaluationResult:
    """Run the pure engine with a canonical input (default track)."""
    return evaluate_spatial(
        SpatialEvaluationInput(
            configuration=configuration,
            track=track or _track(),
            camera_id=camera_id,
            point=point,
            zone_priority=zone_priority,
            table_priority=table_priority,
        )
    )


# =============================================================================
# 1-5. Table membership (INSIDE / OUTSIDE / BOUNDARY)
# =============================================================================


class TestTableMembership:
    """A point is classified against every version-owned table geometry."""

    def test_point_inside_one_table(self) -> None:
        config = _lobby_with_tables()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"
        assert result.observation.table_profile_id == "t-a"
        # Every version table was classified (deterministic profile_id order).
        assert [m.table_profile_id for m in result.table_memberships] == ["t-a"]
        assert result.table_memberships[0].location.value == "inside"

    def test_point_outside_all_tables(self) -> None:
        config = _lobby_with_tables()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(9.0, 9.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"
        assert result.observation.table_profile_id is None
        assert result.table_memberships[0].location.value == "outside"

    def test_point_on_table_boundary_raises_recorded_blocker(self) -> None:
        # (4, 2) lies on TABLE_A's edge — BOUNDARY has no Task 10 policy.
        config = _lobby_with_tables()
        with pytest.raises(BoundaryPolicyUndefinedError, match="table 't-a'"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(4.0, 2.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_point_inside_one_zone_with_table_present(self) -> None:
        config = _lobby_with_tables()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(9.0, 9.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"

    def test_point_outside_all_zones_and_tables(self) -> None:
        config = _lobby_with_tables()
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(100.0, 100.0, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.OUTSIDE
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id is None


# =============================================================================
# 6-7 / 12-13. Overlapping geometry and combined ambiguity
# =============================================================================


class TestOverlappingGeometry:
    """Overlapping zones or tables never silently pick the first match."""

    def test_overlapping_zones_with_tables_is_ambiguous(self) -> None:
        zone_b = _zone_containing("z-b", [[5, 5], [15, 5], [15, 15], [5, 15]])
        config = _version(
            cameras=(_camera(profile_id="cam-1", detection_zones=("z-lobby", "z-b")),),
            zones=(_zone_containing("z-lobby", ZONE_RECT), zone_b),
            tables=(_table("t-mid", [[7, 7], [8, 7], [8, 8], [7, 8]]),),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(7.5, 7.5, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.AMBIGUOUS
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id is None
        # Zone overlap wins the status; table membership is preserved for audit.
        assert [m.table_profile_id for m in result.table_memberships] == ["t-mid"]
        assert result.table_memberships[0].location.value == "inside"

    def test_overlapping_tables_without_priority_is_ambiguous(self) -> None:
        config = _lobby_with_tables(
            tables=(_table("t-a", TABLE_A), _table("t-c", TABLE_C)),
            contained_tables=("t-a", "t-c"),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL),
        )
        # One zone matched, but two tables matched and no precedence exists.
        assert result.observation.status is SpatialStatus.AMBIGUOUS
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id is None
        # Both table matches preserved for audit — never a silent first-pick.
        assert sorted(m.table_profile_id for m in result.table_memberships) == ["t-a", "t-c"]
        assert all(m.location.value == "inside" for m in result.table_memberships)

    def test_overlapping_tables_with_explicit_priority(self) -> None:
        config = _lobby_with_tables(
            tables=(_table("t-a", TABLE_A), _table("t-c", TABLE_C)),
            contained_tables=("t-a", "t-c"),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL),
            table_priority=("t-c", "t-a"),
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"
        assert result.observation.table_profile_id == "t-c"

    def test_priority_list_that_matches_nothing_stays_ambiguous(self) -> None:
        # t-b is a valid version table but does NOT contain the point, so
        # the priority list resolves nothing among the matches.
        config = _lobby_with_tables(
            tables=(_table("t-a", TABLE_A), _table("t-b", TABLE_B), _table("t-c", TABLE_C)),
            contained_tables=("t-a", "t-b", "t-c"),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL),
            table_priority=("t-b",),
        )
        assert result.observation.status is SpatialStatus.AMBIGUOUS
        assert result.observation.table_profile_id is None

    def test_overlapping_tables_without_zone_is_ambiguous(self) -> None:
        # Two overlapping tables with NO zone relationship: the combined
        # ambiguity state is AMBIGUOUS even though no zone matches (the
        # point is genuinely ambiguous about WHICH table it is at).
        away_c = [[49, 49], [51, 49], [51, 51], [49, 51]]  # overlaps TABLE_AWAY
        config = _version(
            cameras=(_camera(profile_id="cam-1", detection_zones=()),),
            tables=(_table("t-away", TABLE_AWAY), _table("t-away-c", away_c)),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(50.5, 50.5, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.AMBIGUOUS
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id is None
        assert sorted(m.table_profile_id for m in result.table_memberships) == [
            "t-away",
            "t-away-c",
        ]


# =============================================================================
# 5 / 8-9. Zone/table relationship (configuration-declared, never assumed)
# =============================================================================


class TestZoneTableRelationship:
    """The relationship is ZoneModel.contained_tables — not "table == zone"."""

    def test_table_inside_zone_contained_relationship(self) -> None:
        config = _lobby_with_tables(contained_tables=("t-a",))
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.table_profile_id == "t-a"
        # The zone's declared contained tables are preserved on the result.
        assert result.matched_zone_contained_tables == ("t-a",)

    def test_table_overlaps_zone_without_containment(self) -> None:
        # The table lies geometrically inside the zone, but the zone does
        # not declare it contained — the config semantics are preserved,
        # never repaired or invented.
        config = _lobby_with_tables(
            tables=(_table("t-overlap", TABLE_OVERLAP),),
            contained_tables=(),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(6.0, 9.5, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"
        assert result.observation.table_profile_id == "t-overlap"
        assert result.matched_zone_contained_tables == ()

    def test_table_outside_zone_has_no_zone_relationship(self) -> None:
        config = _lobby_with_tables(
            tables=(_table("t-away", TABLE_AWAY),),
            contained_tables=(),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(51.0, 51.0, space=CoordinateSpace.VENUE_LOCAL),
        )
        # No zone matches — OUTSIDE (zone-driven status), but the table
        # identity is retained: the table mapping is not discarded.
        assert result.observation.status is SpatialStatus.OUTSIDE
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id == "t-away"
        assert result.matched_zone_contained_tables == ()

    def test_zone_contained_tables_do_not_force_a_table_match(self) -> None:
        # The zone declares t-a as contained, but the point is elsewhere
        # in the zone — no table match, contained list still preserved.
        config = _lobby_with_tables(contained_tables=("t-a",))
        result = evaluate(
            config, config.cameras[0].camera_id, _point(9.0, 9.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.table_profile_id is None
        assert result.matched_zone_contained_tables == ("t-a",)


# =============================================================================
# 6 / 10-11. Exclusion precedence (never table/zone-mapped when intercepted)
# =============================================================================


class TestExclusionPrecedence:
    """EXCLUDED/PRIVACY observations are produced, never zone/table-mapped."""

    def test_exclusion_overlapping_table(self) -> None:
        # The exclusion ROI covers TABLE_A; the point is inside both.
        config = _lobby_with_tables(
            exclusion_rois=(
                _exclusion(
                    "x-table",
                    [[0, 0], [5, 0], [5, 5], [0, 5]],
                    space=CoordinateSpace.VENUE_LOCAL,
                ),
            ),
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.EXCLUDED
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id is None
        assert result.matched_exclusion_roi_profile_id == "x-table"
        # Policy-intercepted points are not table-mapped.
        assert result.table_memberships == ()

    def test_exclusion_overlapping_zone(self) -> None:
        config = _lobby_with_tables(
            exclusion_rois=(_exclusion("x-zone", ZONE_RECT, space=CoordinateSpace.VENUE_LOCAL),),
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.EXCLUDED
        assert result.observation.table_profile_id is None
        assert result.table_memberships == ()

    def test_privacy_overlapping_table(self) -> None:
        config = _lobby_with_tables(
            privacy_rois=(
                _privacy(
                    "p-table",
                    [[0, 0], [5, 0], [5, 5], [0, 5]],
                    space=CoordinateSpace.VENUE_LOCAL,
                ),
            ),
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.PRIVACY
        assert result.observation.table_profile_id is None
        assert result.matched_privacy_roi_profile_id == "p-table"
        assert result.table_memberships == ()

    def test_excluded_observation_not_deleted(self) -> None:
        track = _track()
        config = _lobby_with_tables(
            exclusion_rois=(
                _exclusion(
                    "x-table",
                    [[0, 0], [5, 0], [5, 5], [0, 5]],
                    space=CoordinateSpace.VENUE_LOCAL,
                ),
            ),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            track=track,
        )
        obs = result.observation
        assert obs.status is SpatialStatus.EXCLUDED
        assert obs.session_id == track.session_id
        assert obs.track_id == track.track_id
        assert obs.frame_id == track.frame_id


# =============================================================================
# 9 / 14-15. Configuration-version provenance (never the latest)
# =============================================================================


class TestConfigurationProvenance:
    """The pinned immutable version drives table mapping — never the latest."""

    def _v1_and_v2_with_moved_table(
        self,
    ) -> tuple[ConfigurationVersionModel, ConfigurationVersionModel]:
        camera_id = CameraId(uuid.uuid4())
        cam = _camera(profile_id="cam-1", camera_id=camera_id, detection_zones=("z-lobby",))
        v1 = _version(
            version=1,
            cameras=(cam,),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_A),),
        )
        # Same zone; the table physically moved far away in V2.
        v2 = _version(
            version=2,
            cameras=(cam,),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_AWAY),),
        )
        return v1, v2

    def test_observation_carries_pinned_configuration_version(self) -> None:
        config = _lobby_with_tables()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.configuration_version_id == config.configuration_version_id

    def test_historical_v1_pinned_after_v2_published(self) -> None:
        v1, v2 = self._v1_and_v2_with_moved_table()
        point = _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        track = _track()
        result_v1 = evaluate(v1, v1.cameras[0].camera_id, point, track=track)
        assert result_v1.observation.status is SpatialStatus.INSIDE
        assert result_v1.observation.table_profile_id == "t-a"
        assert result_v1.observation.configuration_version_id == v1.configuration_version_id

        # V2 is published for the same camera — the historical result is
        # unchanged: V2 maps the same point to NO table.
        result_v2 = evaluate(v2, v2.cameras[0].camera_id, point, track=track)
        assert result_v2.observation.status is SpatialStatus.INSIDE
        assert result_v2.observation.table_profile_id is None
        assert result_v2.observation.configuration_version_id == v2.configuration_version_id

        # Re-evaluating against V1 is byte-identical — no fallback to latest.
        replay = evaluate(v1, v1.cameras[0].camera_id, point, track=track)
        assert replay.observation == result_v1.observation


# =============================================================================
# 10 / 16-17. Camera isolation
# =============================================================================


class TestCameraIsolation:
    """Tables are version-scoped; cameras are isolated through the pinned version."""

    def test_tables_are_version_scoped_for_cameras_in_version(self) -> None:
        # Task 10 defines no camera→table binding: both cameras in the
        # version observe the same venue-scoped tables for a VENUE_LOCAL
        # point. Camera B declares no zones, so it is OUTSIDE zone-wise
        # but still maps the venue table.
        cam_a = _camera(profile_id="cam-a", detection_zones=("z-lobby",))
        cam_b = _camera(profile_id="cam-b", detection_zones=())
        config = _version(
            cameras=(cam_a, cam_b),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_A),),
        )
        point = _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        result_a = evaluate(config, cam_a.camera_id, point)
        result_b = evaluate(config, cam_b.camera_id, point)
        assert result_a.observation.status is SpatialStatus.INSIDE
        assert result_a.observation.table_profile_id == "t-a"
        assert result_b.observation.status is SpatialStatus.OUTSIDE
        assert result_b.observation.table_profile_id == "t-a"

    def test_unknown_camera_rejected(self) -> None:
        config = _lobby_with_tables()
        with pytest.raises(CameraNotInConfigurationError, match="not configured"):
            evaluate(
                config,
                CameraId(uuid.uuid4()),
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_camera_from_another_version_rejected(self) -> None:
        config = _lobby_with_tables()
        other = _lobby_with_tables()
        with pytest.raises(CameraNotInConfigurationError):
            evaluate(
                config,
                other.cameras[0].camera_id,
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_image_point_with_version_tables_requires_venue_point(self) -> None:
        # Table mapping needs a VENUE_LOCAL point; an IMAGE_NORMALIZED
        # point against a camera in a version that has tables raises the
        # recorded projection blocker (no silent NO_MATCH).
        config = _lobby_with_tables()
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(config, config.cameras[0].camera_id, _point(0.5, 0.5))

    def test_tables_alone_trigger_projection_blocker(self) -> None:
        # A camera declaring NO zones still cannot table-map an
        # IMAGE_NORMALIZED point when the version owns tables — the
        # venue-scoped tables alone are venue geometry (Step 5).
        camera = _camera(profile_id="cam-1", detection_zones=())
        config = _version(
            cameras=(camera,),
            tables=(_table("t-a", TABLE_A),),
        )
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(config, camera.camera_id, _point(0.5, 0.5))


# =============================================================================
# 11 / 18-19. Tenant / venue isolation
# =============================================================================


class TestTenantVenueIsolation:
    """The engine evaluates ONLY against the configuration it is given."""

    def test_tenant_isolation(self) -> None:
        camera_id = CameraId(uuid.uuid4())
        cam_a = _camera(camera_id=camera_id, detection_zones=("z-lobby",))
        cam_b = _camera(camera_id=camera_id, detection_zones=("z-lobby",))
        config_a = _version(
            tenant_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            cameras=(cam_a,),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_A),),
        )
        config_b = _version(
            tenant_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            cameras=(cam_b,),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_AWAY),),
        )
        point = _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        result_a = evaluate(config_a, camera_id, point)
        result_b = evaluate(config_b, camera_id, point)
        # Same physical camera + point classify by the config in force —
        # tenant A's table never leaks into tenant B.
        assert result_a.observation.table_profile_id == "t-a"
        assert result_b.observation.table_profile_id is None
        assert result_a.observation.configuration_version_id == config_a.configuration_version_id
        assert result_b.observation.configuration_version_id == config_b.configuration_version_id

    def test_venue_isolation(self) -> None:
        camera_id = CameraId(uuid.uuid4())
        tenant_id = uuid.uuid4()
        config_venue_a = _version(
            tenant_id=tenant_id,
            venue_id=uuid.uuid4(),
            cameras=(_camera(camera_id=camera_id, detection_zones=("z-lobby",)),),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_A),),
        )
        config_venue_b = _version(
            tenant_id=tenant_id,
            venue_id=uuid.uuid4(),
            cameras=(_camera(camera_id=camera_id, detection_zones=("z-lobby",)),),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
            tables=(_table("t-a", TABLE_AWAY),),
        )
        point = _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        assert evaluate(config_venue_a, camera_id, point).observation.table_profile_id == "t-a"
        assert evaluate(config_venue_b, camera_id, point).observation.table_profile_id is None


# =============================================================================
# 14 / 20. Negative tests (failures explicit, never silent fallback)
# =============================================================================


class TestNegativeCases:
    """Malformed or missing inputs fail with typed errors — never repaired."""

    def test_missing_configuration_rejected(self) -> None:
        with pytest.raises(InvalidSpatialInputError, match="configuration"):
            SpatialEvaluationInput(
                configuration=None,  # type: ignore[arg-type]
                track=_track(),
                camera_id=CameraId(uuid.uuid4()),
                point=_point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_unpublished_configuration_rejected(self) -> None:
        for status in (
            ConfigurationStatus.DRAFT,
            ConfigurationStatus.VALIDATING,
            ConfigurationStatus.VALIDATED,
        ):
            config = _version(
                status=status,
                cameras=(_camera(detection_zones=("z-lobby",)),),
                zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-a",)),),
                tables=(_table("t-a", TABLE_A),),
            )
            with pytest.raises(ConfigurationNotPublishedError, match="PUBLISHED"):
                evaluate(
                    config,
                    config.cameras[0].camera_id,
                    _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
                )

    def test_invalid_point_rejected(self) -> None:
        config = _lobby_with_tables()
        nan_point = SpatialPointModel(
            x=float("nan"),
            y=3.0,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        with pytest.raises(InvalidSpatialInputError, match="finite"):
            evaluate(config, config.cameras[0].camera_id, nan_point)

    def test_invalid_table_geometry_rejected(self) -> None:
        # A table whose geometry is a POINT violates the table contract.
        bad_table = TableModel(
            profile_id="t-bad",
            name="bad",
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.POINT,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[1, 1]],
            ),
        )
        config = _version(
            cameras=(_camera(detection_zones=("z-lobby",)),),
            zones=(_zone_containing("z-lobby", ZONE_RECT),),
            tables=(bad_table,),
        )
        with pytest.raises(ReferenceIntegrityError, match="tables must be POLYGON"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_invalid_table_polygon_ring_rejected(self) -> None:
        # A degenerate (zero-area) ring passes the pydantic contract but
        # fails the Step 2 polygon validation re-asserted by the engine.
        degenerate = _table("t-bad", [[0, 0], [1, 0], [2, 0], [0, 0]])
        config = _version(
            cameras=(_camera(detection_zones=("z-lobby",)),),
            zones=(_zone_containing("z-lobby", ZONE_RECT),),
            tables=(degenerate,),
        )
        with pytest.raises(ReferenceIntegrityError, match="failed validation"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_invalid_zone_geometry_rejected(self) -> None:
        bad_zone = ZoneModel(
            profile_id="z-bad",
            name="bad",
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.POINT,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[1, 1]],
            ),
        )
        config = _version(
            cameras=(_camera(detection_zones=("z-bad",)),),
            zones=(bad_zone,),
            tables=(_table("t-a", TABLE_A),),
        )
        with pytest.raises(ReferenceIntegrityError, match="zones must be POLYGON"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_mismatched_camera_rejected(self) -> None:
        config = _lobby_with_tables()
        with pytest.raises(CameraNotInConfigurationError):
            evaluate(
                config,
                CameraId(uuid.uuid4()),
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_table_priority_referencing_missing_table_rejected(self) -> None:
        config = _lobby_with_tables()
        with pytest.raises(ReferenceIntegrityError, match="table_priority"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
                table_priority=("t-ghost",),
            )

    def test_duplicate_table_priority_rejected(self) -> None:
        config = _lobby_with_tables(
            tables=(_table("t-a", TABLE_A), _table("t-c", TABLE_C)),
            contained_tables=("t-a", "t-c"),
        )
        with pytest.raises(InvalidSpatialInputError, match="duplicate"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL),
                table_priority=("t-a", "t-a"),
            )

    def test_dangling_contained_tables_rejected(self) -> None:
        # The zone declares a contained table missing from the version.
        config = _version(
            cameras=(_camera(detection_zones=("z-lobby",)),),
            zones=(_zone_containing("z-lobby", ZONE_RECT, ("t-ghost",)),),
            tables=(_table("t-a", TABLE_A),),
        )
        with pytest.raises(ReferenceIntegrityError, match="contained table"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_ambiguous_overlapping_geometry_is_explicit(self) -> None:
        # Ambiguity is never collapsed into a silent first-pick — the
        # observation reports AMBIGUOUS and drops zone/table identity.
        config = _lobby_with_tables(
            tables=(_table("t-a", TABLE_A), _table("t-c", TABLE_C)),
            contained_tables=("t-a", "t-c"),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.AMBIGUOUS
        assert result.observation.zone_profile_id is None
        assert result.observation.table_profile_id is None


# =============================================================================
# 12 / 14. Determinism
# =============================================================================


class TestDeterminism:
    """Same inputs always produce an identical result."""

    def test_repeated_evaluation_is_identical(self) -> None:
        config = _lobby_with_tables(
            tables=(_table("t-a", TABLE_A), _table("t-c", TABLE_C)),
            contained_tables=("t-a", "t-c"),
        )
        point = _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL)
        track = _track()
        first = evaluate(
            config, config.cameras[0].camera_id, point, track=track, table_priority=("t-c", "t-a")
        )
        for _ in range(500):
            again = evaluate(
                config,
                config.cameras[0].camera_id,
                point,
                track=track,
                table_priority=("t-c", "t-a"),
            )
            assert again == first
            assert again.observation == first.observation

    def test_independent_of_table_row_ordering(self) -> None:
        shared_camera_id = CameraId(uuid.uuid4())
        shared_version_id = uuid.uuid4()
        cam = _camera(profile_id="cam-1", camera_id=shared_camera_id, detection_zones=("z-lobby",))
        zone_ordered = _zone_containing("z-lobby", ZONE_RECT, ("t-a", "t-c"))
        zone_reordered = _zone_containing("z-lobby", ZONE_RECT, ("t-c", "t-a"))
        table_a = _table("t-a", TABLE_A)
        table_c = _table("t-c", TABLE_C)
        config_ordered = _version(
            version_id=shared_version_id,
            cameras=(cam,),
            zones=(zone_ordered,),
            tables=(table_a, table_c),
        )
        config_reordered = _version(
            version_id=shared_version_id,
            cameras=(cam,),
            zones=(zone_reordered,),
            tables=(table_c, table_a),
        )
        point = _point(3.5, 3.5, space=CoordinateSpace.VENUE_LOCAL)
        track = _track()
        first = evaluate(config_ordered, config_ordered.cameras[0].camera_id, point, track=track)
        second = evaluate(
            config_reordered, config_reordered.cameras[0].camera_id, point, track=track
        )
        assert first.observation.status is SpatialStatus.AMBIGUOUS
        # Memberships are profile_id-sorted — independent of config order.
        assert [m.table_profile_id for m in first.table_memberships] == [
            m.table_profile_id for m in second.table_memberships
        ]
        assert first.observation == second.observation

    def test_no_current_time_dependence(self) -> None:
        config = _lobby_with_tables()
        event_time = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        track = _track(event_time=event_time)
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            track=track,
        )
        assert result.observation.event_time == event_time


# =============================================================================
# 15. SpatialObservation provenance
# =============================================================================


class TestObservationProvenance:
    """The canonical observation preserves the full reproduction chain."""

    def test_full_provenance_chain(self) -> None:
        track = _track()
        config = _lobby_with_tables()
        point = _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        result = evaluate(config, config.cameras[0].camera_id, point, track=track)
        obs = result.observation
        assert obs.session_id == track.session_id
        assert obs.track_id == track.track_id
        assert obs.frame_id == track.frame_id
        assert obs.event_time == track.event_time
        assert obs.camera_id == config.cameras[0].camera_id
        assert obs.configuration_version_id == config.configuration_version_id
        assert obs.spatial_point == point
        assert obs.status is SpatialStatus.INSIDE
        assert obs.zone_profile_id == "z-lobby"
        assert obs.table_profile_id == "t-a"
        assert obs.engine_version == SPATIAL_ENGINE_VERSION

    def test_engine_version_reflects_step5_semantics(self) -> None:
        # Interpretation semantics changed with table mapping — the
        # contract constant was bumped to 0.2.0 (see contracts.spatial)
        # and every observation carries the version that interpreted it.
        config = _lobby_with_tables()
        obs = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        ).observation
        assert obs.engine_version == SPATIAL_ENGINE_VERSION

    def test_observation_serializable_round_trip(self) -> None:
        config = _lobby_with_tables()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        data = result.observation.model_dump(mode="json")
        restored = SpatialObservation.model_validate(data)
        assert restored == result.observation


# =============================================================================
# 16. Integration: full deterministic chain (no I/O involved)
# =============================================================================


class TestFullChain:
    """Track provenance + point + engine produce the canonical observation."""

    def test_track_to_observation_chain(self) -> None:
        # The deterministic chain: TrackObservation -> canonical point ->
        # engine (exclusion -> zone -> table -> ambiguity) -> observation.
        # A VENUE_LOCAL point represents the Step 2 point policy output
        # after the documented camera->venue projection.
        session_id = VideoSessionId(uuid.uuid4())
        track_id = TrackId(uuid.uuid4())
        config = _lobby_with_tables()
        track = _track(session_id=session_id, track_id=track_id)
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(3.0, 3.0, space=CoordinateSpace.VENUE_LOCAL),
            track=track,
        )
        obs = result.observation
        assert obs.session_id == session_id
        assert obs.track_id == track_id
        assert obs.configuration_version_id == config.configuration_version_id
        assert obs.status is SpatialStatus.INSIDE
        assert obs.zone_profile_id == "z-lobby"
        assert obs.table_profile_id == "t-a"
        # The observation is reproducible from its own provenance.
        data = obs.model_dump(mode="json")
        assert SpatialObservation.model_validate(data) == obs

    def test_image_normalized_track_requires_projection(self) -> None:
        # ``extract_point`` (Step 2) emits IMAGE_NORMALIZED points. Against
        # venue tables this is the recorded projection blocker — never a
        # silent NO_MATCH from live tracks.
        config = _lobby_with_tables()
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(config, config.cameras[0].camera_id, _point(0.5, 0.5))
