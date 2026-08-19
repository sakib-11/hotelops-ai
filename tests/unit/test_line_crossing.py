"""Tests for the deterministic line-crossing / spatial transition engine (Task 14 Step 4).

Covers the full Step 4 scope:

- crossing detection: CROSSED / NO_CROSSING via proper segment
  intersection (closeness to the line is never a crossing);
- side/direction policy: deterministic signed-side determination,
  FORWARD / REVERSE only when the line declares directional semantics,
  UNKNOWN otherwise;
- boundary cases (section 7): previous/current/both on line, same
  side, opposite sides, collinear overlap, endpoint touch — all
  tolerance-driven, never floating-point-random;
- same-track + session scope, frame-order validation (duplicate frame,
  timestamp regression; intermediate gaps allowed per Task 13);
- configuration-version provenance (pinned immutable version, never
  latest) and camera isolation;
- the complete deterministic chain: TrackObservation -> point
  extraction (Step 2) -> line-crossing evaluator -> canonical
  transition observation (no YOLO/ByteTrack/Redis/DB/LLM);
- determinism and the pure-engine boundary.

Fixtures use the REAL Task 10 configuration contracts
(ConfigurationVersionModel / CameraProfileModel / EntranceModel) and
the canonical Step 2 point contract (SpatialPointModel).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.intelligence.geometry import GEOMETRY_TOLERANCE, extract_point
from backend.app.intelligence.spatial import (
    LineCrossingInput,
    evaluate_line_crossing,
)
from backend.app.intelligence.spatial.exceptions import (
    CameraNotInConfigurationError,
    ConfigurationNotPublishedError,
    InvalidLineError,
    InvalidSpatialInputError,
    LineNotApplicableError,
    ReferenceIntegrityError,
    TransitionOrderError,
    TransitionScopeError,
    VenuePointRequiredError,
)
from contracts.common import CameraId, DetectionId, FrameId, TrackId, VideoSessionId, new_uuid
from contracts.configuration import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationStatus,
    ConfigurationVersionModel,
    EntranceDirection,
    EntranceModel,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryScope, GeometryType
from contracts.spatial import (
    SPATIAL_ENGINE_VERSION,
    CrossingDirection,
    CrossingState,
    LineCrossingObservation,
    SpatialPointModel,
    SpatialPointPolicy,
)
from contracts.vision import BoundingBox, TrackObservation, TrackState

TENANT = uuid.uuid4()
VENUE = uuid.uuid4()
CONFIG = uuid.uuid4()

# Canonical venue threshold: horizontal line from (0, 5) to (10, 5).
LINE_HORIZONTAL = [[0, 5], [10, 5]]


# =============================================================================
# Fixture builders (real Task 10 contracts)
# =============================================================================


def _venue_line(
    profile_id: str,
    coords: list[list[float]],
    *,
    direction: EntranceDirection = EntranceDirection.BIDIRECTIONAL,
    camera_profiles: tuple[str, ...] = (),
) -> EntranceModel:
    return EntranceModel(
        profile_id=profile_id,
        name=profile_id,
        geometry=GeometryModel(
            geometry_id=f"g-{uuid.uuid4()}",
            geometry_type=GeometryType.LINESTRING,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            geometry_scope=GeometryScope.VENUE,
            coordinates=coords,
        ),
        direction=direction,
        camera_profiles=list(camera_profiles),
    )


def _camera_line(
    profile_id: str,
    coords: list[list[float]],
    cam_ref: str,
    *,
    direction: EntranceDirection = EntranceDirection.BIDIRECTIONAL,
) -> EntranceModel:
    return EntranceModel(
        profile_id=profile_id,
        name=profile_id,
        geometry=GeometryModel(
            geometry_id=f"g-{uuid.uuid4()}",
            geometry_type=GeometryType.LINESTRING,
            coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
            geometry_scope=GeometryScope.CAMERA,
            reference_camera_profile_id=cam_ref,
            coordinates=coords,
        ),
        direction=direction,
    )


def _camera(
    *,
    profile_id: str = "cam-1",
    camera_id: CameraId | None = None,
) -> CameraProfileModel:
    return CameraProfileModel(
        profile_id=profile_id,
        camera_id=camera_id or CameraId(uuid.uuid4()),
        camera_reference=profile_id,
        resolution_width=1920,
        resolution_height=1080,
        mount_type=CameraMountType.CEILING,
    )


def _version(
    *,
    version: int = 1,
    version_id: uuid.UUID | None = None,
    cameras: tuple[CameraProfileModel, ...] = (),
    entrances: tuple[EntranceModel, ...] = (),
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
        "entrances": list(entrances),
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
    space: CoordinateSpace = CoordinateSpace.VENUE_LOCAL,
    policy: SpatialPointPolicy = SpatialPointPolicy.FOOTPOINT,
) -> SpatialPointModel:
    return SpatialPointModel(x=x, y=y, coordinate_space=space, policy=policy)


def _track(
    *,
    track_id: TrackId | None = None,
    session_id: VideoSessionId | None = None,
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


def _lobby_config(
    *,
    camera: CameraProfileModel | None = None,
    lines: tuple[EntranceModel, ...] = (_venue_line("line-1", LINE_HORIZONTAL),),
) -> ConfigurationVersionModel:
    return _version(cameras=(camera or _camera(),), entrances=lines)


def evaluate(
    configuration: ConfigurationVersionModel,
    camera_id: CameraId,
    previous_track: TrackObservation,
    current_track: TrackObservation,
    previous_point: SpatialPointModel,
    current_point: SpatialPointModel,
    line: EntranceModel,
) -> LineCrossingObservation:
    """Run the pure engine with a canonical input."""
    return evaluate_line_crossing(
        LineCrossingInput(
            configuration=configuration,
            previous_track=previous_track,
            current_track=current_track,
            camera_id=camera_id,
            previous_point=previous_point,
            current_point=current_point,
            line=line,
        )
    )


def _crossing_pair(
    *,
    p1: tuple[float, float] = (5.0, 10.0),
    p2: tuple[float, float] = (5.0, 0.0),
    session_id: VideoSessionId | None = None,
    track_id: TrackId | None = None,
) -> tuple[TrackObservation, TrackObservation, SpatialPointModel, SpatialPointModel]:
    """A canonical previous→current pair (same track, ordered frames)."""
    session_id = session_id or VideoSessionId(new_uuid())
    track_id = track_id or TrackId(new_uuid())
    previous = _track(
        track_id=track_id,
        session_id=session_id,
        frame_id=FrameId(new_uuid()),
        event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
    )
    current = _track(
        track_id=track_id,
        session_id=session_id,
        frame_id=FrameId(new_uuid()),
        event_time=datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC),
    )
    return previous, current, _point(*p1), _point(*p2)


# =============================================================================
# 1-7. Crossing detection, sides, direction
# =============================================================================


class TestCrossingDetection:
    """CROSSED requires a proper segment intersection — never closeness."""

    def test_clear_crossing(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()  # (5,10) → (5,0)
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED

    def test_no_crossing_far_from_line(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(2.0, 20.0), p2=(8.0, 20.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_closeness_is_never_a_crossing(self) -> None:
        # Both points are very close to the line but on the SAME side.
        config = _lobby_config()
        cam = config.cameras[0]
        offset = GEOMETRY_TOLERANCE * 1000
        previous, current, p1, p2 = _crossing_pair(p1=(4.0, 5.0 + offset), p2=(6.0, 5.0 + offset))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_same_side_no_crossing(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(2.0, 8.0), p2=(8.0, 8.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_opposite_sides_is_a_crossing(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(2.0, 8.0), p2=(8.0, 2.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED

    def test_opposite_sides_beyond_line_extent_no_crossing(self) -> None:
        # The movement crosses the line's INFINITE extension beyond the
        # line's endpoints — not a crossing of the configured line.
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(20.0, 10.0), p2=(20.0, 0.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_stationary_point_never_crosses(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 10.0), p2=(5.0, 10.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING


class TestCrossingDirection:
    """FORWARD/REVERSE are geometric and only when direction is configured."""

    def _config_with_direction(self, direction: EntranceDirection) -> ConfigurationVersionModel:
        return _lobby_config(lines=(_venue_line("line-1", LINE_HORIZONTAL, direction=direction),))

    def test_forward_crossing(self) -> None:
        config = self._config_with_direction(EntranceDirection.ENTRANCE)
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 10.0), p2=(5.0, 0.0))  # LEFT → RIGHT
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED
        assert obs.direction is CrossingDirection.FORWARD

    def test_reverse_crossing(self) -> None:
        config = self._config_with_direction(EntranceDirection.ENTRANCE)
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 0.0), p2=(5.0, 10.0))  # RIGHT → LEFT
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED
        assert obs.direction is CrossingDirection.REVERSE

    def test_exit_label_uses_same_geometric_convention(self) -> None:
        # ENTRANCE vs EXIT is business meaning; the engine's FORWARD/
        # REVERSE are geometric only (section 12).
        config = self._config_with_direction(EntranceDirection.EXIT)
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 10.0), p2=(5.0, 0.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.direction is CrossingDirection.FORWARD

    def test_unknown_direction_when_not_configured(self) -> None:
        # BIDIRECTIONAL = direction not configured → UNKNOWN (never invented).
        config = self._config_with_direction(EntranceDirection.BIDIRECTIONAL)
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 10.0), p2=(5.0, 0.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED
        assert obs.direction is CrossingDirection.UNKNOWN

    def test_no_crossing_direction_is_unknown(self) -> None:
        config = self._config_with_direction(EntranceDirection.ENTRANCE)
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(2.0, 8.0), p2=(8.0, 8.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING
        assert obs.direction is CrossingDirection.UNKNOWN


# =============================================================================
# 7. Boundary cases
# =============================================================================


class TestBoundaryCases:
    """Defined, tolerance-driven behavior — never floating-point-random."""

    def test_previous_point_on_line(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 5.0), p2=(5.0, 0.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_current_point_on_line(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 10.0), p2=(7.0, 5.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_both_points_on_line(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 5.0), p2=(7.0, 5.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_line_endpoint_touch(self) -> None:
        # The movement segment's endpoint equals a line endpoint.
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 10.0), p2=(0.0, 5.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_line_endpoint_on_movement_segment(self) -> None:
        # The line's endpoint (0,5) lies ON the vertical movement path —
        # a touch, not a proper crossing.
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(0.0, 10.0), p2=(0.0, 0.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_segment_overlapping_line(self) -> None:
        # Collinear movement along the line — never a crossing.
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(2.0, 5.0), p2=(8.0, 5.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_within_tolerance_is_on_line(self) -> None:
        # The ON_LINE band is the raw cross-product magnitude <= tolerance
        # (segment length x perpendicular distance). For a length-10 line,
        # tolerance/100 in perpendicular distance stays safely ON_LINE
        # (not LEFT/RIGHT) — well clear of the FP noise floor.
        config = _lobby_config()
        cam = config.cameras[0]
        offset = GEOMETRY_TOLERANCE / 100.0
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 5.0 + offset), p2=(5.0, 0.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING

    def test_floating_point_boundary_stays_deterministic(self) -> None:
        # 10x beyond tolerance is strictly LEFT → the crossing is CROSSED;
        # the same input 1000x always returns the same result.
        config = _lobby_config()
        cam = config.cameras[0]
        offset = GEOMETRY_TOLERANCE * 10
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, 5.0 + offset), p2=(5.0, 0.0))
        first = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert first.crossing_state is CrossingState.CROSSED
        for _ in range(1000):
            again = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
            assert again == first

    def test_multi_edge_line_first_crossed_edge_wins(self) -> None:
        # L-shaped polyline (0,0)→(5,0)→(5,5); the movement crosses the
        # vertical edge (5,0)→(5,5), not the horizontal one.
        config = _lobby_config(
            lines=(
                _venue_line(
                    "line-l",
                    [[0, 0], [5, 0], [5, 5]],
                    direction=EntranceDirection.ENTRANCE,
                ),
            )
        )
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair(p1=(10.0, 2.0), p2=(-2.0, 2.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED
        assert obs.direction is CrossingDirection.REVERSE

    def test_near_supporting_extension_does_not_suppress_other_edge_crossing(
        self,
    ) -> None:
        # Reviewer regression: an endpoint near edge A's supporting-line
        # EXTENSION (beyond edge A's extent) must NOT suppress a genuine
        # crossing through edge B of the same polyline.
        # Polyline: edge A = (0,0)->(10,0), edge B = (10,0)->(10,10).
        config = _lobby_config(lines=(_venue_line("line-l", [[0, 0], [10, 0], [10, 10]]),))
        cam = config.cameras[0]
        # p1 lies just above edge A's supporting line (y=0) at x=15 —
        # beyond edge A's endpoint, so NOT on the line's extent. The
        # movement then genuinely crosses edge B at (10, ~6.7).
        offset = GEOMETRY_TOLERANCE * 0.01
        previous, current, p1, p2 = _crossing_pair(p1=(15.0, offset), p2=(9.0, 8.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.CROSSED

    def test_on_one_edge_but_ending_on_other_is_no_crossing(self) -> None:
        # Boundary policy: an endpoint genuinely ON one edge (within its
        # extent) always wins — the movement is NO_CROSSING even though
        # it also crosses a different edge (documented multi-edge rule).
        config = _lobby_config(lines=(_venue_line("line-l", [[0, 0], [10, 0], [10, 10]]),))
        cam = config.cameras[0]
        offset = GEOMETRY_TOLERANCE / 100.0
        previous, current, p1, p2 = _crossing_pair(p1=(5.0, offset), p2=(11.0, 8.0))
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.crossing_state is CrossingState.NO_CROSSING


# =============================================================================
# 8-9. Same-track requirement and frame order
# =============================================================================


class TestTrackAndOrder:
    """A transition exists only for the same track, in temporal order."""

    def test_different_track_ids_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        session = VideoSessionId(new_uuid())
        previous = _track(track_id=TrackId(new_uuid()), session_id=session)
        current = _track(track_id=TrackId(new_uuid()), session_id=session)
        with pytest.raises(TransitionScopeError, match="SAME track"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(5, 10),
                _point(5, 0),
                config.entrances[0],
            )

    def test_different_sessions_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        track = TrackId(new_uuid())
        previous = _track(track_id=track, session_id=VideoSessionId(new_uuid()))
        current = _track(track_id=track, session_id=VideoSessionId(new_uuid()))
        with pytest.raises(TransitionScopeError, match="session"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(5, 10),
                _point(5, 0),
                config.entrances[0],
            )

    def test_duplicate_frame_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        frame = FrameId(new_uuid())
        previous = _track(frame_id=frame)
        current = _track(
            track_id=previous.track_id,
            session_id=previous.session_id,
            frame_id=frame,
        )
        with pytest.raises(TransitionOrderError, match="frame"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(5, 10),
                _point(5, 0),
                config.entrances[0],
            )

    def test_timestamp_regression_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous = _track(event_time=datetime(2026, 7, 29, 12, 0, 5, tzinfo=UTC))
        current = _track(
            track_id=previous.track_id,
            session_id=previous.session_id,
            event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        )
        with pytest.raises(TransitionOrderError, match="regression"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(5, 10),
                _point(5, 0),
                config.entrances[0],
            )

    def test_missing_intermediate_frames_allowed(self) -> None:
        # Task 11/13 policy: skipped indices are legal; a transition over
        # a gap is evaluated normally (frames with distinct ids/times).
        config = _lobby_config()
        cam = config.cameras[0]
        previous = _track(
            frame_id=FrameId(new_uuid()),
            event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        )
        current = _track(
            frame_id=FrameId(new_uuid()),
            event_time=datetime(2026, 7, 29, 12, 0, 30, tzinfo=UTC),
        )
        previous = previous.model_copy(
            update={"track_id": current.track_id, "session_id": current.session_id}
        )
        obs = evaluate(
            config,
            cam.camera_id,
            previous,
            current,
            _point(5, 10),
            _point(5, 0),
            config.entrances[0],
        )
        assert obs.crossing_state is CrossingState.CROSSED


# =============================================================================
# 10-11. Configuration version provenance and camera isolation
# =============================================================================


class TestConfigurationAndCamera:
    """The pinned immutable version and camera scope drive every result."""

    def test_observation_carries_pinned_configuration_version(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.configuration_version_id == config.configuration_version_id

    def test_line_not_in_configuration_rejected(self) -> None:
        config = _lobby_config(lines=())
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        foreign_line = _venue_line("line-ghost", LINE_HORIZONTAL)
        with pytest.raises(ReferenceIntegrityError, match="not part"):
            evaluate(config, cam.camera_id, previous, current, p1, p2, foreign_line)

    def test_line_with_matching_id_but_foreign_geometry_rejected(self) -> None:
        # Reference integrity is profile_id AND geometry: a line with the
        # right profile_id but different geometry is rejected, never
        # silently evaluated (reviewer regression).
        config = _lobby_config()  # line-1 at [[0,5],[10,5]]
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        impostor = _venue_line("line-1", [[20, 5], [30, 5]])
        with pytest.raises(ReferenceIntegrityError, match="does not match"):
            evaluate(config, cam.camera_id, previous, current, p1, p2, impostor)

    def test_non_published_configuration_rejected(self) -> None:
        for status in (
            ConfigurationStatus.DRAFT,
            ConfigurationStatus.VALIDATING,
            ConfigurationStatus.VALIDATED,
        ):
            config = _version(
                status=status,
                cameras=(_camera(),),
                entrances=(_venue_line("line-1", LINE_HORIZONTAL),),
            )
            cam = config.cameras[0]
            previous, current, p1, p2 = _crossing_pair()
            with pytest.raises(ConfigurationNotPublishedError, match="PUBLISHED"):
                evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])

    def test_unknown_camera_rejected(self) -> None:
        config = _lobby_config()
        previous, current, p1, p2 = _crossing_pair()
        with pytest.raises(CameraNotInConfigurationError, match="not configured"):
            evaluate(config, CameraId(uuid.uuid4()), previous, current, p1, p2, config.entrances[0])

    def test_line_of_other_camera_not_applicable(self) -> None:
        cam_a = _camera(profile_id="cam-a")
        cam_b = _camera(profile_id="cam-b")
        line_b = _venue_line("line-b", LINE_HORIZONTAL, camera_profiles=("cam-b",))
        config = _version(cameras=(cam_a, cam_b), entrances=(line_b,))
        previous, current, p1, p2 = _crossing_pair()
        with pytest.raises(LineNotApplicableError, match="not declared"):
            evaluate(config, cam_a.camera_id, previous, current, p1, p2, line_b)

    def test_line_declared_for_camera_applies(self) -> None:
        cam_a = _camera(profile_id="cam-a")
        cam_b = _camera(profile_id="cam-b")
        line_a = _venue_line("line-a", LINE_HORIZONTAL, camera_profiles=("cam-a",))
        config = _version(cameras=(cam_a, cam_b), entrances=(line_a,))
        previous, current, p1, p2 = _crossing_pair()
        obs = evaluate(config, cam_a.camera_id, previous, current, p1, p2, line_a)
        assert obs.crossing_state is CrossingState.CROSSED

    def test_line_without_camera_binding_applies_to_all(self) -> None:
        cam_a = _camera(profile_id="cam-a")
        line = _venue_line("line-a", LINE_HORIZONTAL)  # camera_profiles empty
        config = _version(cameras=(cam_a,), entrances=(line,))
        previous, current, p1, p2 = _crossing_pair()
        obs = evaluate(config, cam_a.camera_id, previous, current, p1, p2, line)
        assert obs.crossing_state is CrossingState.CROSSED

    def test_camera_scoped_line_rejects_other_camera(self) -> None:
        cam_a = _camera(profile_id="cam-a")
        cam_b = _camera(profile_id="cam-b")
        line_a = _camera_line("line-a", [[0.1, 0.4], [0.9, 0.4]], "cam-a")
        config = _version(cameras=(cam_a, cam_b), entrances=(line_a,))
        previous, current, p1, p2 = _crossing_pair()
        with pytest.raises(LineNotApplicableError, match="camera-scoped"):
            evaluate(config, cam_b.camera_id, previous, current, p1, p2, line_a)

    def test_historical_version_isolation(self) -> None:
        # Session → Config V1 → Line V1. Then V2 publishes a moved line.
        # Historical observations continue to use V1 — never the latest.
        camera = _camera(profile_id="cam-1")
        v1 = _version(
            version=1,
            cameras=(camera,),
            entrances=(_venue_line("line-1", [[0, 5], [10, 5]]),),
        )
        v2 = _version(
            version=2,
            cameras=(camera,),
            entrances=(_venue_line("line-1", [[20, 5], [30, 5]]),),
        )
        previous, current, p1, p2 = _crossing_pair()  # crosses x in [0,10] at y=5
        obs_v1 = evaluate(v1, camera.camera_id, previous, current, p1, p2, v1.entrances[0])
        obs_v2 = evaluate(v2, camera.camera_id, previous, current, p1, p2, v2.entrances[0])
        assert obs_v1.crossing_state is CrossingState.CROSSED
        assert obs_v2.crossing_state is CrossingState.NO_CROSSING
        assert obs_v1.configuration_version_id == v1.configuration_version_id
        assert obs_v2.configuration_version_id == v2.configuration_version_id


# =============================================================================
# 19. Invalid geometry and malformed inputs
# =============================================================================


class TestInvalidInputs:
    """Malformed input fails with typed errors — never a silent result."""

    def test_polygon_line_rejected(self) -> None:
        bad = EntranceModel(
            profile_id="line-bad",
            name="bad",
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.POLYGON,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            ),
        )
        config = _version(cameras=(_camera(),), entrances=(bad,))
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        with pytest.raises(InvalidLineError, match="LINESTRING"):
            evaluate(config, cam.camera_id, previous, current, p1, p2, bad)

    def test_image_points_against_venue_line_raises_projection_blocker(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous = _track()
        current = previous.model_copy(update={"frame_id": FrameId(new_uuid())})
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(0.5, 0.5, space=CoordinateSpace.IMAGE_NORMALIZED),
                _point(0.5, 0.4, space=CoordinateSpace.IMAGE_NORMALIZED),
                config.entrances[0],
            )

    def test_venue_points_against_camera_line_rejected(self) -> None:
        line = _camera_line("line-a", [[0.1, 0.4], [0.9, 0.4]], "cam-1")
        config = _version(cameras=(_camera(),), entrances=(line,))
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()  # VENUE_LOCAL points
        with pytest.raises(InvalidLineError, match="coordinate space"):
            evaluate(config, cam.camera_id, previous, current, p1, p2, line)

    def test_points_in_different_spaces_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous = _track()
        current = previous.model_copy(update={"frame_id": FrameId(new_uuid())})
        with pytest.raises(InvalidSpatialInputError, match="coordinate space"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(5, 10, space=CoordinateSpace.VENUE_LOCAL),
                _point(0.5, 0.4, space=CoordinateSpace.IMAGE_NORMALIZED),
                config.entrances[0],
            )

    def test_mixed_point_policies_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous = _track()
        current = previous.model_copy(update={"frame_id": FrameId(new_uuid())})
        with pytest.raises(InvalidSpatialInputError, match="policy"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                _point(5, 10, policy=SpatialPointPolicy.FOOTPOINT),
                _point(5, 0, policy=SpatialPointPolicy.CENTROID),
                config.entrances[0],
            )

    def test_nan_point_rejected(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous = _track()
        current = previous.model_copy(update={"frame_id": FrameId(new_uuid())})
        nan_point = SpatialPointModel(
            x=float("nan"),
            y=0.5,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        with pytest.raises(InvalidSpatialInputError, match="finite"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                nan_point,
                _point(5, 0),
                config.entrances[0],
            )

    def test_missing_inputs_rejected(self) -> None:
        with pytest.raises(InvalidSpatialInputError, match="LineCrossingInput"):
            evaluate_line_crossing(None)  # type: ignore[arg-type]
        with pytest.raises(InvalidSpatialInputError, match="configuration"):
            LineCrossingInput(
                configuration=None,  # type: ignore[arg-type]
                previous_track=_track(),
                current_track=_track(),
                camera_id=CameraId(uuid.uuid4()),
                previous_point=_point(5, 10),
                current_point=_point(5, 0),
                line=_venue_line("line-1", LINE_HORIZONTAL),
            )
        with pytest.raises(InvalidSpatialInputError, match="line"):
            LineCrossingInput(
                configuration=_lobby_config(),
                previous_track=_track(),
                current_track=_track(),
                camera_id=CameraId(uuid.uuid4()),
                previous_point=_point(5, 10),
                current_point=_point(5, 0),
                line=None,  # type: ignore[arg-type]
            )

    def test_invalid_point_is_not_masked_by_config_traversal(self) -> None:
        # Input validation precedes configuration traversal: a NaN point
        # raises the input error even when the config is broken.
        broken = _version(cameras=(_camera(),), entrances=())
        cam = broken.cameras[0]
        previous = _track()
        current = previous.model_copy(update={"frame_id": FrameId(new_uuid())})
        nan_point = SpatialPointModel(
            x=float("nan"),
            y=0.5,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        with pytest.raises(InvalidSpatialInputError, match="finite"):
            evaluate(
                broken,
                cam.camera_id,
                previous,
                current,
                nan_point,
                _point(5, 0),
                _venue_line("line-ghost", LINE_HORIZONTAL),
            )


# =============================================================================
# 13. Contract / provenance
# =============================================================================


class TestObservationContract:
    """The canonical transition observation preserves full provenance."""

    def test_provenance_chain_preserved(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        assert obs.session_id == current.session_id
        assert obs.track_id == current.track_id
        assert obs.camera_id == cam.camera_id
        assert obs.configuration_version_id == config.configuration_version_id
        assert obs.line_profile_id == config.entrances[0].profile_id
        assert obs.previous_frame_id == previous.frame_id
        assert obs.current_frame_id == current.frame_id
        assert obs.previous_event_time == previous.event_time
        assert obs.current_event_time == current.event_time
        assert obs.previous_point == p1
        assert obs.current_point == p2
        assert obs.engine_version == SPATIAL_ENGINE_VERSION

    def test_observation_serializable(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        restored = LineCrossingObservation.model_validate(obs.model_dump(mode="json"))
        assert restored == obs

    def test_contract_rejects_regressed_time_order(self) -> None:
        # The contract itself enforces previous <= current (same
        # invariant the engine enforces via TransitionOrderError).
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        obs = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        bad = obs.model_copy(
            update={
                "previous_event_time": obs.current_event_time,
                "current_event_time": obs.previous_event_time,
            }
        )
        with pytest.raises(ValueError, match="previous_event_time must not follow"):
            LineCrossingObservation.model_validate(bad.model_dump())


# =============================================================================
# 15. Determinism
# =============================================================================


class TestDeterminism:
    """Same inputs always produce an identical observation."""

    def test_repeated_evaluation_is_identical(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        previous, current, p1, p2 = _crossing_pair()
        first = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
        for _ in range(1000):
            again = evaluate(config, cam.camera_id, previous, current, p1, p2, config.entrances[0])
            assert again == first

    def test_independent_of_configuration_list_order(self) -> None:
        line_a = _venue_line("line-a", [[0, 5], [10, 5]])
        line_b = _venue_line("line-b", [[20, 5], [30, 5]])
        camera = _camera()
        shared_version_id = uuid.uuid4()
        config_a = _version(
            version_id=shared_version_id, cameras=(camera,), entrances=(line_a, line_b)
        )
        config_b = _version(
            version_id=shared_version_id, cameras=(camera,), entrances=(line_b, line_a)
        )
        previous, current, p1, p2 = _crossing_pair()
        obs_a = evaluate(config_a, camera.camera_id, previous, current, p1, p2, line_a)
        obs_b = evaluate(config_b, camera.camera_id, previous, current, p1, p2, line_a)
        assert obs_a == obs_b

    def test_no_current_time_dependence(self) -> None:
        config = _lobby_config()
        cam = config.cameras[0]
        event_time = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        previous = _track(event_time=event_time)
        current = _track(event_time=datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC))
        previous = previous.model_copy(
            update={"track_id": current.track_id, "session_id": current.session_id}
        )
        obs = evaluate(
            config,
            cam.camera_id,
            previous,
            current,
            _point(5, 10),
            _point(5, 0),
            config.entrances[0],
        )
        assert obs.previous_event_time == event_time


# =============================================================================
# 16. Integration: the complete deterministic chain
# =============================================================================


class TestIntegrationChain:
    """TrackObservation → extract_point → evaluator → canonical observation."""

    def test_full_chain_camera_scoped_line(self) -> None:
        """The literal Step 4 chain, end to end, with the REAL Step 2
        point extraction and the REAL Task 10 contracts (no SDK/DB)."""
        session = VideoSessionId(new_uuid())
        track_id = TrackId(new_uuid())
        frame_n = FrameId(new_uuid())
        frame_n1 = FrameId(new_uuid())
        t_n = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        t_n1 = datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC)

        # --- Step 2: canonical points from the tracks' bounding boxes ---
        prev_box = BoundingBox(x_min=0.2, y_min=0.3, x_max=0.4, y_max=0.5)
        curr_box = BoundingBox(x_min=0.2, y_min=0.2, x_max=0.4, y_max=0.4)
        prev_point = extract_point(prev_box, SpatialPointPolicy.FOOTPOINT)
        curr_point = extract_point(curr_box, SpatialPointPolicy.FOOTPOINT)
        assert prev_point.coordinate_space is CoordinateSpace.IMAGE_NORMALIZED

        # A camera-scoped threshold at y = 0.45, declared for this camera.
        line = _camera_line(
            "line-door", [[0.1, 0.45], [0.9, 0.45]], "cam-1", direction=EntranceDirection.ENTRANCE
        )
        camera = _camera(profile_id="cam-1")
        config = _version(cameras=(camera,), entrances=(line,))

        previous = TrackObservation(
            track_id=track_id,
            detection_id=DetectionId(new_uuid()),
            frame_id=frame_n,
            session_id=session,
            event_time=t_n,
            track_state=TrackState.ACTIVE,
        )
        current = TrackObservation(
            track_id=track_id,
            detection_id=DetectionId(new_uuid()),
            frame_id=frame_n1,
            session_id=session,
            event_time=t_n1,
            track_state=TrackState.ACTIVE,
        )

        obs = evaluate(config, camera.camera_id, previous, current, prev_point, curr_point, line)
        assert obs.crossing_state is CrossingState.CROSSED
        assert obs.direction is CrossingDirection.FORWARD
        assert obs.previous_point == prev_point
        assert obs.current_point == curr_point
        assert obs.track_id == track_id
        assert obs.session_id == session
        assert obs.line_profile_id == "line-door"
        assert obs.previous_frame_id == frame_n
        assert obs.current_frame_id == frame_n1

    def test_full_chain_venue_line_post_projection(self) -> None:
        """The same chain with a Task 10 venue threshold and VENUE_LOCAL
        points (the documented post-projection stage)."""
        session = VideoSessionId(new_uuid())
        track_id = TrackId(new_uuid())
        camera = _camera(profile_id="cam-1")
        line = _venue_line("line-threshold", LINE_HORIZONTAL, direction=EntranceDirection.ENTRANCE)
        config = _version(cameras=(camera,), entrances=(line,))

        previous = _track(track_id=track_id, session_id=session)
        current = _track(track_id=track_id, session_id=session)
        current = current.model_copy(update={"frame_id": FrameId(new_uuid())})

        obs = evaluate(
            config, camera.camera_id, previous, current, _point(5, 10), _point(5, 0), line
        )
        assert obs.crossing_state is CrossingState.CROSSED
        assert obs.direction is CrossingDirection.FORWARD
        assert obs.configuration_version_id == config.configuration_version_id

    def test_live_track_points_against_venue_line_record_blocker(self) -> None:
        # Production reality (recorded Step 3 blocker): real extract_point
        # output (IMAGE_NORMALIZED) against a Task 10 venue line raises
        # the typed projection error — never a silent NO_CROSSING.
        config = _lobby_config()
        cam = config.cameras[0]
        prev_point = extract_point(
            BoundingBox(x_min=0.2, y_min=0.3, x_max=0.4, y_max=0.5), SpatialPointPolicy.FOOTPOINT
        )
        curr_point = extract_point(
            BoundingBox(x_min=0.2, y_min=0.2, x_max=0.4, y_max=0.4), SpatialPointPolicy.FOOTPOINT
        )
        previous = _track()
        current = previous.model_copy(update={"frame_id": FrameId(new_uuid())})
        with pytest.raises(VenuePointRequiredError, match="VENUE_LOCAL"):
            evaluate(
                config,
                cam.camera_id,
                previous,
                current,
                prev_point,
                curr_point,
                config.entrances[0],
            )


# =============================================================================
# 11. Pure engine boundary
# =============================================================================


class TestEnginePurity:
    """The Step 4 engine and its geometry primitives perform no I/O."""

    def test_no_io_imports_in_transition_modules(self) -> None:
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
        root = Path(__file__).resolve().parents[2]
        targets = [
            root / "backend" / "app" / "intelligence" / "spatial" / "transitions.py",
            root / "backend" / "app" / "intelligence" / "geometry" / "segments.py",
        ]
        for path in targets:
            text = path.read_text()
            for module in forbidden:
                assert not re.search(rf"^\s*(from|import)\s+{module}\b", text, re.MULTILINE), (
                    f"I/O/stateful module {module!r} leaked into {path.name}"
                )
            assert "print(" not in text, f"print() leaked into {path.name}"
            assert "TODO" not in text and "FIXME" not in text, f"TODO/FIXME leaked into {path.name}"
