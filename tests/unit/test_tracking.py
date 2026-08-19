"""Tests for the multi-object tracking abstraction (Task 13).

Covers the complete ``ObjectTracker`` contract behind the canonical
Task 4 ``TrackObservation`` boundary:

- unit scenarios: empty/single/multiple detections, continuing/new
  tracks, temporarily-missing (LOST) and ended (TERMINATED) tracks,
  class consistency, frame ordering, source/session isolation;
- track identity: deterministic session-scoped canonical ids (uuid5);
- negative matrix: invalid detections/provenance, scope violations,
  backend init/runtime failures, restart, "no tracks" vs "tracker
  failure";
- contract: observations round-trip the canonical Task 4 contract;
- integration: the REAL Task 12 -> Task 13 boundary
  (``FramePacket -> YOLOv8Adapter -> DetectionObservation ->
  ByteTrackAdapter -> TrackObservation``) via the same fake-SDK seams
  used by the Task 12 suites — no Task 12 bypass;
- architectural isolation: no tracking SDK import or mention anywhere
  outside the adapter.

Production code is not weakened to make these tests pass.
"""

from __future__ import annotations

import ast
import sys
import types
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

import backend.app.intelligence.tracking.bytetrack_adapter as tracker_module
from backend.app.intelligence.detectors import (
    DetectionInput,
    DetectorConfig,
    Device,
    ModelSpec,
    yolo_adapter,
)
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from backend.app.intelligence.tracking import (
    ByteTrackAdapter,
    ObjectTracker,
    TrackClassSwitchError,
    TrackerConfig,
    TrackingError,
    TrackingExecutionError,
    TrackingInput,
    TrackOrderError,
    TrackScopeError,
    box_iou,
    track_uuid,
    validate_tracking_provenance,
)
from contracts.common import (
    SCHEMA_VERSION,
    DetectionId,
    FrameId,
    TrackId,
    VideoAssetId,
    VideoSessionId,
    new_uuid,
)
from contracts.video import FramePacket
from contracts.vision import BoundingBox, DetectionObservation, TrackObservation, TrackState

# The REAL numpy-conversion seam, captured at module import (before any
# fixture patches the module attribute) so the missing-numpy test is
# deterministic regardless of the environment.
_REAL_TO_DETS_ARRAY = tracker_module._to_dets_array

# Production scan roots for the architectural-isolation guards.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = (PROJECT_ROOT / "backend" / "app", PROJECT_ROOT / "contracts")


# ---------------------------------------------------------------------------
# Helpers / canonical builders
# ---------------------------------------------------------------------------


def make_session() -> VideoSessionId:
    return VideoSessionId(new_uuid())


def make_source() -> VideoAssetId:
    return VideoAssetId(new_uuid())


def make_frame(
    session: VideoSessionId,
    *,
    index: int = 0,
    event_time: datetime | None = None,
    source_ref: VideoAssetId | None = None,
) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=session,
        source_ref=source_ref,
        frame_index=index,
        event_time=event_time or datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        width=640,
        height=480,
    )


def make_detection(
    frame: FramePacket,
    *,
    box: tuple[float, float, float, float] = (0.1, 0.1, 0.5, 0.5),
    class_name: str = "person",
    class_id: int = 0,
    confidence: float = 0.9,
) -> DetectionObservation:
    x_min, y_min, x_max, y_max = box
    return DetectionObservation(
        detection_id=DetectionId(new_uuid()),
        frame_id=frame.frame_id,
        session_id=frame.session_id,
        source_ref=frame.source_ref,
        frame_index=frame.frame_index,
        class_name=class_name,
        class_id=class_id,
        confidence=confidence,
        bounding_box=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        event_time=frame.event_time,
        image_width=640,
        image_height=480,
    )


def make_adapter(
    session: VideoSessionId,
    *,
    config: TrackerConfig | None = None,
) -> ByteTrackAdapter:
    return ByteTrackAdapter(session_id=session, config=config)


def box_to_tlwh(box: tuple[float, float, float, float]) -> list[float]:
    x_min, y_min, x_max, y_max = box
    return [x_min, y_min, x_max - x_min, y_max - y_min]


# ---------------------------------------------------------------------------
# Fake tracking SDK (same seam pattern as the Task 12 fake YOLO SDK)
# ---------------------------------------------------------------------------


class FakeSTrack:
    """Deterministic stand-in for the backend's STrack target."""

    def __init__(
        self,
        track_id: int,
        tlwh: Sequence[float],
        *,
        score: float = 0.9,
        cls: int | None = 0,
        lost: bool = False,
    ) -> None:
        self.track_id = track_id
        self.tlwh = list(tlwh)
        self.score = score
        self.cls = cls
        self.lost = lost


class FakeBYTETracker:
    """SDK double with per-test failure injection via class attributes.

    ``update_plan`` is consumed one entry per ``update()`` call (EOF
    yields empty results) so multi-frame scenarios are deterministic.
    """

    instances: ClassVar[list[FakeBYTETracker]] = []
    update_plan: ClassVar[list[list[FakeSTrack]]] = []
    update_error: ClassVar[Exception | None] = None
    last_dets: ClassVar[Any] = None

    def __init__(self, args: Any) -> None:
        self.args = args
        FakeBYTETracker.instances.append(self)

    def update(self, dets: Any, img_info: Any, img_size: Any) -> list[FakeSTrack]:
        FakeBYTETracker.last_dets = dets
        if FakeBYTETracker.update_error is not None:
            raise FakeBYTETracker.update_error
        if FakeBYTETracker.update_plan:
            return FakeBYTETracker.update_plan.pop(0)
        return []


@pytest.fixture
def fake_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake tracking SDK + numpy-free dets conversion."""
    FakeBYTETracker.instances = []
    FakeBYTETracker.update_plan = []
    FakeBYTETracker.update_error = None
    FakeBYTETracker.last_dets = None
    module = types.ModuleType("bytetrack")
    module.BYTETracker = FakeBYTETracker
    module.__version__ = "0.0.0-test"
    monkeypatch.setitem(sys.modules, "bytetrack", module)
    monkeypatch.setattr(tracker_module, "_to_dets_array", lambda rows: rows)


def single_track(
    local_id: int,
    box: tuple[float, float, float, float] = (0.1, 0.1, 0.5, 0.5),
    *,
    lost: bool = False,
) -> list[FakeSTrack]:
    return [FakeSTrack(local_id, box_to_tlwh(box), lost=lost)]


# ---------------------------------------------------------------------------
# Tracker configuration validation
# ---------------------------------------------------------------------------


class TestTrackerConfigValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("track_thresh", -0.1),
            ("track_thresh", 1.1),
            ("match_thresh", -0.1),
            ("match_thresh", 1.5),
            ("detection_match_iou", -0.01),
            ("detection_match_iou", 1.01),
            ("track_buffer", -1),
            ("frame_rate", 0),
            ("min_hits", -1),
        ],
    )
    def test_invalid_values_rejected(self, field: str, value: Any) -> None:
        with pytest.raises(ValueError):
            TrackerConfig(**{field: value})

    def test_defaults_are_sane(self) -> None:
        config = TrackerConfig()
        assert config.track_thresh == pytest.approx(0.5)
        assert config.match_thresh == pytest.approx(0.8)
        assert config.track_buffer == 30
        assert config.frame_rate == 30
        assert config.detection_match_iou == pytest.approx(0.5)
        assert config.allow_class_switch is False


# ---------------------------------------------------------------------------
# Protocol boundary
# ---------------------------------------------------------------------------


class TestObjectTrackerProtocol:
    def test_adapter_satisfies_protocol(self) -> None:
        assert isinstance(make_adapter(make_session()), ObjectTracker)

    def test_unrelated_object_is_not_a_tracker(self) -> None:
        assert not isinstance("not a tracker", ObjectTracker)
        assert not isinstance(42, ObjectTracker)

    def test_tracker_swappable_without_downstream_change(self) -> None:
        """A structurally-conforming tracker replaces the adapter."""

        class TestTracker:
            @property
            def tracker_id(self) -> str:
                return "test"

            async def update(self, inp: TrackingInput) -> list[TrackObservation]:
                return []

            async def close(self) -> None:
                return None

        assert isinstance(TestTracker(), ObjectTracker)


# ---------------------------------------------------------------------------
# Basic tracking: empty / single / multiple
# ---------------------------------------------------------------------------


class TestBasicTracking:
    async def test_empty_detections_yield_no_tracks(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        observations = await tracker.update(TrackingInput(frame=make_frame(session), detections=[]))
        assert observations == []
        stats = tracker.stats()
        assert stats.total_updates == 1
        assert stats.active_tracks == 0
        assert stats.total_tracks_created == 0

    async def test_single_detection_produces_active_track(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [single_track(1)]
        tracker = make_adapter(session)
        frame = make_frame(session, index=0)
        det = make_detection(frame)
        observations = await tracker.update(TrackingInput(frame=frame, detections=[det]))
        assert len(observations) == 1
        obs = observations[0]
        # Provenance verbatim from the frame; linked to the detection.
        assert obs.session_id == session
        assert obs.frame_id == frame.frame_id
        assert obs.event_time == frame.event_time
        assert obs.detection_id == det.detection_id
        assert obs.schema_version == SCHEMA_VERSION
        assert obs.track_state is TrackState.ACTIVE
        # Tracker identity + local id in metadata (never SDK objects).
        assert obs.tracking_metadata is not None
        assert obs.tracking_metadata["tracker"] == "bytetrack"
        assert obs.tracking_metadata["local_track_id"] == 1
        assert obs.tracking_metadata["matched"] is True

    async def test_multiple_detections_produce_distinct_tracks(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [
            [
                FakeSTrack(1, box_to_tlwh((0.1, 0.1, 0.3, 0.3))),
                FakeSTrack(2, box_to_tlwh((0.6, 0.1, 0.9, 0.4))),
            ]
        ]
        tracker = make_adapter(session)
        frame = make_frame(session)
        det_a = make_detection(frame, box=(0.1, 0.1, 0.3, 0.3), class_id=0)
        det_b = make_detection(frame, box=(0.6, 0.1, 0.9, 0.4), class_id=1, class_name="bag")
        observations = await tracker.update(TrackingInput(frame=frame, detections=[det_a, det_b]))
        assert len(observations) == 2
        assert observations[0].track_id != observations[1].track_id
        # Each observation links ITS matched detection.
        assert {o.detection_id for o in observations} == {det_a.detection_id, det_b.detection_id}
        assert tracker.stats().active_tracks == 2
        assert tracker.stats().total_tracks_created == 2


# ---------------------------------------------------------------------------
# Track identity: stable, distinct, deterministic, session-scoped
# ---------------------------------------------------------------------------


class TestTrackIdentity:
    async def test_continuing_track_keeps_stable_id(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [
            single_track(1, (0.1, 0.1, 0.5, 0.5)),
            single_track(1, (0.2, 0.1, 0.6, 0.5)),
        ]
        tracker = make_adapter(session)
        ids: list[TrackId] = []
        for index in range(2):
            frame = make_frame(session, index=index)
            det = make_detection(frame, box=(0.2 * index + 0.1, 0.1, 0.2 * index + 0.5, 0.5))
            observations = await tracker.update(TrackingInput(frame=frame, detections=[det]))
            assert len(observations) == 1
            ids.append(observations[0].track_id)
        # The same backend-local id maps to the SAME canonical track id.
        assert ids[0] == ids[1]
        # And the observations link the per-frame detections.
        assert observations[0].detection_id == det.detection_id

    async def test_new_track_local_id_is_a_new_canonical_track(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [
            single_track(1),
            single_track(2),
        ]
        tracker = make_adapter(session)
        ids = []
        for index in range(2):
            frame = make_frame(session, index=index)
            det = make_detection(frame)
            observations = await tracker.update(TrackingInput(frame=frame, detections=[det]))
            ids.append(observations[0].track_id)
        assert ids[0] != ids[1]

    async def test_same_local_id_in_two_sessions_is_two_distinct_tracks(
        self, fake_tracker: None
    ) -> None:
        """Camera A track 1 and Camera B track 1 are NOT the same object."""
        session_a = make_session()
        session_b = make_session()
        tracker_a = make_adapter(session_a)
        tracker_b = make_adapter(session_b)
        frame_a = make_frame(session_a)
        frame_b = make_frame(session_b)
        FakeBYTETracker.update_plan = [single_track(1), single_track(1)]
        obs_a = await tracker_a.update(
            TrackingInput(frame=frame_a, detections=[make_detection(frame_a)])
        )
        obs_b = await tracker_b.update(
            TrackingInput(frame=frame_b, detections=[make_detection(frame_b)])
        )
        assert obs_a[0].track_id != obs_b[0].track_id

    def test_track_uuid_is_deterministic_and_session_scoped(self) -> None:
        session = make_session()
        other = make_session()
        assert track_uuid(session, 1) == track_uuid(session, 1)
        assert track_uuid(session, 1) != track_uuid(session, 2)
        assert track_uuid(session, 1) != track_uuid(other, 1)
        # uuid5 → valid UUID, deterministic across processes.
        assert isinstance(uuid.UUID(str(track_uuid(session, 1))), uuid.UUID)


# ---------------------------------------------------------------------------
# Lifecycle: LOST and TERMINATED
# ---------------------------------------------------------------------------


class TestTrackLifecycle:
    async def test_temporarily_missing_detection_emits_lost_then_reactivates(
        self, fake_tracker: None
    ) -> None:
        session = make_session()
        # Frame 0: matched ACTIVE; frame 1: lost (no detection); frame 2: back.
        FakeBYTETracker.update_plan = [
            single_track(1, (0.1, 0.1, 0.5, 0.5)),
            single_track(1, (0.1, 0.1, 0.5, 0.5), lost=True),
            single_track(1, (0.3, 0.1, 0.7, 0.5)),
        ]
        tracker = make_adapter(session)
        states: list[TrackState] = []
        ids: set[TrackId] = set()
        for index in range(3):
            frame = make_frame(session, index=index)
            box = (0.1, 0.1, 0.5, 0.5) if index == 0 else (0.3, 0.1, 0.7, 0.5)
            detections = [make_detection(frame, box=box)] if index != 1 else []
            observations = await tracker.update(TrackingInput(frame=frame, detections=detections))
            # A lost frame still emits the retained track (LOST) — the
            # missing detection does NOT silently create a new track.
            assert len(observations) == 1
            states.append(observations[0].track_state)
            ids.add(observations[0].track_id)
        assert states == [TrackState.ACTIVE, TrackState.LOST, TrackState.ACTIVE]
        assert len(ids) == 1  # one stable track across the gap
        assert tracker.stats().total_tracks_created == 1
        assert tracker.stats().active_tracks == 1

    async def test_ended_track_emits_terminated_exactly_once(self, fake_tracker: None) -> None:
        session = make_session()
        # Frame 0: active; frame 1: lost (buffered); frame 2: dropped by
        # the backend -> TERMINATED; frame 3: no further observations.
        FakeBYTETracker.update_plan = [
            single_track(1),
            single_track(1, lost=True),
            [],
            [],
        ]
        tracker = make_adapter(session)
        states: list[list[TrackState]] = []
        for index in range(4):
            frame = make_frame(session, index=index)
            detections = [make_detection(frame)] if index == 0 else []
            observations = await tracker.update(TrackingInput(frame=frame, detections=detections))
            states.append([o.track_state for o in observations])
        assert states == [
            [TrackState.ACTIVE],
            [TrackState.LOST],
            [TrackState.TERMINATED],
            [],
        ]
        stats = tracker.stats()
        assert stats.total_tracks_ended == 1
        assert stats.active_tracks == 0


# ---------------------------------------------------------------------------
# Class consistency
# ---------------------------------------------------------------------------


class TestClassConsistency:
    async def test_class_switch_rejected_without_policy(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [
            single_track(1, (0.1, 0.1, 0.5, 0.5)),
            single_track(1, (0.1, 0.1, 0.5, 0.5)),
        ]
        tracker = make_adapter(session)
        frame0 = make_frame(session, index=0)
        await tracker.update(
            TrackingInput(frame=frame0, detections=[make_detection(frame0, class_id=0)])
        )
        # The same track id now matches a DIFFERENT class at the same box.
        frame1 = make_frame(session, index=1)
        with pytest.raises(TrackClassSwitchError, match="switched class"):
            await tracker.update(
                TrackingInput(
                    frame=frame1,
                    detections=[make_detection(frame1, class_id=1, class_name="bag")],
                )
            )
        assert tracker.stats().total_failed_updates == 1

    async def test_class_switch_allowed_when_configured(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [
            single_track(1, (0.1, 0.1, 0.5, 0.5)),
            single_track(1, (0.1, 0.1, 0.5, 0.5)),
        ]
        tracker = make_adapter(session, config=TrackerConfig(allow_class_switch=True))
        frame0 = make_frame(session, index=0)
        await tracker.update(
            TrackingInput(frame=frame0, detections=[make_detection(frame0, class_id=0)])
        )
        frame1 = make_frame(session, index=1)
        det = make_detection(frame1, class_id=1, class_name="bag")
        observations = await tracker.update(TrackingInput(frame=frame1, detections=[det]))
        assert len(observations) == 1
        assert observations[0].detection_id == det.detection_id  # follows the detection


# ---------------------------------------------------------------------------
# Frame ordering and scope isolation
# ---------------------------------------------------------------------------


class TestFrameOrder:
    async def test_duplicate_frame_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session, index=0)
        await tracker.update(TrackingInput(frame=frame, detections=[]))
        with pytest.raises(TrackOrderError, match="duplicate frame"):
            await tracker.update(TrackingInput(frame=frame, detections=[]))

    async def test_out_of_order_index_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        await tracker.update(TrackingInput(frame=make_frame(session, index=2), detections=[]))
        with pytest.raises(TrackOrderError, match="does not advance"):
            await tracker.update(TrackingInput(frame=make_frame(session, index=1), detections=[]))

    async def test_skipped_index_is_allowed(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        await tracker.update(TrackingInput(frame=make_frame(session, index=0), detections=[]))
        # A gap (dropped frames under backpressure) is not an error.
        observations = await tracker.update(
            TrackingInput(frame=make_frame(session, index=5), detections=[])
        )
        assert observations == []

    async def test_timestamp_regression_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        base = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        tracker = make_adapter(session)
        await tracker.update(
            TrackingInput(
                frame=make_frame(session, index=0, event_time=base + timedelta(seconds=2)),
                detections=[],
            )
        )
        with pytest.raises(TrackOrderError, match="event_time regression"):
            await tracker.update(
                TrackingInput(
                    frame=make_frame(session, index=1, event_time=base + timedelta(seconds=1)),
                    detections=[],
                )
            )


class TestScopeIsolation:
    async def test_frame_from_another_session_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        other = make_session()
        with pytest.raises(TrackScopeError, match="does not match tracker session"):
            await tracker.update(TrackingInput(frame=make_frame(other), detections=[]))

    async def test_source_switch_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        await tracker.update(
            TrackingInput(
                frame=make_frame(session, index=0, source_ref=make_source()),
                detections=[],
            )
        )
        with pytest.raises(TrackScopeError, match="established source"):
            await tracker.update(
                TrackingInput(
                    frame=make_frame(session, index=1, source_ref=make_source()),
                    detections=[],
                )
            )


# ---------------------------------------------------------------------------
# Negative matrix
# ---------------------------------------------------------------------------


class TestNegative:
    async def test_detection_from_another_frame_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session, index=0)
        other = make_frame(session, index=1)
        with pytest.raises(TrackingError, match="not input frame"):
            await tracker.update(TrackingInput(frame=frame, detections=[make_detection(other)]))

    async def test_detection_session_mismatch_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session)
        # Same frame identity but a DIFFERENT session on the detection:
        # the provenance check must reject it before the backend runs.
        det = DetectionObservation(
            detection_id=DetectionId(new_uuid()),
            frame_id=frame.frame_id,
            session_id=make_session(),
            frame_index=frame.frame_index,
            class_name="person",
            class_id=0,
            confidence=0.9,
            bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
            event_time=frame.event_time,
        )
        with pytest.raises(TrackingError, match="session"):
            await tracker.update(TrackingInput(frame=frame, detections=[det]))

    async def test_detection_without_class_id_rejected(self, fake_tracker: None) -> None:
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session)
        det = DetectionObservation(
            detection_id=DetectionId(new_uuid()),
            frame_id=frame.frame_id,
            session_id=session,
            frame_index=0,
            class_name="person",
            confidence=0.9,
            bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
            event_time=frame.event_time,
        )  # class_id omitted (pre-extension observation)
        with pytest.raises(TrackingError, match="class_id"):
            await tracker.update(TrackingInput(frame=frame, detections=[det]))

    async def test_missing_sdk_is_typed(
        self, fake_tracker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "bytetrack", None)
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session)
        with pytest.raises(TrackingExecutionError, match="SDK") as excinfo:
            await tracker.update(TrackingInput(frame=frame, detections=[]))
        assert isinstance(excinfo.value.cause, ImportError)

    async def test_tracker_init_failure_is_typed(
        self, fake_tracker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FailingInit(FakeBYTETracker):
            def __init__(self, args: Any) -> None:
                msg = "out of memory"
                raise RuntimeError(msg)

        module = types.ModuleType("bytetrack")
        module.BYTETracker = FailingInit
        monkeypatch.setitem(sys.modules, "bytetrack", module)
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session)
        with pytest.raises(TrackingExecutionError, match="initialize") as excinfo:
            await tracker.update(TrackingInput(frame=frame, detections=[]))
        assert isinstance(excinfo.value.cause, RuntimeError)

    async def test_tracker_runtime_failure_is_typed_not_zero_tracks(
        self, fake_tracker: None
    ) -> None:
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session)
        await tracker.update(TrackingInput(frame=frame, detections=[]))
        FakeBYTETracker.update_error = RuntimeError("cuda sync error")
        with pytest.raises(TrackingExecutionError) as excinfo:
            await tracker.update(TrackingInput(frame=make_frame(session, index=1), detections=[]))
        assert isinstance(excinfo.value.cause, RuntimeError)
        # TRACKER FAILURE is distinct from NO TRACKS: a failure is
        # counted, an empty result never is.
        assert tracker.stats().total_failed_updates == 1
        assert tracker.stats().total_updates == 2

    async def test_missing_numpy_is_typed(
        self, fake_tracker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force numpy to be "absent" deterministically (the same trick the
        # detector tests use for the missing SDK): the adapter fails typed
        # instead of fabricating tracks — regardless of whether numpy is
        # installed in the environment.
        # Force numpy to be "absent" deterministically (the same trick the
        # detector tests use for the missing SDK) and restore the REAL
        # conversion seam: the adapter fails typed instead of fabricating
        # tracks — regardless of whether numpy is installed.
        monkeypatch.setitem(sys.modules, "numpy", None)
        monkeypatch.setattr(tracker_module, "_to_dets_array", _REAL_TO_DETS_ARRAY)
        session = make_session()
        tracker = make_adapter(session)
        frame = make_frame(session)
        with pytest.raises(TrackingExecutionError, match="numpy"):
            await tracker.update(TrackingInput(frame=frame, detections=[]))


# ---------------------------------------------------------------------------
# Restart / cleanup
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_close_then_update_starts_fresh(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [single_track(1), single_track(101)]
        tracker = make_adapter(session)
        frame0 = make_frame(session, index=0)
        first = await tracker.update(
            TrackingInput(frame=frame0, detections=[make_detection(frame0)])
        )
        assert first[0].tracking_metadata["local_track_id"] == 1
        await tracker.close()
        # After a restart old track ids are never pretended active.
        frame1 = make_frame(session, index=1)
        second = await tracker.update(
            TrackingInput(frame=frame1, detections=[make_detection(frame1)])
        )
        assert second[0].track_id != first[0].track_id
        assert second[0].tracking_metadata["local_track_id"] == 101
        assert tracker.active_tracks == 1
        assert tracker.stats().total_tracks_created == 1  # fresh instance

    async def test_close_is_idempotent(self, fake_tracker: None) -> None:
        tracker = make_adapter(make_session())
        await tracker.close()
        await tracker.close()  # safe
        assert tracker.active_tracks == 0
        assert tracker.stats().total_updates == 0


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestObservability:
    async def test_stats_record_detections_and_failures(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [single_track(1)]
        tracker = make_adapter(session)
        frame0 = make_frame(session, index=0)
        await tracker.update(TrackingInput(frame=frame0, detections=[make_detection(frame0)]))
        FakeBYTETracker.update_error = RuntimeError("boom")
        with pytest.raises(TrackingExecutionError):
            await tracker.update(TrackingInput(frame=make_frame(session, index=1), detections=[]))
        stats = tracker.stats()
        assert stats.total_updates == 2
        assert stats.total_frames == 2
        assert stats.total_detections == 1
        assert stats.total_failed_updates == 1
        assert stats.total_tracks_created == 1
        assert stats.active_tracks == 1
        assert stats.last_update_seconds is not None
        assert stats.total_update_seconds >= 0.0
        assert stats.tracker_id == "bytetrack"
        assert stats.session_id == session


# ---------------------------------------------------------------------------
# Contract round-trip
# ---------------------------------------------------------------------------


class TestContract:
    async def test_observations_round_trip_task4_contract(self, fake_tracker: None) -> None:
        session = make_session()
        FakeBYTETracker.update_plan = [single_track(1, lost=True)]
        tracker = make_adapter(session)
        frame = make_frame(session)
        observations = await tracker.update(
            TrackingInput(frame=frame, detections=[make_detection(frame)])
        )
        for obs in observations:
            restored = TrackObservation.model_validate(obs.model_dump())
            assert restored == obs

    def test_validate_tracking_provenance_passes_for_matching_input(self) -> None:
        session = make_session()
        frame = make_frame(session)
        validate_tracking_provenance(frame, [make_detection(frame)])  # must not raise

    def test_box_iou_is_deterministic(self) -> None:
        a = BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5)
        b = BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5)
        disjoint = BoundingBox(x_min=0.6, y_min=0.6, x_max=0.9, y_max=0.9)
        assert box_iou(a, b) == pytest.approx(1.0)
        assert box_iou(a, disjoint) == pytest.approx(0.0)
        assert box_iou(a, b) == box_iou(a, b)


# ---------------------------------------------------------------------------
# Integration: REAL Task 12 -> Task 13 boundary (no Task 12 bypass)
# ---------------------------------------------------------------------------


class _FakeBoxes:
    def __init__(self, xyxy: list[list[float]], conf: list[list[float]], cls: list[list[int]]):
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls

    def __len__(self) -> int:
        return len(self.xyxy)


@dataclass
class _FakeResult:
    boxes: _FakeBoxes


class _MovingBoxYOLO:
    """Scripted YOLO double: one person box drifting right per call."""

    instances: ClassVar[list[_MovingBoxYOLO]] = []
    _predict_step: ClassVar[int] = 0

    def __init__(self, artifact_uri: str) -> None:
        self.artifact_uri = artifact_uri
        self.names: dict[int, str] = {0: "person", 1: "bag"}
        _MovingBoxYOLO.instances.append(self)

    def predict(self, **kwargs: Any) -> Any:
        # Emit PIXEL coordinates (the adapter normalizes them): a person
        # box drifting right, on a 640x480 frame.
        x_norm = 0.05 + 0.1 * _MovingBoxYOLO._predict_step
        _MovingBoxYOLO._predict_step += 1
        x1, x2 = round(x_norm * 640), round((x_norm + 0.08) * 640)
        boxes = _FakeBoxes([[x1, 24, x2, 240]], [[0.9]], [[0]])
        return [_FakeResult(boxes)]


class TestIntegration:
    async def test_frame_packet_to_detection_to_track_real_boundaries(
        self, fake_tracker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FramePacket -> YOLOv8Adapter -> DetectionObservation ->
        ByteTrackAdapter -> TrackObservation via the real boundaries."""
        module = types.ModuleType("ultralytics")
        module.YOLO = _MovingBoxYOLO
        monkeypatch.setitem(sys.modules, "ultralytics", module)
        monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: False)
        monkeypatch.setattr(yolo_adapter, "_mps_available", lambda: False)
        monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", lambda image: (object(), 640, 480))
        spec = ModelSpec(
            model_id="yolov8n",
            model_name="yolov8n",
            model_version="8.1.0",
            artifact_uri="memory://tracking-integration/yolov8n.pt",
            artifact_sha256="a" * 64,
            device=Device.CPU,
            class_names=("person", "bag"),
        )
        detector = YOLOv8Adapter(
            model_spec=spec,
            config=DetectorConfig(
                confidence_threshold=0.5,
                input_width=640,
                input_height=480,
                device=Device.CPU,
            ),
        )

        session = make_session()
        source = make_source()
        base = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        # The backend emits one stable track across the sequence.
        FakeBYTETracker.update_plan = [
            [FakeSTrack(1, [0.05, 0.05, 0.08, 0.45])],
            [FakeSTrack(1, [0.15, 0.05, 0.08, 0.45])],
            [FakeSTrack(1, [0.25, 0.05, 0.08, 0.45])],
        ]
        tracker = ByteTrackAdapter(session_id=session)

        track_ids: list[TrackId] = []
        linked: list[DetectionId] = []
        for index in range(3):
            frame = FramePacket(
                frame_id=FrameId(new_uuid()),
                session_id=session,
                source_ref=source,
                frame_index=index,
                event_time=base + timedelta(seconds=index),
                width=640,
                height=480,
            )
            # The REAL Task 12 adapter (fake-SDK seam) produces the
            # canonical detections for this frame.
            detections = await detector.detect(DetectionInput(frame=frame, image=b"frame-bytes"))
            assert len(detections) == 1
            # The REAL Task 13 adapter (fake-SDK seam) tracks them.
            observations = await tracker.update(TrackingInput(frame=frame, detections=detections))
            assert len(observations) == 1
            obs = observations[0]
            track_ids.append(obs.track_id)
            linked.append(obs.detection_id)
            assert obs.session_id == session
            assert obs.frame_id == frame.frame_id
            assert obs.event_time == frame.event_time
            assert obs.track_state is TrackState.ACTIVE
            assert obs.tracking_metadata is not None
            assert obs.tracking_metadata["tracker"] == "bytetrack"

        # One stable track across the whole sequence, linked to the
        # exact detection that matched it on every frame.
        assert len(set(track_ids)) == 1
        assert len(set(linked)) == 3  # each frame had its own detection


# ---------------------------------------------------------------------------
# Architectural isolation (Task 13, Step 17)
# ---------------------------------------------------------------------------


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for root in PRODUCTION_DIRS:
        files.extend(sorted(root.rglob("*.py")))
    return sorted(files)


def _rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


class TestTrackingIsolation:
    def test_no_tracking_sdk_imports_in_production(self) -> None:
        """No ``bytetrack`` import exists anywhere in production code."""
        vendor_modules = ("bytetrack", "bytetracker")
        for path in _production_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        assert root not in vendor_modules, (
                            f"{_rel(path)} imports vendor SDK '{root}'"
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    assert root not in vendor_modules, f"{_rel(path)} imports vendor SDK '{root}'"

    def test_tracking_sdk_package_string_only_in_adapter(self) -> None:
        """The package string appears only inside the designated adapter.

        The canonical ``TrackObservation`` contract may name the tracker
        in its documentation; backend/app business code may not.
        """
        import re

        pattern = re.compile(r"\bbytetrack\b")
        offenders = {
            _rel(path)
            for path in sorted((PROJECT_ROOT / "backend" / "app").rglob("*.py"))
            if pattern.search(path.read_text(encoding="utf-8").lower())
        }
        assert offenders == {"backend/app/intelligence/tracking/bytetrack_adapter.py"}, (
            f"tracking SDK references leaked outside the adapter: {sorted(offenders)}"
        )
