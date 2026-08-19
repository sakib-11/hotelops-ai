"""Tests for the deterministic spatial evaluation engine (Task 14 Step 3).

Covers the full Step 3 scope:

- camera-scoped geometry (a track is evaluated only against the zones/
  ROIs its camera declares, inside the pinned configuration version);
- zone membership: INSIDE / OUTSIDE, boundary as a recorded blocker
  (never silently converted), multiple overlapping zones with and
  without an explicit priority;
- exclusion evaluation: EXCLUDED (provenance preserved, never
  deleted), privacy precedence (PRIVACY > EXCLUDED > zones);
- configuration-version provenance: the observation always carries the
  pinned immutable version — never the latest (V1 pinned after V2 is
  published must not change the historical result);
- multi-tenant/venue isolation through the passed configuration;
- determinism: identical input always produces an identical result, and
  the result is independent of database/list ordering;
- the pure-engine boundary: no I/O imports, no current-time reads.

Fixtures use the REAL Task 10 configuration contracts
(ConfigurationVersionModel / ZoneModel / CameraProfileModel /
ExclusionROIModel / PrivacyROIModel) and the canonical Step 2 point
contract (SpatialPointModel).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
    SpatialEvaluationError,
    VenuePointRequiredError,
)
from contracts.common import CameraId, DetectionId, FrameId, TrackId, VideoSessionId, new_uuid
from contracts.configuration import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationStatus,
    ConfigurationVersionModel,
    ExclusionROIModel,
    PrivacyROIModel,
    TableModel,
    ZoneModel,
    ZoneType,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryScope, GeometryType
from contracts.spatial import (
    SPATIAL_ENGINE_VERSION,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.vision import TrackObservation, TrackState

TENANT = uuid.uuid4()
VENUE = uuid.uuid4()
CONFIG = uuid.uuid4()

# Canonical venue zone: 10 x 10 square (VENUE_LOCAL).
ZONE_RECT = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
# Bottom-right quadrant of the unit square (IMAGE_NORMALIZED).
IMAGE_RECT = [[0.5, 0], [1, 0], [1, 0.5], [0.5, 0.5], [0.5, 0]]


# =============================================================================
# Fixture builders (real Task 10 contracts)
# =============================================================================


def _venue_polygon(coords: list[list[float]]) -> GeometryModel:
    return GeometryModel(
        geometry_id=f"g-{uuid.uuid4()}",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=CoordinateSpace.VENUE_LOCAL,
        geometry_scope=GeometryScope.VENUE,
        coordinates=[*coords, coords[0]],
    )


def _camera_polygon(coords: list[list[float]], cam_ref: str) -> GeometryModel:
    return GeometryModel(
        geometry_id=f"g-{uuid.uuid4()}",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
        geometry_scope=GeometryScope.CAMERA,
        reference_camera_profile_id=cam_ref,
        coordinates=[*coords, coords[0]],
    )


def _zone(profile_id: str, coords: list[list[float]]) -> ZoneModel:
    return ZoneModel(
        profile_id=profile_id,
        name=profile_id,
        zone_type=ZoneType.LOBBY,
        geometry=_venue_polygon(coords),
    )


def _exclusion(
    profile_id: str,
    coords: list[list[float]],
    *,
    space: CoordinateSpace = CoordinateSpace.IMAGE_NORMALIZED,
    cam_ref: str = "cam-1",
) -> ExclusionROIModel:
    geometry = (
        _camera_polygon(coords, cam_ref)
        if space is CoordinateSpace.IMAGE_NORMALIZED
        else _venue_polygon(coords)
    )
    return ExclusionROIModel(
        profile_id=profile_id,
        name=profile_id,
        geometry=geometry,
        excluded_tasks=["detection"],
    )


def _privacy(
    profile_id: str,
    coords: list[list[float]],
    *,
    space: CoordinateSpace = CoordinateSpace.IMAGE_NORMALIZED,
    cam_ref: str = "cam-1",
) -> PrivacyROIModel:
    geometry = (
        _camera_polygon(coords, cam_ref)
        if space is CoordinateSpace.IMAGE_NORMALIZED
        else _venue_polygon(coords)
    )
    return PrivacyROIModel(
        profile_id=profile_id,
        name=profile_id,
        geometry=geometry,
        privacy_action="blur",
    )


def _camera(
    *,
    profile_id: str = "cam-1",
    camera_id: CameraId | None = None,
    detection_zones: tuple[str, ...] = (),
    privacy_rois: tuple[str, ...] = (),
    exclusion_rois: tuple[str, ...] = (),
) -> CameraProfileModel:
    return CameraProfileModel(
        profile_id=profile_id,
        camera_id=camera_id or CameraId(uuid.uuid4()),
        camera_reference=profile_id,
        resolution_width=1920,
        resolution_height=1080,
        mount_type=CameraMountType.CEILING,
        detection_zones=list(detection_zones),
        privacy_rois=list(privacy_rois),
        exclusion_rois=list(exclusion_rois),
    )


def _version(
    *,
    version: int = 1,
    version_id: uuid.UUID | None = None,
    cameras: tuple[CameraProfileModel, ...] = (),
    zones: tuple[ZoneModel, ...] = (),
    tables: tuple[TableModel, ...] = (),
    privacy_rois: tuple[PrivacyROIModel, ...] = (),
    exclusion_rois: tuple[ExclusionROIModel, ...] = (),
    status: ConfigurationStatus = ConfigurationStatus.PUBLISHED,
    tenant_id: uuid.UUID = TENANT,
    venue_id: uuid.UUID = VENUE,
) -> ConfigurationVersionModel:
    kwargs: dict = {
        "configuration_version_id": version_id or uuid.uuid4(),
        "configuration_id": CONFIG,
        "venue_id": venue_id,
        "tenant_id": tenant_id,
        "version": version,
        "status": status,
        "cameras": list(cameras),
        "zones": list(zones),
        "tables": list(tables),
        "privacy_rois": list(privacy_rois),
        "exclusion_rois": list(exclusion_rois),
    }
    if status is ConfigurationStatus.PUBLISHED:
        kwargs.update(
            validated_at=datetime(2026, 7, 29, 11, 0, 0, tzinfo=UTC),
            validated_by="validator",
            published_at=datetime(2026, 7, 29, 11, 5, 0, tzinfo=UTC),
            published_by="publisher",
        )
    return ConfigurationVersionModel(**kwargs)


def _point(
    x: float,
    y: float,
    *,
    space: CoordinateSpace = CoordinateSpace.IMAGE_NORMALIZED,
    policy: SpatialPointPolicy = SpatialPointPolicy.FOOTPOINT,
) -> SpatialPointModel:
    return SpatialPointModel(x=x, y=y, coordinate_space=space, policy=policy)


def _track(
    *,
    session_id: VideoSessionId | None = None,
    track_id: TrackId | None = None,
    frame_id: FrameId | None = None,
    event_time: datetime | None = None,
) -> TrackObservation:
    return TrackObservation(
        track_id=track_id or TrackId(new_uuid()),
        detection_id=DetectionId(new_uuid()),
        frame_id=frame_id or FrameId(new_uuid()),
        session_id=session_id or VideoSessionId(new_uuid()),
        event_time=event_time or datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        track_state=TrackState.ACTIVE,
    )


def evaluate(
    configuration: ConfigurationVersionModel,
    camera_id: CameraId,
    point: SpatialPointModel,
    *,
    track: TrackObservation | None = None,
    zone_priority: tuple[str, ...] = (),
) -> SpatialEvaluationResult:
    """Run the pure engine with a canonical input (default track)."""
    return evaluate_spatial(
        SpatialEvaluationInput(
            configuration=configuration,
            track=track or _track(),
            camera_id=camera_id,
            point=point,
            zone_priority=zone_priority,
        )
    )


def _lobby_config(
    *,
    camera_id: CameraId | None = None,
    detection_zones: tuple[str, ...] = ("z-lobby",),
    zones: tuple[ZoneModel, ...] = (_zone("z-lobby", ZONE_RECT),),
    exclusion_rois: tuple[ExclusionROIModel, ...] = (),
    privacy_rois: tuple[PrivacyROIModel, ...] = (),
) -> ConfigurationVersionModel:
    camera = _camera(
        profile_id="cam-1",
        camera_id=camera_id,
        detection_zones=detection_zones,
        exclusion_rois=tuple(r.profile_id for r in exclusion_rois),
        privacy_rois=tuple(r.profile_id for r in privacy_rois),
    )
    return _version(
        cameras=(camera,), zones=zones, exclusion_rois=exclusion_rois, privacy_rois=privacy_rois
    )


# =============================================================================
# 1. Zone membership (INSIDE / OUTSIDE)
# =============================================================================


class TestZoneMembership:
    """A point is classified against the zones ITS camera declares."""

    def test_point_clearly_inside_one_zone(self) -> None:
        config = _lobby_config()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"
        assert [m.zone_profile_id for m in result.zone_memberships] == ["z-lobby"]
        assert result.zone_memberships[0].location.value == "inside"

    def test_point_clearly_outside_every_zone(self) -> None:
        config = _lobby_config()
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(50.0, 50.0, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.OUTSIDE
        assert result.observation.zone_profile_id is None
        assert result.zone_memberships[0].location.value == "outside"

    def test_no_matching_zone_when_camera_declares_none(self) -> None:
        config = _version(cameras=(_camera(),))
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.OUTSIDE
        assert result.observation.zone_profile_id is None
        assert result.zone_memberships == ()

    def test_point_inside_undeclared_zone_is_outside(self) -> None:
        # Zone B exists in the version but camera A does not declare it —
        # camera-scoped geometry must never leak across cameras.
        zone_b = _zone("z-other", [[20, 20], [30, 20], [30, 30], [20, 30], [20, 20]])
        config = _version(
            cameras=(_camera(profile_id="cam-1", detection_zones=("z-lobby",)),),
            zones=(_zone("z-lobby", ZONE_RECT), zone_b),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(25.0, 25.0, space=CoordinateSpace.VENUE_LOCAL),
        )
        assert result.observation.status is SpatialStatus.OUTSIDE
        # Only the camera-declared zone was evaluated.
        assert [m.zone_profile_id for m in result.zone_memberships] == ["z-lobby"]


# =============================================================================
# 3 / 6. Boundary policy (recorded blocker — never silently converted)
# =============================================================================


class TestBoundaryPolicy:
    """BOUNDARY has no Task 10 policy — the engine records the blocker."""

    def test_point_on_zone_boundary_raises_typed_blocker(self) -> None:
        config = _lobby_config()
        with pytest.raises(BoundaryPolicyUndefinedError, match="boundary"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(5.0, 0.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_point_on_zone_vertex_raises_typed_blocker(self) -> None:
        config = _lobby_config()
        with pytest.raises(BoundaryPolicyUndefinedError, match="boundary"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(0.0, 0.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_point_on_exclusion_boundary_raises_typed_blocker(self) -> None:
        config = _lobby_config(exclusion_rois=(_exclusion("x-door", IMAGE_RECT),))
        with pytest.raises(BoundaryPolicyUndefinedError, match="boundary"):
            evaluate(config, config.cameras[0].camera_id, _point(0.5, 0.25))

    def test_boundary_is_never_silently_converted(self) -> None:
        config = _lobby_config()
        with pytest.raises(SpatialEvaluationError) as exc_info:
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(5.0, 0.0, space=CoordinateSpace.VENUE_LOCAL),
            )
        assert not isinstance(exc_info.value, InvalidSpatialInputError)


# =============================================================================
# 4 / 5. Exclusion evaluation
# =============================================================================


class TestExclusionEvaluation:
    """Exclusion geometry is evaluated separately from operational zones."""

    def test_point_inside_exclusion_zone(self) -> None:
        config = _lobby_config(exclusion_rois=(_exclusion("x-door", IMAGE_RECT),))
        result = evaluate(config, config.cameras[0].camera_id, _point(0.75, 0.25))
        assert result.observation.status is SpatialStatus.EXCLUDED
        assert result.observation.zone_profile_id is None
        assert result.matched_exclusion_roi_profile_id == "x-door"

    def test_point_inside_operational_zone_but_outside_exclusion(self) -> None:
        # Venue-scoped exclusion far away from the zone; the venue point
        # is evaluated against both, then zone membership applies.
        config = _lobby_config(
            exclusion_rois=(
                _exclusion(
                    "x-far",
                    [[50, 50], [51, 50], [51, 51], [50, 51]],
                    space=CoordinateSpace.VENUE_LOCAL,
                ),
            )
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-lobby"
        assert result.matched_exclusion_roi_profile_id is None

    def test_exclusion_has_precedence_over_zones(self) -> None:
        # The exclusion ROI covers the entire zone — EXCLUDED wins.
        config = _lobby_config(
            exclusion_rois=(_exclusion("x-whole", ZONE_RECT, space=CoordinateSpace.VENUE_LOCAL),)
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.EXCLUDED
        assert result.observation.zone_profile_id is None

    def test_privacy_has_precedence_over_zones(self) -> None:
        # Point inside a venue-scoped privacy ROI AND a zone: PRIVACY,
        # no zone identity (privacy is supreme per INV-GEO-07).
        config = _lobby_config(
            privacy_rois=(_privacy("p-zone", ZONE_RECT, space=CoordinateSpace.VENUE_LOCAL),),
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.PRIVACY
        assert result.observation.zone_profile_id is None
        assert result.matched_privacy_roi_profile_id == "p-zone"

    def test_privacy_has_precedence_over_exclusion(self) -> None:
        # Point inside BOTH a privacy ROI and an exclusion ROI: PRIVACY.
        config = _lobby_config(
            privacy_rois=(_privacy("p-face", IMAGE_RECT),),
            exclusion_rois=(_exclusion("x-door", IMAGE_RECT),),
        )
        result = evaluate(config, config.cameras[0].camera_id, _point(0.75, 0.25))
        assert result.observation.status is SpatialStatus.PRIVACY
        assert result.matched_privacy_roi_profile_id == "p-face"
        assert result.matched_exclusion_roi_profile_id is None

    def test_excluded_observation_is_not_deleted(self) -> None:
        # The provenance chain must survive exclusion (never deleted).
        track = _track()
        config = _lobby_config(exclusion_rois=(_exclusion("x-door", IMAGE_RECT),))
        result = evaluate(config, config.cameras[0].camera_id, _point(0.75, 0.25), track=track)
        obs = result.observation
        assert obs.status is SpatialStatus.EXCLUDED
        assert obs.session_id == track.session_id
        assert obs.track_id == track.track_id
        assert obs.frame_id == track.frame_id
        assert obs.event_time == track.event_time

    def test_roi_of_other_camera_is_not_applied(self) -> None:
        # Camera B declares the exclusion ROI; camera A must ignore it —
        # the SAME point is EXCLUDED for B and INSIDE for A.
        cam_a = _camera(profile_id="cam-a", detection_zones=("z-lobby",))
        cam_b = _camera(
            profile_id="cam-b",
            detection_zones=("z-lobby",),
            exclusion_rois=("x-b-only",),
        )
        config = _version(
            cameras=(cam_a, cam_b),
            zones=(_zone("z-lobby", ZONE_RECT),),
            exclusion_rois=(_exclusion("x-b-only", ZONE_RECT, space=CoordinateSpace.VENUE_LOCAL),),
        )
        point = _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        result_a = evaluate(config, cam_a.camera_id, point)
        result_b = evaluate(config, cam_b.camera_id, point)
        assert result_a.observation.status is SpatialStatus.INSIDE
        assert result_a.matched_exclusion_roi_profile_id is None
        assert result_b.observation.status is SpatialStatus.EXCLUDED
        assert result_b.matched_exclusion_roi_profile_id == "x-b-only"


# =============================================================================
# 6 / 11 / 12. Overlapping zones
# =============================================================================


class TestOverlappingZones:
    """Deterministic overlap handling without invented hotel semantics."""

    def test_multiple_matching_zones_without_priority_is_ambiguous(self) -> None:
        zone_b = _zone("z-b", [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]])
        config = _lobby_config(
            detection_zones=("z-lobby", "z-b"),
            zones=(_zone("z-lobby", ZONE_RECT), zone_b),
        )
        result = evaluate(
            config, config.cameras[0].camera_id, _point(7.5, 7.5, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.status is SpatialStatus.AMBIGUOUS
        # Per the SpatialObservation contract, AMBIGUOUS carries no zone
        # identity — but the engine result preserves all matches for audit.
        assert result.observation.zone_profile_id is None
        assert sorted(m.zone_profile_id for m in result.zone_memberships) == ["z-b", "z-lobby"]
        assert all(m.location.value == "inside" for m in result.zone_memberships)

    def test_multiple_matching_zones_with_explicit_priority(self) -> None:
        zone_b = _zone("z-b", [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]])
        config = _lobby_config(
            detection_zones=("z-lobby", "z-b"),
            zones=(_zone("z-lobby", ZONE_RECT), zone_b),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(7.5, 7.5, space=CoordinateSpace.VENUE_LOCAL),
            zone_priority=("z-b", "z-lobby"),
        )
        assert result.observation.status is SpatialStatus.INSIDE
        assert result.observation.zone_profile_id == "z-b"

    def test_priority_list_that_matches_nothing_stays_ambiguous(self) -> None:
        zone_b = _zone("z-b", [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]])
        zone_c = _zone("z-c", [[100, 100], [110, 100], [110, 110], [100, 110], [100, 100]])
        config = _lobby_config(
            detection_zones=("z-lobby", "z-b", "z-c"),
            zones=(_zone("z-lobby", ZONE_RECT), zone_b, zone_c),
        )
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(7.5, 7.5, space=CoordinateSpace.VENUE_LOCAL),
            zone_priority=("z-c",),
        )
        assert result.observation.status is SpatialStatus.AMBIGUOUS

    def test_priority_referencing_undeclared_zone_rejected(self) -> None:
        config = _lobby_config()
        with pytest.raises(ReferenceIntegrityError, match="zone_priority"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
                zone_priority=("z-ghost",),
            )

    def test_duplicate_priority_rejected(self) -> None:
        zone_b = _zone("z-b", [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]])
        config = _lobby_config(
            detection_zones=("z-lobby", "z-b"),
            zones=(_zone("z-lobby", ZONE_RECT), zone_b),
        )
        with pytest.raises(InvalidSpatialInputError, match="duplicate"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(7.5, 7.5, space=CoordinateSpace.VENUE_LOCAL),
                zone_priority=("z-lobby", "z-lobby"),
            )


# =============================================================================
# 7. Camera isolation
# =============================================================================


class TestCameraIsolation:
    """A track is never evaluated against another camera's geometry."""

    def test_camera_a_zones_never_applied_to_camera_b(self) -> None:
        cam_a = _camera(profile_id="cam-a", detection_zones=("z-a",))
        cam_b = _camera(profile_id="cam-b", detection_zones=("z-b",))
        config = _version(
            cameras=(cam_a, cam_b),
            zones=(
                _zone("z-a", [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]),
                _zone("z-b", [[20, 20], [30, 20], [30, 30], [20, 30], [20, 20]]),
            ),
        )
        # Same track point, same version — only the camera changes.
        point = _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        result_a = evaluate(config, cam_a.camera_id, point)
        result_b = evaluate(config, cam_b.camera_id, point)
        assert result_a.observation.status is SpatialStatus.INSIDE
        assert result_a.observation.zone_profile_id == "z-a"
        assert result_b.observation.status is SpatialStatus.OUTSIDE
        assert result_b.observation.zone_profile_id is None

    def test_unknown_camera_rejected(self) -> None:
        config = _lobby_config()
        with pytest.raises(CameraNotInConfigurationError, match="not configured"):
            evaluate(
                config, CameraId(uuid.uuid4()), _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
            )

    def test_wrong_camera_configuration_rejected(self) -> None:
        # A camera that exists only in ANOTHER version is rejected here.
        config = _lobby_config()
        other = _lobby_config()
        with pytest.raises(CameraNotInConfigurationError):
            evaluate(
                config,
                other.cameras[0].camera_id,
                _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_mismatched_session_camera_never_cross_evaluates(self) -> None:
        # Section 13 "mismatched session/camera": the engine enforces it
        # by refusing to evaluate a track under a camera it does not
        # match — camera B's evaluation NEVER consults camera A's zones.
        cam_a = _camera(profile_id="cam-a", detection_zones=("z-a",))
        cam_b = _camera(profile_id="cam-b", detection_zones=("z-b",))
        config = _version(
            cameras=(cam_a, cam_b),
            zones=(
                _zone("z-a", [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]),
                _zone("z-b", [[20, 20], [30, 20], [30, 30], [20, 30], [20, 30]]),
            ),
        )
        track_a = _track()  # a track observed by camera A
        point = _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        # Evaluated under camera A: inside A's zone.
        assert (
            evaluate(config, cam_a.camera_id, point, track=track_a).observation.status
            is SpatialStatus.INSIDE
        )
        # Evaluated under camera B (the mismatch): A's geometry is never
        # consulted — the same track/point is OUTSIDE B's zones.
        mismatched = evaluate(config, cam_b.camera_id, point, track=track_a)
        assert mismatched.observation.status is SpatialStatus.OUTSIDE
        assert mismatched.observation.zone_profile_id is None


# =============================================================================
# 8 / 9. Configuration-version provenance (never the latest)
# =============================================================================


class TestConfigurationProvenance:
    """The pinned immutable version drives every result — never the latest."""

    def _two_versions_with_moved_zone(
        self,
    ) -> tuple[ConfigurationVersionModel, ConfigurationVersionModel]:
        camera_id = CameraId(uuid.uuid4())
        cam = _camera(profile_id="cam-1", camera_id=camera_id, detection_zones=("z-lobby",))
        v1 = _version(
            version=1,
            cameras=(cam,),
            zones=(_zone("z-lobby", [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]),),
        )
        v2 = _version(
            version=2,
            cameras=(cam,),
            zones=(_zone("z-lobby", [[20, 20], [30, 20], [30, 30], [20, 30], [20, 20]]),),
        )
        return v1, v2

    def test_observation_carries_pinned_configuration_version(self) -> None:
        config = _lobby_config()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        assert result.observation.configuration_version_id == config.configuration_version_id

    def test_old_session_pinned_to_v1_after_v2_published(self) -> None:
        v1, v2 = self._two_versions_with_moved_zone()
        # The point is inside v1's zone and outside v2's zone.
        point = _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        track = _track()
        result_v1 = evaluate(v1, v1.cameras[0].camera_id, point, track=track)
        assert result_v1.observation.status is SpatialStatus.INSIDE
        assert result_v1.observation.configuration_version_id == v1.configuration_version_id

        # v2 exists for the same camera — the historical result is unchanged.
        result_v2 = evaluate(v2, v2.cameras[0].camera_id, point, track=track)
        assert result_v2.observation.status is SpatialStatus.OUTSIDE
        assert result_v2.observation.configuration_version_id == v2.configuration_version_id
        # Re-evaluating against v1 is still INSIDE — no fallback to latest.
        replay = evaluate(v1, v1.cameras[0].camera_id, point, track=track)
        assert replay.observation == result_v1.observation

    def test_non_published_configuration_rejected(self) -> None:
        for status in (
            ConfigurationStatus.DRAFT,
            ConfigurationStatus.VALIDATING,
            ConfigurationStatus.VALIDATED,
        ):
            config = _version(
                status=status,
                cameras=(_camera(detection_zones=("z-lobby",)),),
                zones=(_zone("z-lobby", ZONE_RECT),),
            )
            with pytest.raises(ConfigurationNotPublishedError, match="PUBLISHED"):
                evaluate(
                    config,
                    config.cameras[0].camera_id,
                    _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
                )


# =============================================================================
# 8. SpatialObservation mapping / provenance
# =============================================================================


class TestObservationMapping:
    """The canonical observation carries the full provenance chain."""

    def test_provenance_chain_preserved(self) -> None:
        track = _track()
        config = _lobby_config()
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
            track=track,
        )
        obs = result.observation
        assert obs.session_id == track.session_id
        assert obs.track_id == track.track_id
        assert obs.frame_id == track.frame_id
        assert obs.event_time == track.event_time
        assert obs.camera_id == config.cameras[0].camera_id
        assert obs.configuration_version_id == config.configuration_version_id
        assert obs.engine_version == SPATIAL_ENGINE_VERSION
        assert obs.table_profile_id is None  # table mapping is a later step

    def test_spatial_point_carried_verbatim(self) -> None:
        # Inside the camera-scoped exclusion ROI -> EXCLUDED; the point
        # is still carried verbatim (never mutated or re-projected).
        point = _point(0.75, 0.25, policy=SpatialPointPolicy.FOOTPOINT)
        config = _lobby_config(exclusion_rois=(_exclusion("x-door", IMAGE_RECT),))
        result = evaluate(config, config.cameras[0].camera_id, point)
        assert result.observation.status is SpatialStatus.EXCLUDED
        assert result.observation.spatial_point == point
        assert result.observation.spatial_point.policy is SpatialPointPolicy.FOOTPOINT

    def test_observation_serializable(self) -> None:
        from contracts.spatial import SpatialObservation

        config = _lobby_config()
        result = evaluate(
            config, config.cameras[0].camera_id, _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        )
        data = result.observation.model_dump(mode="json")
        restored = SpatialObservation.model_validate(data)
        assert restored == result.observation


# =============================================================================
# 13. Negative tests
# =============================================================================


class TestNegativeCases:
    """Malformed or missing inputs fail with typed errors — never repaired."""

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(InvalidSpatialInputError, match="SpatialEvaluationInput"):
            evaluate_spatial(None)  # type: ignore[arg-type]
        with pytest.raises(InvalidSpatialInputError, match="configuration"):
            SpatialEvaluationInput(
                configuration=None,  # type: ignore[arg-type]
                track=_track(),
                camera_id=CameraId(uuid.uuid4()),
                point=_point(0.5, 0.5),
            )

    def test_wrong_configuration_type_rejected(self) -> None:
        with pytest.raises(InvalidSpatialInputError, match="configuration"):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration="not-a-config",  # type: ignore[arg-type]
                    track=_track(),
                    camera_id=CameraId(uuid.uuid4()),
                    point=_point(0.5, 0.5),
                )
            )

    def test_missing_track_rejected(self) -> None:
        config = _lobby_config()
        with pytest.raises(InvalidSpatialInputError, match="track"):
            SpatialEvaluationInput(
                configuration=config,
                track=None,  # type: ignore[arg-type]
                camera_id=config.cameras[0].camera_id,
                point=_point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_missing_camera_rejected(self) -> None:
        config = _lobby_config()
        with pytest.raises(InvalidSpatialInputError, match="camera_id"):
            SpatialEvaluationInput(
                configuration=config,
                track=_track(),
                camera_id=None,  # type: ignore[arg-type]
                point=_point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_wrong_typed_inputs_rejected_by_the_engine(self) -> None:
        config = _lobby_config()
        camera_id = config.cameras[0].camera_id
        with pytest.raises(InvalidSpatialInputError, match="track"):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=config,
                    track="not-a-track",  # type: ignore[arg-type]
                    camera_id=camera_id,
                    point=_point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
                )
            )
        with pytest.raises(InvalidSpatialInputError, match="camera_id"):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=config,
                    track=_track(),
                    camera_id="not-a-camera",  # type: ignore[arg-type]
                    point=_point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
                )
            )
        with pytest.raises(InvalidSpatialInputError, match="point"):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=config,
                    track=_track(),
                    camera_id=camera_id,
                    point="not-a-point",  # type: ignore[arg-type]
                )
            )

    def test_invalid_point_is_not_masked_by_config_traversal(self) -> None:
        # A NaN point must raise the input error even when the camera
        # also declares a missing reference — input validation precedes
        # configuration traversal (deterministic failure ordering).
        nan_point = SpatialPointModel(
            x=float("nan"),
            y=0.5,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        broken = _version(cameras=(_camera(detection_zones=("z-ghost",)),))
        with pytest.raises(InvalidSpatialInputError, match="finite"):
            evaluate(broken, broken.cameras[0].camera_id, nan_point)

    def test_invalid_point_rejected(self) -> None:
        config = _lobby_config()
        nan_point = SpatialPointModel(
            x=float("nan"),
            y=0.5,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        with pytest.raises(InvalidSpatialInputError, match="finite"):
            evaluate(config, config.cameras[0].camera_id, nan_point)

    def test_malformed_zone_definition_rejected(self) -> None:
        # A zone whose geometry is a POINT violates the zone contract.
        bad_zone = ZoneModel(
            profile_id="z-bad",
            name="bad",
            zone_type=ZoneType.CUSTOM,
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.POINT,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[1, 1]],
            ),
        )
        config = _version(cameras=(_camera(detection_zones=("z-bad",)),), zones=(bad_zone,))
        with pytest.raises(ReferenceIntegrityError, match="zones must be POLYGON"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(1.0, 1.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_invalid_polygon_ring_rejected(self) -> None:
        # A zero-area ring passes the pydantic contract but fails the
        # Step 2 polygon validation re-asserted by the engine.
        degenerate = _zone("z-bad", [[0, 0], [1, 0], [2, 0], [0, 0]])
        config = _version(cameras=(_camera(detection_zones=("z-bad",)),), zones=(degenerate,))
        with pytest.raises(ReferenceIntegrityError, match="failed validation"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(0.5, 0.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_missing_zone_reference_rejected(self) -> None:
        config = _version(cameras=(_camera(detection_zones=("z-ghost",)),))
        with pytest.raises(ReferenceIntegrityError, match="not present"):
            evaluate(
                config,
                config.cameras[0].camera_id,
                _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
            )

    def test_missing_exclusion_reference_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        missing = _version(
            cameras=(
                _camera(profile_id="cam-1", camera_id=cam.camera_id, exclusion_rois=("x-ghost",)),
            ),
            zones=(_zone("z-lobby", ZONE_RECT),),
        )
        with pytest.raises(ReferenceIntegrityError, match="not present"):
            evaluate(missing, cam.camera_id, _point(0.75, 0.25))

    def test_missing_privacy_reference_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        missing = _version(
            cameras=(
                _camera(profile_id="cam-1", camera_id=cam.camera_id, privacy_rois=("p-ghost",)),
            ),
            zones=(_zone("z-lobby", ZONE_RECT),),
        )
        with pytest.raises(ReferenceIntegrityError, match="not present"):
            evaluate(missing, cam.camera_id, _point(0.75, 0.25))

    def test_image_point_with_declared_zones_requires_venue_point(self) -> None:
        config = _lobby_config()
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(config, config.cameras[0].camera_id, _point(0.5, 0.5))

    def test_image_point_with_venue_exclusion_roi_requires_venue_point(self) -> None:
        config = _lobby_config(
            exclusion_rois=(
                _exclusion(
                    "x-venue",
                    [[20, 20], [21, 20], [21, 21], [20, 21]],
                    space=CoordinateSpace.VENUE_LOCAL,
                ),
            ),
        )
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(config, config.cameras[0].camera_id, _point(0.5, 0.5))

    def test_image_point_with_no_venue_geometry_is_outside(self) -> None:
        # Camera declares no zones and no venue-scoped ROIs: OUTSIDE is
        # a legitimate result, not a masked engine failure.
        config = _version(cameras=(_camera(),))
        result = evaluate(config, config.cameras[0].camera_id, _point(0.25, 0.25))
        assert result.observation.status is SpatialStatus.OUTSIDE


# =============================================================================
# 10. Multi-tenant / venue isolation
# =============================================================================


class TestTenantVenueIsolation:
    """The engine evaluates ONLY against the configuration it is given."""

    def test_same_camera_id_in_different_tenants_is_isolated(self) -> None:
        camera_id = CameraId(uuid.uuid4())
        zone_a = _zone("z-lobby", [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        zone_b = _zone("z-lobby", [[20, 20], [30, 20], [30, 30], [20, 30], [20, 30]])
        config_a = _version(
            tenant_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            cameras=(_camera(camera_id=camera_id, detection_zones=("z-lobby",)),),
            zones=(zone_a,),
        )
        config_b = _version(
            tenant_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            cameras=(_camera(camera_id=camera_id, detection_zones=("z-lobby",)),),
            zones=(zone_b,),
        )
        point = _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        result_a = evaluate(config_a, camera_id, point)
        result_b = evaluate(config_b, camera_id, point)
        # The same physical camera + point classifies by the config in
        # force — tenant A's geometry never leaks into tenant B.
        assert result_a.observation.status is SpatialStatus.INSIDE
        assert result_b.observation.status is SpatialStatus.OUTSIDE
        assert result_a.observation.configuration_version_id == config_a.configuration_version_id
        assert result_b.observation.configuration_version_id == config_b.configuration_version_id


# =============================================================================
# 14. Determinism
# =============================================================================


class TestDeterminism:
    """Same inputs always produce an identical result."""

    def test_repeated_evaluation_is_identical(self) -> None:
        config = _lobby_config()
        point = _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL)
        track = _track()
        first = evaluate(config, config.cameras[0].camera_id, point, track=track)
        for _ in range(1000):
            again = evaluate(config, config.cameras[0].camera_id, point, track=track)
            assert again == first
            assert again.observation == first.observation

    def test_independent_of_database_row_ordering(self) -> None:
        zone_a = _zone("z-a", [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        zone_b = _zone("z-b", [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]])
        shared_camera_id = CameraId(uuid.uuid4())
        shared_version_id = uuid.uuid4()
        cam_a = _camera(
            profile_id="cam-1", camera_id=shared_camera_id, detection_zones=("z-a", "z-b")
        )
        cam_b = _camera(
            profile_id="cam-1", camera_id=shared_camera_id, detection_zones=("z-b", "z-a")
        )
        config_ordered = _version(
            version_id=shared_version_id, cameras=(cam_a,), zones=(zone_a, zone_b)
        )
        config_reordered = _version(
            version_id=shared_version_id, cameras=(cam_b,), zones=(zone_b, zone_a)
        )
        point = _point(7.5, 7.5, space=CoordinateSpace.VENUE_LOCAL)
        track = _track()
        first = evaluate(config_ordered, config_ordered.cameras[0].camera_id, point, track=track)
        second = evaluate(
            config_reordered, config_reordered.cameras[0].camera_id, point, track=track
        )
        assert first.observation.status is SpatialStatus.AMBIGUOUS
        assert [m.zone_profile_id for m in first.zone_memberships] == [
            m.zone_profile_id for m in second.zone_memberships
        ]
        assert first.observation == second.observation

    def test_no_current_time_dependence(self) -> None:
        config = _lobby_config()
        event_time = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        track = _track(event_time=event_time)
        result = evaluate(
            config,
            config.cameras[0].camera_id,
            _point(5.0, 5.0, space=CoordinateSpace.VENUE_LOCAL),
            track=track,
        )
        # The observation time is the track's time — never a clock read.
        assert result.observation.event_time == event_time


# =============================================================================
# 11. Pure engine boundary
# =============================================================================


class TestEnginePurity:
    """The spatial package performs no I/O and has no hidden state."""

    def test_no_io_imports_in_spatial_package(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "spatial"
        )
        forbidden = [
            "sqlalchemy",
            "redis",
            "httpx",
            "boto3",
            "botocore",
            "openai",
            "anthropic",
            "urllib",
            "requests",
            "socket",
            "asyncio",
            "random",
            "time",
            "datetime",
        ]
        for path in sorted(package_dir.glob("*.py")):
            text = path.read_text()
            for module in forbidden:
                assert not re.search(rf"^\s*(from|import)\s+{module}\b", text, re.MULTILINE), (
                    f"I/O/stateful module {module!r} leaked into {path.name}"
                )

    def test_no_print_or_debug_leftovers(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "spatial"
        )
        for path in sorted(package_dir.glob("*.py")):
            text = path.read_text()
            assert "print(" not in text, f"print() leaked into {path.name}"
            assert "TODO" not in text and "FIXME" not in text, f"TODO/FIXME leaked into {path.name}"
