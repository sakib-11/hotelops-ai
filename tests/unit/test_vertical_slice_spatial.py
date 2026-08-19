"""Task 18.6 — single ROI vertical slice.

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 14
spatial boundary:

    TrackObservation → evaluate_spatial → SpatialObservation (ROI membership)

The ROI is NOT hardcoded in processing code: it comes from the ONE
published Task 10 configuration version pinned in the fixture manifest
(``manifest.spatial``) — the same immutable version the engine requires
(never "the latest configuration").  The engine is pure and deterministic:
no database, no current time, no geometry recomputation inside the rule
layer.

The fixture declares its zone geometry in VENUE_LOCAL with a deterministic
1:1 venue mapping (venue-local == fixture pixels) and the centroid point
policy, so the golden spatial point (box center) and the golden status are
derived by the SAME rule that the engine evaluates.

Verified here (each against the pinned published configuration version):
- outside ROI          → OUTSIDE (no zone identity);
- entering ROI         → INSIDE (the first frame whose centroid is inside);
- inside ROI           → INSIDE with the zone profile id;
- exiting ROI          → OUTSIDE after the person leaves the zone;
- boundary point       → the documented blocker: a centroid exactly on the
                         ROI edge raises BoundaryPolicyUndefinedError
                         (never silently converted to INSIDE/OUTSIDE);
- exclusion behavior   → EXCLUDED when the pinned version declares an
                         exclusion ROI (policy-intercepted, precedence
                         over zones);
- wrong configuration  → a different/unpublished version is rejected
                         (typed error, never a silent fallback);
- provenance           → configuration_version, tenant, venue, camera and
                         zone identity preserved on the observation;
- the full connect      → the tracked detection's box is reduced to the
                         canonical point by the REAL Step 2 geometry layer
                         (``extract_point``) under the fixture's 1:1 venue
                         mapping, then evaluated — the derived evaluation
                         reproduces the manifest's golden status on every
                         frame (INSIDE on the inside interval, the typed
                         boundary blocker on the edge frame).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.intelligence.geometry import extract_point
from backend.app.intelligence.spatial.engine import (
    SpatialEvaluationInput,
    evaluate_spatial,
)
from backend.app.intelligence.spatial.exceptions import (
    BoundaryPolicyUndefinedError,
    CameraNotInConfigurationError,
    ConfigurationNotPublishedError,
)
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    DetectionId,
    FrameId,
    TrackId,
    VideoSessionId,
    new_uuid,
)
from contracts.configuration import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationStatus,
    ConfigurationVersionModel,
    ExclusionROIModel,
    ZoneModel,
    ZoneType,
)
from contracts.geometry import (
    CoordinateSpace,
    GeometryModel,
    GeometryScope,
    GeometryType,
)
from contracts.spatial import (
    SpatialObservation,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.vision import BoundingBox, TrackObservation, TrackState

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = FIXTURES_DIR / "manifest.json"

SCHEMA = "hotelops.vertical-slice/1.0"


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


# ---------------------------------------------------------------------------
# Configuration from the manifest — the ONE published version (never built
# in processing code; the ROI comes from Task 10 configuration).
# ---------------------------------------------------------------------------


def _zone_model(manifest: dict) -> ZoneModel:
    spatial = manifest["spatial"]
    coords = spatial["zone_geometry"]["coordinates"]
    return ZoneModel(
        profile_id=spatial["zone_profile_id"],
        name=spatial["zone_profile_id"],
        zone_type=ZoneType.LOBBY,
        geometry=GeometryModel(
            geometry_id=spatial["zone_geometry"]["geometry_id"],
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            geometry_scope=GeometryScope.VENUE,
            # The ring is closed deterministically by the GeometryModel.
            coordinates=[list(p) for p in coords],
        ),
    )


def _camera_model(
    manifest: dict,
    *,
    zone_profile_id: str | None = None,
    exclusion_rois: tuple[ExclusionROIModel, ...] = (),
) -> CameraProfileModel:
    spatial = manifest["spatial"]
    return CameraProfileModel(
        profile_id=spatial["camera_profile_id"],
        camera_id=CameraId(uuid.UUID(spatial["camera_id"])),
        camera_reference=spatial["camera_profile_id"],
        resolution_width=320,
        resolution_height=240,
        mount_type=CameraMountType.CEILING,
        detection_zones=[zone_profile_id or spatial["zone_profile_id"]],
        privacy_rois=[],
        exclusion_rois=[roi.profile_id for roi in exclusion_rois],
    )


def _published_configuration(
    manifest: dict,
    *,
    zone_profile_id: str | None = None,
    exclusion_rois: tuple[ExclusionROIModel, ...] = (),
    status: ConfigurationStatus = ConfigurationStatus.PUBLISHED,
) -> ConfigurationVersionModel:
    """The pinned PUBLISHED configuration version from the manifest."""
    spatial = manifest["spatial"]
    kwargs: dict = {
        "configuration_version_id": ConfigurationVersionId(
            uuid.UUID(spatial["configuration_version_id"])
        ),
        "configuration_id": uuid.UUID(spatial["configuration_version_id"]),
        "venue_id": uuid.UUID(spatial["venue_id"]),
        "tenant_id": uuid.UUID(spatial["tenant_id"]),
        "version": 1,
        "status": status,
        "cameras": [
            _camera_model(
                manifest,
                zone_profile_id=zone_profile_id,
                exclusion_rois=exclusion_rois,
            )
        ],
        "zones": [_zone_model(manifest)],
        "tables": [],
        "privacy_rois": [],
        "exclusion_rois": list(exclusion_rois),
    }
    if status is ConfigurationStatus.PUBLISHED:
        kwargs.update(
            validated_at=datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC),
            validated_by="validator",
            published_at=datetime(2026, 8, 1, 9, 5, 0, tzinfo=UTC),
            published_by="publisher",
        )
    return ConfigurationVersionModel(**kwargs)


def _exclusion_roi(manifest: dict, *, profile_id: str = "roi-exclusion") -> ExclusionROIModel:
    """An exclusion ROI declared in the pinned version, in the fixture's
    VENUE_LOCAL plane (venue-scoped — the evaluated point is VENUE_LOCAL,
    and cross-space ROI evaluation is forbidden by the engine).
    Occupies the venue-local square (0..64, 0..48)."""
    return ExclusionROIModel(
        profile_id=profile_id,
        name=profile_id,
        geometry=GeometryModel(
            geometry_id=f"g-{profile_id}",
            geometry_type=GeometryType.POLYGON,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            geometry_scope=GeometryScope.VENUE,
            coordinates=[
                [0.0, 0.0],
                [64.0, 0.0],
                [64.0, 48.0],
                [0.0, 48.0],
            ],
        ),
        excluded_tasks=["detection"],
    )


# ---------------------------------------------------------------------------
# Track + point from the fixture's golden trajectory
# ---------------------------------------------------------------------------


def _track(
    *,
    frame_index: int,
    event_time: datetime,
    session_id: VideoSessionId,
    camera_id: CameraId,
    configuration_version_id: ConfigurationVersionId,
) -> TrackObservation:
    return TrackObservation(
        track_id=TrackId(new_uuid()),
        detection_id=DetectionId(new_uuid()),
        frame_id=FrameId(new_uuid()),
        session_id=session_id,
        event_time=event_time,
        track_state=TrackState.ACTIVE,
    )


def _golden_point(manifest: dict, frame_index: int) -> SpatialPointModel | None:
    entry = manifest["timeline"][frame_index]
    point = entry["spatial_point"]
    if point is None:
        return None
    return SpatialPointModel(
        x=point["x"],
        y=point["y"],
        coordinate_space=CoordinateSpace.VENUE_LOCAL,
        policy=SpatialPointPolicy.CENTROID,
    )


def _evaluate(
    configuration: ConfigurationVersionModel,
    manifest: dict,
    frame_index: int,
) -> SpatialObservation:
    """Evaluate the fixture's golden centroid for one frame."""
    spatial = manifest["spatial"]
    point = _golden_point(manifest, frame_index)
    assert point is not None, f"frame {frame_index} has no golden spatial point"
    camera_id = CameraId(uuid.UUID(spatial["camera_id"]))
    track = _track(
        frame_index=frame_index,
        event_time=datetime.fromisoformat(manifest["timeline"][frame_index]["event_time"]),
        session_id=VideoSessionId(new_uuid()),
        camera_id=camera_id,
        configuration_version_id=ConfigurationVersionId(
            uuid.UUID(spatial["configuration_version_id"])
        ),
    )
    result = evaluate_spatial(
        SpatialEvaluationInput(
            configuration=configuration,
            track=track,
            camera_id=camera_id,
            point=point,
        )
    )
    return result.observation


# ---------------------------------------------------------------------------
# The slice: fixture golden tracks → spatial engine → ROI membership
# ---------------------------------------------------------------------------


class TestSingleRoiSlice:
    def test_inside_roi_frames(self) -> None:
        """The fixture's expected INSIDE frames evaluate to INSIDE with the
        zone identity — the exact ``track enters configured ROI → INSIDE``
        contract."""
        manifest = _load_manifest()
        configuration = _published_configuration(manifest)
        for entry in manifest["timeline"]:
            if entry["spatial_status"] != "inside":
                continue
            observation = _evaluate(configuration, manifest, entry["frame_index"])
            assert observation.status is SpatialStatus.INSIDE
            assert observation.zone_profile_id == manifest["spatial"]["zone_profile_id"]

    def test_entering_roi_first_inside_frame(self) -> None:
        """Frame 7 is the first frame whose centroid is strictly inside."""
        manifest = _load_manifest()
        configuration = _published_configuration(manifest)
        inside_frames = [
            entry["frame_index"]
            for entry in manifest["timeline"]
            if entry["spatial_status"] == "inside"
        ]
        assert inside_frames[0] == 7
        observation = _evaluate(configuration, manifest, 7)
        assert observation.status is SpatialStatus.INSIDE
        assert observation.zone_profile_id == manifest["spatial"]["zone_profile_id"]

    def test_outside_roi_before_entry(self) -> None:
        """A point clearly outside the zone (before the person enters) is
        OUTSIDE with no zone identity — never a fabricated INSIDE."""
        manifest = _load_manifest()
        configuration = _published_configuration(manifest)
        # A deterministic outside point: far from the ROI polygon.
        outside = SpatialPointModel(
            x=0.5,
            y=0.5,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        camera_id = CameraId(uuid.UUID(manifest["spatial"]["camera_id"]))
        track = _track(
            frame_index=0,
            event_time=datetime.fromisoformat(manifest["timeline"][0]["event_time"]),
            session_id=VideoSessionId(new_uuid()),
            camera_id=camera_id,
            configuration_version_id=ConfigurationVersionId(
                uuid.UUID(manifest["spatial"]["configuration_version_id"])
            ),
        )
        observation = evaluate_spatial(
            SpatialEvaluationInput(
                configuration=configuration,
                track=track,
                camera_id=camera_id,
                point=outside,
            )
        ).observation
        assert observation.status is SpatialStatus.OUTSIDE
        assert observation.zone_profile_id is None

    def test_exiting_roi_is_outside(self) -> None:
        """A point beyond the zone (person has walked out) is OUTSIDE."""
        manifest = _load_manifest()
        configuration = _published_configuration(manifest)
        # Beyond the ROI right edge (x > 280) in the fixture's venue plane.
        exited = SpatialPointModel(
            x=299.0,
            y=120.0,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        camera_id = CameraId(uuid.UUID(manifest["spatial"]["camera_id"]))
        track = _track(
            frame_index=28,
            event_time=datetime.fromisoformat(manifest["timeline"][28]["event_time"]),
            session_id=VideoSessionId(new_uuid()),
            camera_id=camera_id,
            configuration_version_id=ConfigurationVersionId(
                uuid.UUID(manifest["spatial"]["configuration_version_id"])
            ),
        )
        observation = evaluate_spatial(
            SpatialEvaluationInput(
                configuration=configuration,
                track=track,
                camera_id=camera_id,
                point=exited,
            )
        ).observation
        assert observation.status is SpatialStatus.OUTSIDE
        assert observation.zone_profile_id is None

    def test_boundary_point_is_never_silently_converted(self) -> None:
        """Frame 6's centroid lies exactly on the ROI edge — the engine
        raises the documented BoundaryPolicyUndefinedError instead of
        silently classifying it."""
        manifest = _load_manifest()
        configuration = _published_configuration(manifest)
        with pytest.raises(BoundaryPolicyUndefinedError):
            _evaluate(configuration, manifest, 6)

    def test_exclusion_roi_has_precedence(self) -> None:
        """A point inside an exclusion ROI declared by the pinned version
        is EXCLUDED (policy-intercepted) even inside the zone."""
        manifest = _load_manifest()
        configuration = _published_configuration(
            manifest, exclusion_rois=(_exclusion_roi(manifest),)
        )
        # The exclusion ROI occupies the venue-local square (0..64, 0..48)
        # — the fixture's top-left. Use a point inside it.
        excluded_point = SpatialPointModel(
            x=30.0,
            y=20.0,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        camera_id = CameraId(uuid.UUID(manifest["spatial"]["camera_id"]))
        track = _track(
            frame_index=8,
            event_time=datetime.fromisoformat(manifest["timeline"][8]["event_time"]),
            session_id=VideoSessionId(new_uuid()),
            camera_id=camera_id,
            configuration_version_id=ConfigurationVersionId(
                uuid.UUID(manifest["spatial"]["configuration_version_id"])
            ),
        )
        observation = evaluate_spatial(
            SpatialEvaluationInput(
                configuration=configuration,
                track=track,
                camera_id=camera_id,
                point=excluded_point,
            )
        ).observation
        assert observation.status is SpatialStatus.EXCLUDED
        assert observation.zone_profile_id is None

    def test_wrong_configuration_version_rejected(self) -> None:
        """A different/unpublished configuration version is a typed error —
        never a silent fallback to the latest."""
        manifest = _load_manifest()
        # Draft version (not published): the engine refuses.
        draft = _published_configuration(manifest, status=ConfigurationStatus.DRAFT)
        with pytest.raises(ConfigurationNotPublishedError):
            _evaluate(draft, manifest, 10)

    def test_unknown_camera_rejected(self) -> None:
        """A camera not in the pinned configuration is a typed error."""
        manifest = _load_manifest()
        configuration = _published_configuration(manifest)
        # Remove the camera from the version → CameraNotInConfigurationError.
        spatial = manifest["spatial"]
        stripped = configuration.model_copy(update={"cameras": []})
        point = _golden_point(manifest, 10)
        assert point is not None
        with pytest.raises(CameraNotInConfigurationError):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=stripped,
                    track=_track(
                        frame_index=10,
                        event_time=datetime.fromisoformat(manifest["timeline"][10]["event_time"]),
                        session_id=VideoSessionId(new_uuid()),
                        camera_id=CameraId(uuid.UUID(spatial["camera_id"])),
                        configuration_version_id=ConfigurationVersionId(
                            uuid.UUID(spatial["configuration_version_id"])
                        ),
                    ),
                    camera_id=CameraId(uuid.UUID(spatial["camera_id"])),
                    point=point,
                )
            )


# ---------------------------------------------------------------------------
# Provenance — every observation preserves the pinned identity
# ---------------------------------------------------------------------------


class TestSpatialProvenance:
    def test_observation_preserves_configuration_version_tenant_venue_camera(
        self,
    ) -> None:
        """The observation carries configuration_version_id, tenant/venue
        (via the pinned configuration), camera, and zone identity."""
        manifest = _load_manifest()
        spatial = manifest["spatial"]
        configuration = _published_configuration(manifest)
        observation = _evaluate(configuration, manifest, 15)
        assert observation.status is SpatialStatus.INSIDE
        assert observation.configuration_version_id == ConfigurationVersionId(
            uuid.UUID(spatial["configuration_version_id"])
        )
        assert observation.camera_id == CameraId(uuid.UUID(spatial["camera_id"]))
        assert observation.zone_profile_id == spatial["zone_profile_id"]
        # Tenant/venue are pinned by the configuration version itself.
        assert configuration.tenant_id == uuid.UUID(spatial["tenant_id"])
        assert configuration.venue_id == uuid.UUID(spatial["venue_id"])
        # Event time preserved verbatim.
        assert observation.event_time == datetime.fromisoformat(
            manifest["timeline"][15]["event_time"]
        )
        assert observation.event_time.tzinfo is not None


# ---------------------------------------------------------------------------
# The full connect: tracked box → Step 2 geometry layer → engine
# ---------------------------------------------------------------------------


class TestTrackToSpatialConnection:
    """The Task 18.6 connect through the REAL geometry layer.

    The tests above feed the manifest's precomputed golden centroid. These
    derive the canonical point from the tracked detection's bounding box
    via the Step 2 geometry layer (``extract_point``) and the fixture's
    declared 1:1 venue mapping (venue-local == fixture pixels, declared in
    the generator — the fixture is its own venue plane), then evaluate
    through the engine. This is the exact ``track enters configured ROI →
    spatial observation says INSIDE`` contract: point extraction is real,
    the ROI geometry comes from the ONE published Task 10 configuration
    version, and no geometry is invented in processing code.
    """

    @staticmethod
    def _venue_point(manifest: dict, frame_index: int) -> SpatialPointModel:
        """Derive the canonical VENUE_LOCAL centroid for a fixture frame.

        ``extract_point`` yields the IMAGE_NORMALIZED box centroid; the
        fixture's deterministic 1:1 venue mapping maps it to the
        VENUE_LOCAL plane the engine's zones are declared in.
        """
        det = manifest["timeline"][frame_index]["golden_detections"][0]
        width = manifest["metadata"]["width"]
        height = manifest["metadata"]["height"]
        normalized = extract_point(
            BoundingBox(
                x_min=det["x1"] / width,
                y_min=det["y1"] / height,
                x_max=det["x2"] / width,
                y_max=det["y2"] / height,
            ),
            SpatialPointPolicy.CENTROID,
        )
        return SpatialPointModel(
            x=normalized.x * width,
            y=normalized.y * height,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )

    def test_geometry_layer_reproduces_the_manifest_golden_point(self) -> None:
        """The extracted centroid equals the manifest's golden centroid on
        every present frame — the two paths can never drift apart."""
        manifest = _load_manifest()
        for entry in manifest["timeline"]:
            if not entry["person_present"]:
                continue
            golden = entry["spatial_point"]
            assert golden is not None
            derived = self._venue_point(manifest, entry["frame_index"])
            assert derived.x == pytest.approx(golden["x"])
            assert derived.y == pytest.approx(golden["y"])
            assert derived.coordinate_space is CoordinateSpace.VENUE_LOCAL
            assert derived.policy is SpatialPointPolicy.CENTROID

    def test_track_enters_configured_roi_and_is_inside(self) -> None:
        """Expected fixture: the entering track evaluates to INSIDE with the
        pinned zone identity — point derived from the tracked box, geometry
        from the ONE published Task 10 configuration version."""
        manifest = _load_manifest()
        spatial = manifest["spatial"]
        configuration = _published_configuration(manifest)
        camera_id = CameraId(uuid.UUID(spatial["camera_id"]))
        frame_index = manifest["trajectory"]["inside_roi_from"]
        observation = evaluate_spatial(
            SpatialEvaluationInput(
                configuration=configuration,
                track=_track(
                    frame_index=frame_index,
                    event_time=datetime.fromisoformat(
                        manifest["timeline"][frame_index]["event_time"]
                    ),
                    session_id=VideoSessionId(new_uuid()),
                    camera_id=camera_id,
                    configuration_version_id=ConfigurationVersionId(
                        uuid.UUID(spatial["configuration_version_id"])
                    ),
                ),
                camera_id=camera_id,
                point=self._venue_point(manifest, frame_index),
            )
        ).observation
        assert observation.status is SpatialStatus.INSIDE
        assert observation.zone_profile_id == spatial["zone_profile_id"]
        assert observation.configuration_version_id == ConfigurationVersionId(
            uuid.UUID(spatial["configuration_version_id"])
        )

    def test_derived_evaluation_matches_manifest_status_on_every_frame(self) -> None:
        """Sweep: for every present frame the derived-point evaluation
        matches the manifest's golden spatial status — INSIDE with the zone
        identity on the inside interval, the typed BOUNDARY blocker on the
        edge frame, never a silent conversion."""
        manifest = _load_manifest()
        spatial = manifest["spatial"]
        configuration = _published_configuration(manifest)
        camera_id = CameraId(uuid.UUID(spatial["camera_id"]))
        for entry in manifest["timeline"]:
            if not entry["person_present"]:
                continue
            frame_index = entry["frame_index"]
            evaluation = SpatialEvaluationInput(
                configuration=configuration,
                track=_track(
                    frame_index=frame_index,
                    event_time=datetime.fromisoformat(entry["event_time"]),
                    session_id=VideoSessionId(new_uuid()),
                    camera_id=camera_id,
                    configuration_version_id=ConfigurationVersionId(
                        uuid.UUID(spatial["configuration_version_id"])
                    ),
                ),
                camera_id=camera_id,
                point=self._venue_point(manifest, frame_index),
            )
            if entry["spatial_status"] == "inside":
                observation = evaluate_spatial(evaluation).observation
                assert observation.status is SpatialStatus.INSIDE
                assert observation.zone_profile_id == spatial["zone_profile_id"]
            else:
                # The documented boundary frame — a typed blocker, never a
                # silent INSIDE/OUTSIDE conversion.
                with pytest.raises(BoundaryPolicyUndefinedError):
                    evaluate_spatial(evaluation)


# ---------------------------------------------------------------------------
# ROI geometry must NOT be hardcoded in the detector/tracker layer
# ---------------------------------------------------------------------------


class TestNoHardcodedGeometry:
    def test_detector_and_tracker_import_no_spatial_geometry(self) -> None:
        """The ROI polygon lives in the Task 10 configuration — never in the
        detector/tracker/processing code."""
        import backend.app.intelligence.detectors.yolo_adapter as adapter_module
        import backend.app.intelligence.tracking.bytetrack_adapter as tracker_module

        for module in (adapter_module, tracker_module):
            source = Path(module.__file__).read_text()
            assert "zone" not in source
            assert "polygon" not in source
            assert "ROI" not in source

    def test_manifest_spatial_block_is_the_only_geometry_source(self) -> None:
        """The zone geometry in the fixture equals the manifest's published
        ROI — the slice never re-derives it."""
        manifest = _load_manifest()
        assert manifest["schema"] == SCHEMA
        spatial = manifest["spatial"]
        # The published zone polygon IS the fixture ROI polygon.
        assert spatial["zone_geometry"]["coordinates"] == manifest["roi"]["polygon"]
        assert spatial["zone_geometry"]["coordinate_space"] == "venue_local"
        assert spatial["point_policy"] == "centroid"
