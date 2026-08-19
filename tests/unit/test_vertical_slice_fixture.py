"""Task 18.2 — controlled vertical-slice fixture verification.

The fixture (``tests/unit/fixtures/vertical_slice/``) is a fully
deterministic, network-free "video": 30 pure-stdlib PNG frames of a
person walking into the ROI and remaining inside.  These tests prove:

- the fixture is DETERMINISTIC — the committed bytes are exactly the
  generator output, and regeneration is a no-op;
- the golden manifest is self-consistent (metadata, trajectory,
  detections, timeline, expected event);
- the fixture decodes through the REAL Task 11 boundary
  (``FileFrameSource`` over the Task 9 ``FakeStorageAdapter``) with
  known dimensions / FPS / frame count / timestamps / frame index;
- cancellation and cleanup release every resource;
- the expected timeline drives exactly ONE logical occupancy event.

No randomness, no network, no external decode SDK.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import struct
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.intelligence.pipeline import FrameConsumer, FramePipeline
from backend.app.intelligence.sources.base import DecodeStatus, FrameData, FrameSourceState
from backend.app.intelligence.sources.decoder import DecodedFrame
from backend.app.intelligence.sources.exceptions import FrameDecodeError, SourceNotOpenError
from backend.app.intelligence.sources.file import FileFrameSource
from backend.app.intelligence.sources.queue import BoundedFrameQueue, QueuedFrame, QueueFullPolicy
from contracts.common import VideoAssetId, VideoSessionId
from contracts.video import FramePacket, SourceType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = FIXTURES_DIR / "manifest.json"
GENERATOR_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "generate_vertical_slice_fixture.py"
)

SCHEMA = "hotelops.vertical-slice/1.0"


# The generator lives in ``scripts/`` (not a package); load it by path
# with importlib so the test runs the ACTUAL committed script — same
# convention as the detection golden regression (GENERATOR_SCRIPT path).
# mypy treats the dynamic load as Any; the values are validated by the
# assertions themselves.
_generator = importlib.util.spec_from_file_location(
    "generate_vertical_slice_fixture", GENERATOR_SCRIPT
)
assert _generator is not None and _generator.loader is not None
GENERATOR = importlib.util.module_from_spec(_generator)
_generator.loader.exec_module(GENERATOR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Read the IHDR width/height from PNG bytes (pure struct parse)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    (length,) = struct.unpack(">I", data[8:12])
    assert data[12:16] == b"IHDR", "IHDR must follow the signature"
    width, height = struct.unpack(">II", data[16:24])
    assert length == 13
    return width, height


def _sha256(data: bytes) -> str:

    return hashlib.sha256(data).hexdigest()


class _FixtureDecoder:
    """Deterministic in-memory ``FrameDecoder`` for the fixture.

    Emits one ``DecodedFrame`` per committed fixture frame in order
    (EOF on exhaustion).  The container bytes from storage are received
    but not parsed — the decode library stays isolated behind the
    ``FrameDecoder`` protocol (same convention as the detection golden
    regression).  ``decode_error_at`` injects a ``FrameDecodeError`` at
    the given source positions to exercise the base source's corrupt-
    frame policy without corrupting the committed fixture.
    """

    def __init__(
        self,
        frames: list[DecodedFrame],
        *,
        decode_error_at: set[int] | None = None,
    ) -> None:
        self._frames = list(frames)
        self._decode_error_at = decode_error_at or set()
        self._read_calls = 0
        self.opened = False
        self.closed = False
        self.received_stream: AsyncIterator[bytes] | None = None

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        self.opened = True
        self.received_stream = stream

    async def read(self) -> DecodedFrame | None:
        if not self.opened:
            raise RuntimeError("decoder not opened")
        position = self._read_calls
        self._read_calls += 1
        if position in self._decode_error_at:
            raise FrameDecodeError(f"corrupt frame at source position {position}")
        if position >= len(self._frames):
            return None
        frame = self._frames[position]
        return frame

    async def close(self) -> None:
        self.closed = True


async def _object_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _build_decoded_frames(manifest: dict) -> list[DecodedFrame]:
    """One DecodedFrame per fixture frame (deterministic)."""
    meta = manifest["metadata"]
    frames: list[DecodedFrame] = []
    for frame in range(meta["frame_count"]):
        png = (FIXTURES_DIR / f"frame_{frame:03d}.png").read_bytes()
        width, height = _png_dimensions(png)
        frames.append(
            DecodedFrame(
                width=width,
                height=height,
                data=png,
                pts_seconds=frame / meta["fps"],
            )
        )
    return frames


async def _open_source(
    manifest: dict,
    *,
    decode_error_at: set[int] | None = None,
) -> tuple[FileFrameSource, _FixtureDecoder, FakeStorageAdapter]:
    """Seed storage with the fixture object and open FileFrameSource."""
    storage = FakeStorageAdapter()
    # The object bytes are the concatenated fixture frames — a stand-in
    # for the recorded-video container (deterministic, no network).
    object_data = b"".join(
        (FIXTURES_DIR / f"frame_{frame:03d}.png").read_bytes()
        for frame in range(manifest["metadata"]["frame_count"])
    )
    await storage.put_object_stream(
        "tenants/fixture/vertical_slice/recording.bin",
        _object_stream(object_data),
        content_type="video/mp4",
        size_bytes=len(object_data),
    )
    decoder = _FixtureDecoder(_build_decoded_frames(manifest), decode_error_at=decode_error_at)
    capture = datetime.fromisoformat(manifest["metadata"]["capture_time"])
    source = FileFrameSource(
        session_id=VideoSessionId(uuid.uuid4()),
        source_ref=VideoAssetId(uuid.uuid4()),
        storage=storage,
        object_key="tenants/fixture/vertical_slice/recording.bin",
        decoder=decoder,
        capture_time=capture,
    )
    return source, decoder, storage


# ---------------------------------------------------------------------------
# Determinism — regeneration must be a no-op
# ---------------------------------------------------------------------------


class TestFixtureDeterminism:
    """The committed fixture is byte-identical to generator output."""

    def test_committed_frames_match_generator_output(self) -> None:
        gen = GENERATOR
        for frame in range(gen.FRAME_COUNT):
            present = gen.person_present(frame)
            expected = gen.render_frame(
                gen.WIDTH,
                gen.HEIGHT,
                person=present,
                person_x=gen.box_x(frame) if present else 0,
            )
            committed = (FIXTURES_DIR / f"frame_{frame:03d}.png").read_bytes()
            assert committed == expected, f"frame_{frame:03d}.png drifted"

    def test_committed_manifest_matches_generator_output(self) -> None:
        committed = json.loads(MANIFEST.read_text())
        recomputed = json.dumps(GENERATOR.expected_timeline(), indent=2, sort_keys=True)
        # Deterministic JSON — the timeline serializes identically.
        assert json.dumps(committed["timeline"], indent=2, sort_keys=True) == recomputed

    def test_regeneration_is_byte_stable(self) -> None:
        before = {f.name: _sha256(f.read_bytes()) for f in sorted(FIXTURES_DIR.glob("*.png"))}
        # Re-running the generator must not change any committed byte.
        GENERATOR.main()
        after = {f.name: _sha256(f.read_bytes()) for f in sorted(FIXTURES_DIR.glob("*.png"))}
        assert after == before

    def test_manifest_schema_and_determinism_flags(self) -> None:
        manifest = _load_manifest()
        assert manifest["schema"] == SCHEMA
        assert manifest["metadata"]["deterministic"] is True
        assert manifest["metadata"]["no_network"] is True
        assert manifest["metadata"]["source_type"] == "recorded"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestFixtureMetadata:
    def test_metadata_matches_committed_frames(self) -> None:
        manifest = _load_manifest()
        meta = manifest["metadata"]
        frame_files = sorted(FIXTURES_DIR.glob("frame_*.png"))
        assert len(frame_files) == meta["frame_count"]
        for frame_file in frame_files:
            width, height = _png_dimensions(frame_file.read_bytes())
            assert (width, height) == (meta["width"], meta["height"])
        assert meta["fps"] > 0
        # Timestamps: capture time is a fixed UTC instant (never wall clock).
        capture = datetime.fromisoformat(meta["capture_time"])
        assert capture.tzinfo is not None

    def test_timeline_is_complete_and_monotonic(self) -> None:
        manifest = _load_manifest()
        timeline = manifest["timeline"]
        assert len(timeline) == manifest["metadata"]["frame_count"]
        indexes = [entry["frame_index"] for entry in timeline]
        assert indexes == list(range(manifest["metadata"]["frame_count"]))
        capture = datetime.fromisoformat(manifest["metadata"]["capture_time"])
        for entry in timeline:
            # Event-time derived deterministically: capture + index / FPS.
            expected = capture + timedelta(
                seconds=entry["frame_index"] / manifest["metadata"]["fps"]
            )
            assert datetime.fromisoformat(entry["event_time"]) == expected

    def test_trajectory_constants_are_consistent(self) -> None:
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        timeline = manifest["timeline"]
        assert traj["enter_frame"] < traj["inside_roi_from"] < traj["exit_frame"]
        assert traj["empty_from"] > traj["exit_frame"]
        assert traj["empty_from"] <= manifest["metadata"]["frame_count"]
        # The person is present exactly on [enter, empty_from).
        for entry in timeline:
            present = traj["enter_frame"] <= entry["frame_index"] < traj["empty_from"]
            assert entry["person_present"] == present


# ---------------------------------------------------------------------------
# Golden detections + ROI + expected timeline
# ---------------------------------------------------------------------------


class TestGoldenTimeline:
    def test_golden_detections_cover_present_frames_only(self) -> None:
        manifest = _load_manifest()
        for entry in manifest["timeline"]:
            detections = entry["golden_detections"]
            if entry["person_present"]:
                assert len(detections) == 1
                det = detections[0]
                assert det["class_name"] == "person"
                assert 0.0 <= det["confidence"] <= 1.0
                # Bounding box exactly matches the rendered rectangle:
                # deterministic, on-frame, non-degenerate.
                assert det["x1"] < det["x2"] and det["y1"] < det["y2"]
                assert det["x1"] >= 0 and det["y1"] >= 0
                assert det["x2"] <= manifest["metadata"]["width"]
                assert det["y2"] <= manifest["metadata"]["height"]
            else:
                assert detections == []

    def test_golden_tracks_are_one_stable_identity(self) -> None:
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        active_ids: set[str] = set()
        for entry in manifest["timeline"]:
            tracks = entry["golden_tracks"]
            if entry["person_present"]:
                assert len(tracks) == 1
                track = tracks[0]
                assert track["track_id"] == "track-person-001"
                assert track["class_name"] == "person"
                assert track["state"] == "active"
                active_ids.add(track["track_id"])
            else:
                assert tracks == []
        # ONE identity for the whole episode — no fragmentation.
        assert active_ids == {"track-person-001"}
        # The track exists across the full [enter, empty_from) interval.
        on_frame_count = sum(1 for e in manifest["timeline"] if e["person_present"])
        assert on_frame_count == traj["empty_from"] - traj["enter_frame"]

    def test_roi_membership_matches_generator_geometry(self) -> None:
        manifest = _load_manifest()
        for entry in manifest["timeline"]:
            assert entry["person_inside_roi"] == GENERATOR.person_inside_roi(entry["frame_index"])

    def test_roi_membership_matches_trajectory_constants(self) -> None:
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        for entry in manifest["timeline"]:
            inside = traj["inside_roi_from"] <= entry["frame_index"] < traj["empty_from"]
            assert entry["person_inside_roi"] == inside

    def test_presence_confirmation_timeline(self) -> None:
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        confirmed_frame = traj["enter_frame"] + 1
        for entry in manifest["timeline"]:
            if entry["frame_index"] == confirmed_frame:
                assert entry["presence"] == "enter_confirmed"
            elif entry["person_present"]:
                assert entry["presence"] == "present"
            else:
                assert entry["presence"] == "absent"

    def test_expected_event_is_single_occupancy_session(self) -> None:
        manifest = _load_manifest()
        events = manifest["expected_events"]
        assert len(events) == 1
        event = events[0]
        assert event["event_type"] == "operational.occupancy_session"
        assert event["phase"] == "started"
        assert event["count"] == 1
        # Entry confirmation frame — the single golden trigger.
        traj = manifest["trajectory"]
        assert event["trigger_frame"] == traj["enter_frame"] + 1


# ---------------------------------------------------------------------------
# Decoding through the REAL Task 11 boundary (FileFrameSource)
# ---------------------------------------------------------------------------


class TestDecodingViaFileFrameSource:
    async def test_full_decode_dimensions_fps_count_timestamps(self) -> None:
        manifest = _load_manifest()
        meta = manifest["metadata"]
        source, decoder, _storage = await _open_source(manifest)

        packets = []
        try:
            async with source:
                async for packet in source:
                    packets.append(packet)
        finally:
            await source.aclose()

        # Frame count and provenance index sequence.
        assert len(packets) == meta["frame_count"]
        assert [p.frame_index for p in packets] == list(range(meta["frame_count"]))
        # Dimensions.
        assert all(p.width == meta["width"] and p.height == meta["height"] for p in packets)
        # Recorded source type + shared session identity.
        assert source.source_type is SourceType.RECORDED
        assert len({p.session_id for p in packets}) == 1
        # Event-time: capture_time + pts (frame_index / FPS), deterministic.
        capture = datetime.fromisoformat(meta["capture_time"])
        for packet in packets:
            expected = capture + timedelta(seconds=packet.frame_index / meta["fps"])
            assert packet.event_time == expected
            assert packet.event_time.tzinfo is not None
        # Strictly increasing, never wall-clock.
        times = [p.event_time for p in packets]
        assert times == sorted(times)
        assert len(set(times)) == len(times)
        # Decoder fully consumed + closed by source close.
        assert decoder.closed

    async def test_cancellation_releases_resources(self) -> None:
        manifest = _load_manifest()
        source, decoder, _storage = await _open_source(manifest)

        async with source:
            count = 0
            async for _packet in source:
                count += 1
                if count == 3:
                    break  # cancel mid-iteration
        # close() after cancellation: decoder closed, stream released.
        await source.aclose()
        assert decoder.closed
        assert decoder.received_stream is not None
        # The object stream must be closed by the source: an async
        # generator whose frame has been exhausted is closed.
        stream = decoder.received_stream
        assert stream.ag_frame is None

    async def test_close_is_idempotent_and_safe_before_open(self) -> None:
        manifest = _load_manifest()
        source, decoder, _storage = await _open_source(manifest)
        await source.aclose()  # close before open — must be safe
        assert decoder.closed
        await source.aclose()  # double close — idempotent
        assert decoder.closed
        # Reading without open fails explicitly (never a silent empty stream).
        with pytest.raises(SourceNotOpenError):
            await source._produce_next()

    async def test_packet_payload_is_the_rendered_frame(self) -> None:
        manifest = _load_manifest()
        source, _decoder, _storage = await _open_source(manifest)
        meta = manifest["metadata"]
        seen = 0
        async with source:
            async for packet in source:
                data = source.last_frame_data
                assert data is not None
                # The decoded pixel payload is the exact committed PNG.
                committed = (FIXTURES_DIR / f"frame_{packet.frame_index:03d}.png").read_bytes()
                assert data.data == committed
                assert data.pts_seconds == pytest.approx(packet.frame_index / meta["fps"])
                seen += 1
        assert seen == meta["frame_count"]


# ---------------------------------------------------------------------------
# Task 18.3 — FILE INGESTION VERTICAL SLICE
#
# Fixture → FileFrameSource → FramePipeline (bounded queue) → FrameConsumer.
# The consumer receives canonical (FramePacket, FrameData) pairs and MUST
# NOT be able to tell the source is a file — the FramePacket contract
# carries no live/recorded discriminator (ADR-005).
# ---------------------------------------------------------------------------


class _SliceConsumer:
    """Source-agnostic CV consumer: records (packet, data) pairs.

    It never reads a source type — FramePacket has none — so it cannot
    branch on live vs recorded (this IS the transparency property).
    """

    def __init__(self) -> None:
        self.frames: list[tuple[FramePacket, FrameData]] = []

    async def consume(self, frame: QueuedFrame) -> None:
        self.frames.append((frame.packet, frame.data))


class _SlowConsumer(_SliceConsumer):
    """Consumer that sleeps per frame to force backpressure/queue-full."""

    def __init__(self, *, delay: float = 0.001) -> None:
        super().__init__()
        self._delay = delay

    async def consume(self, frame: QueuedFrame) -> None:
        await asyncio.sleep(self._delay)
        await super().consume(frame)


def _make_slice_pipeline(
    consumer: FrameConsumer, *, maxsize: int, policy: QueueFullPolicy
) -> FramePipeline:
    return FramePipeline(
        queue=BoundedFrameQueue(maxsize=maxsize, full_policy=policy),
        consumer=consumer,
    )


class TestFileIngestionVerticalSlice:
    """The fixture drives the full ingestion boundary."""

    async def test_successful_decode_all_canonical_fields(self) -> None:
        """Every FramePacket carries the canonical contract fields + companion."""
        manifest = _load_manifest()
        meta = manifest["metadata"]
        source, decoder, _storage = await _open_source(manifest)
        consumer = _SliceConsumer()
        pipeline = _make_slice_pipeline(consumer, maxsize=16, policy=QueueFullPolicy.BLOCK)
        await pipeline.run(source)

        assert len(consumer.frames) == meta["frame_count"]
        packets = [p for p, _ in consumer.frames]

        # session_id — one shared session across the whole recording.
        assert len({p.session_id for p in packets}) == 1
        # frame_index — monotonic from 0, no gaps, no reordering.
        assert [p.frame_index for p in packets] == list(range(meta["frame_count"]))
        # frame_id — unique per frame (provenance identity).
        assert len({p.frame_id for p in packets}) == len(packets)
        # source_ref — the recorded asset identity stamped on every packet.
        assert all(p.source_ref == source.source_ref for p in packets)
        # event timestamp — capture + pts, deterministic, strictly ordered.
        capture = datetime.fromisoformat(meta["capture_time"])
        times = [p.event_time for p in packets]
        assert times == sorted(times)
        assert len(set(times)) == len(times)
        for packet in packets:
            assert packet.event_time.tzinfo is not None
            assert packet.event_time == capture + timedelta(
                seconds=packet.frame_index / meta["fps"]
            )
        # width / height.
        assert all(p.width == meta["width"] and p.height == meta["height"] for p in packets)
        # Companion FrameData: source timestamp + decode status + payload.
        for packet, data in consumer.frames:
            assert data.frame_index == packet.frame_index
            assert data.decode_status is DecodeStatus.OK
            assert data.source_timestamp is None  # recorded: pts-based timing
            assert data.pts_seconds == pytest.approx(packet.frame_index / meta["fps"])
            committed = (FIXTURES_DIR / f"frame_{packet.frame_index:03d}.png").read_bytes()
            assert data.data == committed
        # Provenance: versioned schema on the packet, clean lifecycle.
        assert all(p.schema_version for p in packets)
        assert source.state is FrameSourceState.CLOSED
        assert decoder.closed

    async def test_corrupt_frame_skipped_and_sequence_continues(self) -> None:
        """A corrupt frame is counted + skipped; ordering and payload stay intact."""
        manifest = _load_manifest()
        meta = manifest["metadata"]
        # Corrupt every 7th source position (isolated failures, well under
        # the consecutive-error limit) — never the fixture files themselves.
        corrupt_at = {7, 14, 21}
        source, decoder, _storage = await _open_source(manifest, decode_error_at=corrupt_at)
        consumer = _SliceConsumer()
        pipeline = _make_slice_pipeline(consumer, maxsize=16, policy=QueueFullPolicy.BLOCK)
        await pipeline.run(source)

        # 3 corrupt frames skipped → 27 delivered, indices still 0..26
        # (decode failures do not consume a frame index).
        assert len(consumer.frames) == meta["frame_count"] - len(corrupt_at)
        packets = [p for p, _ in consumer.frames]
        assert [p.frame_index for p in packets] == list(range(meta["frame_count"] - 3))
        assert source.decode_errors == 3
        assert source.state is FrameSourceState.CLOSED
        assert decoder.closed

    async def test_timestamp_behavior_is_deterministic_event_time(self) -> None:
        """Event-time is capture+pts — never wall clock, never processing time."""
        manifest = _load_manifest()
        meta = manifest["metadata"]
        source, _decoder, _storage = await _open_source(manifest)
        consumer = _SliceConsumer()
        pipeline = _make_slice_pipeline(consumer, maxsize=16, policy=QueueFullPolicy.BLOCK)
        await pipeline.run(source)

        capture = datetime.fromisoformat(meta["capture_time"])
        for packet, data in consumer.frames:
            # Event timestamp = capture + pts_seconds (deterministic).
            assert packet.event_time == capture + timedelta(seconds=data.pts_seconds)
            # pts advances at the fixture FPS.
            assert data.pts_seconds == pytest.approx(packet.frame_index / meta["fps"])

    async def test_frame_ordering_preserved_through_queue(self) -> None:
        """Frames arrive at the consumer in exact source order (no reorder)."""
        manifest = _load_manifest()
        source, _decoder, _storage = await _open_source(manifest)
        consumer = _SliceConsumer()
        # BLOCK policy + fast consumer: every frame, in order.
        pipeline = _make_slice_pipeline(consumer, maxsize=4, policy=QueueFullPolicy.BLOCK)
        await pipeline.run(source)
        indices = [p.frame_index for p, _ in consumer.frames]
        assert indices == list(range(manifest["metadata"]["frame_count"]))
        # Payload order matches the manifest's golden trajectory (detections
        # present exactly on the on-frame interval).
        traj = manifest["trajectory"]
        for _i, (packet, data) in enumerate(consumer.frames):
            present = traj["enter_frame"] <= packet.frame_index < traj["empty_from"]
            # PNG payloads differ between empty and occupied frames — assert
            # the ordering reflects the fixture timeline deterministically.
            assert (data.data != (FIXTURES_DIR / "frame_000.png").read_bytes()) == present

    async def test_cancellation_releases_source_and_queue(self) -> None:
        """Cancelling the pipeline mid-run closes the source, decoder, queue."""
        manifest = _load_manifest()
        source, decoder, _storage = await _open_source(manifest)
        consumer = _SlowConsumer(delay=0.01)
        pipeline = _make_slice_pipeline(consumer, maxsize=2, policy=QueueFullPolicy.BLOCK)

        task = asyncio.create_task(pipeline.run(source))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Guaranteed cleanup on cancellation (async with in pipeline.run).
        assert source.state is FrameSourceState.CLOSED
        assert decoder.closed
        assert pipeline.queue.closed

    async def test_resource_cleanup_after_consumer_failure(self) -> None:
        """A consumer failure still releases the source and the queue."""
        manifest = _load_manifest()
        source, decoder, _storage = await _open_source(manifest)

        class _FailingConsumer:
            async def consume(self, frame: QueuedFrame) -> None:
                raise RuntimeError("cv consumer failure")

        pipeline = _make_slice_pipeline(
            _FailingConsumer(), maxsize=16, policy=QueueFullPolicy.BLOCK
        )
        with pytest.raises(RuntimeError, match="cv consumer failure"):
            await pipeline.run(source)
        assert source.state is FrameSourceState.CLOSED
        assert decoder.closed
        assert pipeline.queue.closed

    async def test_bounded_queue_block_policy_delivers_every_frame(self) -> None:
        """BLOCK: zero loss under backpressure (recorded semantics)."""
        manifest = _load_manifest()
        meta = manifest["metadata"]
        source, _decoder, _storage = await _open_source(manifest)
        consumer = _SlowConsumer(delay=0.0005)
        pipeline = _make_slice_pipeline(consumer, maxsize=2, policy=QueueFullPolicy.BLOCK)
        await pipeline.run(source)

        assert len(consumer.frames) == meta["frame_count"]
        assert pipeline.queue.dropped_frames == 0
        assert pipeline.queue.total_enqueued == meta["frame_count"]
        assert pipeline.queue.total_dequeued == meta["frame_count"]
        assert source.dropped_frames == 0

    async def test_bounded_queue_drop_oldest_counts_drops(self) -> None:
        """DROP_OLDEST: bounded latency, counted loss, newest wins."""
        manifest = _load_manifest()
        meta = manifest["metadata"]
        source, _decoder, _storage = await _open_source(manifest)
        consumer = _SlowConsumer(delay=0.002)
        pipeline = _make_slice_pipeline(consumer, maxsize=2, policy=QueueFullPolicy.DROP_OLDEST)
        await pipeline.run(source)

        # Loss is counted on BOTH the queue and the source (observability
        # stays in sync), and the queue admitted every produced frame.
        assert pipeline.queue.dropped_frames > 0
        assert source.dropped_frames == pipeline.queue.dropped_frames
        assert pipeline.queue.total_enqueued == meta["frame_count"]
        # Delivered frames are the newest survivors — still strictly
        # increasing (a suffix of the original sequence).
        delivered = [p.frame_index for p, _ in consumer.frames]
        assert delivered == sorted(delivered)
        assert len(delivered) == meta["frame_count"] - pipeline.queue.dropped_frames


class TestDownstreamCannotTellSourceIsAFile:
    """The consumer's input is schema-identical for live and recorded."""

    async def test_fixture_frames_consumed_via_source_agnostic_contract(self) -> None:
        """The slice consumer receives canonical FramePackets — no file knowledge."""
        manifest = _load_manifest()
        source, _decoder, _storage = await _open_source(manifest)
        consumer = _SliceConsumer()
        pipeline = _make_slice_pipeline(consumer, maxsize=16, policy=QueueFullPolicy.BLOCK)
        await pipeline.run(source)
        packets = [p for p, _ in consumer.frames]
        # The FramePacket contract has NO live/recorded discriminator.
        assert not hasattr(packets[0], "source_type")
        assert not hasattr(packets[0], "file_path")
        assert not hasattr(packets[0], "storage_key")
        # And the consumer never needed one — it processed all frames.
        assert len(packets) == manifest["metadata"]["frame_count"]

    async def test_same_consumer_contract_accepts_an_rtsp_producer(self) -> None:
        """Downstream code cannot depend on the source being a file: the same
        consumer contract is satisfied by a live producer."""
        from backend.app.intelligence.sources.exceptions import SourceTerminatedError
        from tests.unit.test_pipeline_integration import (
            make_file_source,
            make_rtsp_frame,
            make_rtsp_source,
        )

        consumer = _SliceConsumer()
        # A recorded source (any frames — the contract is what matters).
        file_source, _ = await make_file_source(
            frames=[
                DecodedFrame(width=320, height=240, data=b"a" * 16, pts_seconds=0.0),
                DecodedFrame(width=320, height=240, data=b"b" * 16, pts_seconds=0.1),
            ]
        )
        await _make_slice_pipeline(consumer, maxsize=8, policy=QueueFullPolicy.BLOCK).run(
            file_source
        )
        # A live producer feeds the SAME consumer type.
        rtsp_source, transport, _ = make_rtsp_source(
            frames_per_session=[[make_rtsp_frame(payload=1)]]
        )
        with pytest.raises(SourceTerminatedError):  # live session exhausts
            await _make_slice_pipeline(consumer, maxsize=8, policy=QueueFullPolicy.BLOCK).run(
                rtsp_source
            )
        assert transport.disconnect_calls >= 1
        # One consumer received both: no branching, no source knowledge.
        packets = [p for p, _ in consumer.frames]
        assert len(packets) == 3
        assert [p.frame_index for p in packets] == [0, 1, 0]
        # The consumer's packets carry no file discriminator in either case.
        assert all(not hasattr(p, "source_type") for p in packets)
