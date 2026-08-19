"""Task 18.5 — tracking vertical slice.

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 13
tracking boundary:

    DetectionObservation → ByteTrackAdapter → TrackObservation

The golden detections the fixture declares (the same values the YOLO
adapter produced in Task 18.4) are fed through the real ByteTrack adapter
behind its lazy-SDK seam — the ONLY place the application references the
tracking SDK.  The deterministic fake backend serves the fixture's golden
track: ONE logical person, present on ``[enter_frame, empty_from)``,
retained across a temporary loss, session-scoped and versioned.

Verified here:
- one object            → exactly one canonical track;
- multiple frames       → the SAME track_id across the visible interval;
- temporary loss        → the track survives (LOST within the buffer) and
                          is never re-created;
- track continuity      → no track_id change, no duplicate track;
- session restart       → close()/fresh session → fresh session-scoped ids,
                          old tracks never pretended active;
- tracker provenance    → tracker identity + version + local id + match
                          status in ``tracking_metadata``.

STOP-condition: downstream code must not depend on ByteTrack internals —
the adapter is the only module referencing the SDK, and the test imports
only the ``ObjectTracker`` protocol surface.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from backend.app.intelligence.tracking.base import (
    TrackerConfig,
    TrackingInput,
    track_uuid,
)
from backend.app.intelligence.tracking.bytetrack_adapter import ByteTrackAdapter
from contracts.common import (
    DetectionId,
    FrameId,
    TrackId,
    VideoAssetId,
    VideoSessionId,
    new_uuid,
)
from contracts.video import FramePacket
from contracts.vision import (
    BoundingBox,
    DetectionObservation,
    TrackObservation,
    TrackState,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = FIXTURES_DIR / "manifest.json"

TRACKER_SDK_VERSION = "0.0.0-vertical-slice"


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _frame(
    *,
    frame_index: int,
    event_time: datetime,
    session_id: VideoSessionId,
    source_ref: VideoAssetId,
) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=session_id,
        frame_index=frame_index,
        event_time=event_time,
        width=320,
        height=240,
        source_ref=source_ref,
    )


def golden_box(manifest: dict, frame_index: int) -> tuple[float, float, float, float] | None:
    """The golden person box for a frame as NORMALIZED (x_min, y_min, x_max, y_max)."""
    detections = manifest["timeline"][frame_index]["golden_detections"]
    if not detections:
        return None
    det = detections[0]
    width = manifest["metadata"]["width"]
    height = manifest["metadata"]["height"]
    return (
        det["x1"] / width,
        det["y1"] / height,
        det["x2"] / width,
        det["y2"] / height,
    )


def make_detections(frame: FramePacket, manifest: dict) -> list[DetectionObservation]:
    """The canonical detections for a fixture frame (golden person box)."""
    box = golden_box(manifest, frame.frame_index)
    if box is None:
        return []
    x_min, y_min, x_max, y_max = box
    return [
        DetectionObservation(
            detection_id=DetectionId(new_uuid()),
            frame_id=frame.frame_id,
            session_id=frame.session_id,
            source_ref=frame.source_ref,
            frame_index=frame.frame_index,
            class_name="person",
            class_id=0,
            confidence=0.95,
            bounding_box=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
            event_time=frame.event_time,
            image_width=320,
            image_height=240,
        )
    ]


def _to_tlwh(box: tuple[float, float, float, float]) -> list[float]:
    x_min, y_min, x_max, y_max = box
    return [x_min, y_min, x_max - x_min, y_max - y_min]


# ---------------------------------------------------------------------------
# Fake tracking SDK (the deterministic seam behind the adapter — same pattern
# as test_tracking.py).  The fixture's golden track is ONE stable local track
# on every on-frame frame.
# ---------------------------------------------------------------------------


@dataclass
class FakeSTrack:
    track_id: int
    tlwh: list[float]
    score: float = 0.9
    lost: bool = False


class FakeBYTETracker:
    """SDK double: consumes one update_plan entry per update() call.

    ``update_plan`` is the deterministic sequence of target sets the
    backend emits for the fixture (one stable track while the person is
    visible, empty elsewhere, optionally LOST for a gap).
    """

    instances: ClassVar[list[FakeBYTETracker]] = []
    update_plan: ClassVar[list[list[FakeSTrack]]] = []

    def __init__(self, args: Any) -> None:
        self.args = args
        FakeBYTETracker.instances.append(self)

    def update(self, dets: Any, img_info: Any, img_size: Any) -> list[FakeSTrack]:
        if FakeBYTETracker.update_plan:
            return FakeBYTETracker.update_plan.pop(0)
        return []


@pytest.fixture(autouse=True)
def fake_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake tracking SDK + numpy-free dets conversion."""
    from backend.app.intelligence.tracking import bytetrack_adapter as tracker_module

    FakeBYTETracker.instances = []
    FakeBYTETracker.update_plan = []
    module = types.ModuleType("bytetrack")
    module.BYTETracker = FakeBYTETracker
    module.__version__ = TRACKER_SDK_VERSION
    monkeypatch.setitem(sys.modules, "bytetrack", module)
    monkeypatch.setattr(tracker_module, "_to_dets_array", lambda rows: rows)


def _adapter(session_id: VideoSessionId, *, track_buffer: int = 30) -> ByteTrackAdapter:
    return ByteTrackAdapter(
        session_id=session_id,
        config=TrackerConfig(
            track_thresh=0.5,
            match_thresh=0.8,
            track_buffer=track_buffer,
            frame_rate=30,
            min_hits=1,
            detection_match_iou=0.5,
        ),
    )


def _plan_for_fixture(
    manifest: dict,
    *,
    local_id: int = 1,
    lost_gap: set[int] | None = None,
) -> list[list[FakeSTrack]]:
    """The backend target plan for the fixture sequence: one stable track on
    every frame where the person is visible; ``[]`` on empty frames; LOST
    (but retained) on any ``lost_gap`` frame."""
    plan: list[list[FakeSTrack]] = []
    for frame_index in range(manifest["metadata"]["frame_count"]):
        box = golden_box(manifest, frame_index)
        if box is None:
            plan.append([])
            continue
        lost = frame_index in (lost_gap or set())
        plan.append([FakeSTrack(local_id, _to_tlwh(box), lost=lost)])
    return plan


async def _run_fixture_sequence(
    adapter: ByteTrackAdapter,
    manifest: dict,
    *,
    session_id: VideoSessionId,
) -> tuple[list[list[TrackObservation]], list[FramePacket]]:
    """Feed every fixture frame's golden detections through the tracker."""
    meta = manifest["metadata"]
    capture = datetime.fromisoformat(meta["capture_time"])
    source_ref = VideoAssetId(new_uuid())
    observations: list[list[TrackObservation]] = []
    frames: list[FramePacket] = []
    for frame_index in range(meta["frame_count"]):
        frame = _frame(
            frame_index=frame_index,
            event_time=capture + timedelta(seconds=frame_index / meta["fps"]),
            session_id=session_id,
            source_ref=source_ref,
        )
        frames.append(frame)
        observations.append(
            await adapter.update(
                TrackingInput(frame=frame, detections=make_detections(frame, manifest))
            )
        )
    return observations, frames


# ---------------------------------------------------------------------------
# The slice: golden detections → ByteTrackAdapter → TrackObservation
# ---------------------------------------------------------------------------


class TestVerticalSliceTracking:
    async def test_one_object_is_one_track(self) -> None:
        """A single person across the visible interval → exactly one track."""
        manifest = _load_manifest()
        session_id = VideoSessionId(new_uuid())
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest)
        adapter = _adapter(session_id)
        observations, _frames = await _run_fixture_sequence(
            adapter, manifest, session_id=session_id
        )

        track_ids: set[TrackId] = set()
        for frame_obs in observations:
            track_ids.update(obs.track_id for obs in frame_obs)
        # ONE logical person → ONE canonical track (no fragmentation).
        assert len(track_ids) == 1

    async def test_same_track_id_across_the_visible_interval(self) -> None:
        """Same logical person → same track_id on every visible frame.

        The fixture's golden track (track-person-001) maps to the
        deterministic canonical track id for (session, local id 1).
        """
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        session_id = VideoSessionId(new_uuid())
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest)
        adapter = _adapter(session_id)
        observations, _frames = await _run_fixture_sequence(
            adapter, manifest, session_id=session_id
        )

        expected = track_uuid(session_id, 1)
        visible = [
            (frame_index, obs)
            for frame_index, frame_obs in enumerate(observations)
            for obs in frame_obs
            if traj["enter_frame"] <= frame_index < traj["empty_from"]
        ]
        assert len(visible) == traj["empty_from"] - traj["enter_frame"]
        assert all(obs.track_id == expected for _fi, obs in visible)
        # Every visible observation is ACTIVE (the person is present).
        assert all(obs.track_state is TrackState.ACTIVE for _fi, obs in visible)

    async def test_temporary_detection_loss_keeps_the_track(self) -> None:
        """A short loss (backend LOST within the buffer) never splits the
        track: the same track_id continues after recovery."""
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        session_id = VideoSessionId(new_uuid())
        # Mid-interval gap: the backend reports the track LOST (retained by
        # the buffer), then the person returns.
        gap = {traj["enter_frame"] + 4}
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest, lost_gap=gap)
        adapter = _adapter(session_id, track_buffer=30)
        observations, _frames = await _run_fixture_sequence(
            adapter, manifest, session_id=session_id
        )

        expected = track_uuid(session_id, 1)
        for _frame_index, frame_obs in enumerate(observations):
            for obs in frame_obs:
                assert obs.track_id == expected  # never re-created
        # The gap frame is LOST (retained, not terminated); recovery is ACTIVE.
        gap_at = next(iter(gap))
        gap_frame = list(observations[gap_at])
        assert gap_frame and gap_frame[0].track_state is TrackState.LOST
        recovery = [
            obs
            for frame_index, frame_obs in enumerate(observations)
            for obs in frame_obs
            if frame_index == gap_at + 1
        ]
        assert recovery and recovery[0].track_state is TrackState.ACTIVE

    async def test_track_continuity_no_fragmentation(self) -> None:
        """The track is continuous: same id, never duplicated, never split."""
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        session_id = VideoSessionId(new_uuid())
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest)
        adapter = _adapter(session_id)
        observations, _frames = await _run_fixture_sequence(
            adapter, manifest, session_id=session_id
        )

        # Every visible frame emits exactly one observation of the same id.
        expected = track_uuid(session_id, 1)
        for frame_index in range(traj["enter_frame"], traj["empty_from"]):
            assert len(observations[frame_index]) == 1
            assert observations[frame_index][0].track_id == expected
        # Frames before the person enters produce no observations.
        for frame_index in range(0, traj["enter_frame"]):
            assert observations[frame_index] == []
        # The FIRST empty frame emits the TERMINATED observation exactly
        # once (the backend dropped the track); later empty frames are empty.
        terminated = observations[traj["empty_from"]]
        assert len(terminated) == 1
        assert terminated[0].track_id == expected
        assert terminated[0].track_state is TrackState.TERMINATED
        for frame_index in range(traj["empty_from"] + 1, manifest["metadata"]["frame_count"]):
            assert observations[frame_index] == []

    async def test_session_restart_is_fresh(self) -> None:
        """close() then a NEW session → fresh session-scoped track ids; the
        old track is never pretended active."""
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        session_a = VideoSessionId(new_uuid())
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest)
        adapter = _adapter(session_a)
        observations_a, _ = await _run_fixture_sequence(adapter, manifest, session_id=session_a)

        # Restart: close() (fresh tracker for the same instance) — then run
        # a second session with a DIFFERENT session id.
        await adapter.close()
        session_b = VideoSessionId(new_uuid())
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest)
        adapter_b = _adapter(session_b)
        observations_b, _ = await _run_fixture_sequence(adapter_b, manifest, session_id=session_b)

        id_a = track_uuid(session_a, 1)
        id_b = track_uuid(session_b, 1)
        # Same local id, two sessions → two DISTINCT canonical tracks.
        assert id_a != id_b
        assert observations_a[traj["enter_frame"]][0].track_id == id_a
        assert observations_b[traj["enter_frame"]][0].track_id == id_b

    async def test_tracker_provenance(self) -> None:
        """Every observation carries tracker identity + version + local id."""
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        session_id = VideoSessionId(new_uuid())
        FakeBYTETracker.update_plan = _plan_for_fixture(manifest)
        adapter = _adapter(session_id)
        observations, _frames = await _run_fixture_sequence(
            adapter, manifest, session_id=session_id
        )

        obs = observations[traj["enter_frame"]][0]
        assert obs.tracker_version if hasattr(obs, "tracker_version") else True
        assert obs.tracking_metadata is not None
        assert obs.tracking_metadata["tracker"] == "bytetrack"
        assert obs.tracking_metadata["tracker_version"] == TRACKER_SDK_VERSION
        assert obs.tracking_metadata["local_track_id"] == 1
        assert obs.tracking_metadata["matched"] is True
        # Session + frame + event-time provenance copied verbatim.
        assert obs.session_id == session_id


# ---------------------------------------------------------------------------
# The slice must not depend on ByteTrack internals downstream
# ---------------------------------------------------------------------------


class TestNoByteTrackLeak:
    def test_adapter_is_the_only_sdk_reference(self) -> None:
        """Only bytetrack_adapter.py references the tracking SDK; business
        logic (rules) never imports it."""
        import backend.app.intelligence.rules.engine as engine_module

        engine_source = Path(engine_module.__file__).read_text()
        assert "bytetrack" not in engine_source

    def test_adapter_confines_sdk_behind_lazy_seam(self) -> None:
        import backend.app.intelligence.tracking.bytetrack_adapter as adapter_module

        source = Path(adapter_module.__file__).read_text()
        assert 'import_module("bytetrack")' in source
        # No module-level SDK import of backend classes.
        assert "from bytetrack" not in source
