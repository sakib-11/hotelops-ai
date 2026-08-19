"""Integration tests for the ingestion → CV boundary (Task 11, Phase 7).

Proves that BOTH ``FileFrameSource`` (recorded) and ``RTSPFrameSource``
(live) reach the SAME downstream processing boundary through the SAME
source-agnostic ``FramePipeline`` pump:

    FileFrameSource ──┐
                      ├──> FrameSource ──> BoundedFrameQueue ──> FrameConsumer
    RTSPFrameSource ──┘

The downstream consumer receives canonical ``(FramePacket, FrameData)``
pairs and MUST NOT branch on the source type — the ``FramePacket``
schema carries no live/recorded discriminator (ADR-005).  These tests
verify: identical downstream input from both sources, no source-class
leakage into the CV layer, queue-full policy integration (BLOCK =
zero loss, DROP_OLDEST = counted drops), failure propagation
(consumer error / source termination), cancellation cleanup, and the
already-shut-down-queue guard.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.intelligence.pipeline import FrameConsumer, FramePipeline
from backend.app.intelligence.sources.base import FrameSource, FrameSourceState
from backend.app.intelligence.sources.decoder import DecodedFrame
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    QueueClosedError,
    SourceTerminatedError,
)
from backend.app.intelligence.sources.file import FileFrameSource
from backend.app.intelligence.sources.queue import BoundedFrameQueue, QueuedFrame, QueueFullPolicy
from backend.app.intelligence.sources.rtsp import ReconnectPolicy, RTSPFrameSource
from contracts.common import VideoAssetId, VideoSessionId
from contracts.video import FramePacket, SourceType

CAPTURE_TIME = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
FILE_KEY = "tenants/t1/venues/v1/recordings/2026/07/29/cam1.mp4"


def make_session_id() -> VideoSessionId:
    return VideoSessionId(uuid4())


def make_asset_id() -> VideoAssetId:
    return VideoAssetId(uuid4())


# ---------------------------------------------------------------------------
# Fakes (same conventions as the Phase 4/6 source tests)
# ---------------------------------------------------------------------------


async def _bytes_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


class FakeFileDecoder:
    """Deterministic in-memory decoder for FileFrameSource tests."""

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

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        self.opened = True

    async def read(self) -> DecodedFrame | None:
        position = self._read_calls
        self._read_calls += 1
        if position in self._decode_error_at:
            raise FrameDecodeError(f"corrupt frame at source position {position}")
        if position >= len(self._frames):
            return None
        return self._frames[position]

    async def close(self) -> None:
        self.closed = True


def make_file_frame(
    width: int = 1920, height: int = 1080, *, pts_seconds: float = 0.0, payload: int = 0
) -> DecodedFrame:
    return DecodedFrame(
        width=width, height=height, data=bytes([payload]) * 16, pts_seconds=pts_seconds
    )


async def make_file_source(
    *,
    frames: list[DecodedFrame],
    decode_error_at: set[int] | None = None,
    max_consecutive_decode_errors: int = 100,
) -> tuple[FileFrameSource, FakeFileDecoder]:
    storage = FakeStorageAdapter()
    await storage.put_object_stream(
        FILE_KEY, _bytes_stream(b"mp4-bytes"), content_type="video/mp4", size_bytes=9
    )
    decoder = FakeFileDecoder(frames, decode_error_at=decode_error_at)
    source = FileFrameSource(
        session_id=make_session_id(),
        source_ref=make_asset_id(),
        storage=storage,
        object_key=FILE_KEY,
        decoder=decoder,
        capture_time=CAPTURE_TIME,
        max_consecutive_decode_errors=max_consecutive_decode_errors,
    )
    return source, decoder


class FakeRtspTransport:
    """RtspTransport whose sessions end after their frames (bounded reconnect)."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> AsyncIterator[bytes]:
        self.connect_calls += 1
        return _bytes_stream(b"rtsp-bytes")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class FakeRtspDecoder:
    """Session decoder: each open() advances to the next session's frames."""

    def __init__(self, frames_per_session: list[list[DecodedFrame]]) -> None:
        self._frames_per_session = list(frames_per_session)
        self._session_index = 0
        self._frame_index = 0
        self.opened = 0
        self.closed = False

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        self.opened += 1
        if self.opened > 1:
            self._session_index += 1
            self._frame_index = 0

    async def read(self) -> DecodedFrame | None:
        if self._session_index >= len(self._frames_per_session):
            return None
        session = self._frames_per_session[self._session_index]
        if self._frame_index >= len(session):
            return None
        frame = session[self._frame_index]
        self._frame_index += 1
        return frame

    async def close(self) -> None:
        self.closed = True


def make_rtsp_frame(width: int = 1280, height: int = 720, *, payload: int = 0) -> DecodedFrame:
    return DecodedFrame(width=width, height=height, data=bytes([payload]) * 16)


def tiny_reconnect_policy(max_attempts: int = 2) -> ReconnectPolicy:
    return ReconnectPolicy(
        max_attempts=max_attempts,
        base_delay_seconds=0.001,
        max_delay_seconds=0.002,
        jitter=0.0,
    )


def make_rtsp_source(
    *, frames_per_session: list[list[DecodedFrame]]
) -> tuple[RTSPFrameSource, FakeRtspTransport, FakeRtspDecoder]:
    transport = FakeRtspTransport()
    decoder = FakeRtspDecoder(frames_per_session)
    source = RTSPFrameSource(
        session_id=make_session_id(),
        source_ref=make_asset_id(),
        transport=transport,
        decoder=decoder,
        source_url="rtsp://cam1.local:554/live",
        reconnect_policy=tiny_reconnect_policy(),
    )
    return source, transport, decoder


# ---------------------------------------------------------------------------
# Downstream consumers (CV boundary)
# ---------------------------------------------------------------------------


class RecordingConsumer:
    """Source-agnostic CV consumer: records every (packet, data) pair.

    It never reads a source type — there is none on FramePacket — so it
    cannot branch on live vs recorded.
    """

    def __init__(self) -> None:
        self.frames: list[tuple[FramePacket, bytes]] = []
        self.fail_after: int | None = None

    async def consume(self, frame: QueuedFrame) -> None:
        if self.fail_after is not None and len(self.frames) >= self.fail_after:
            msg = "cv consumer failure"
            raise RuntimeError(msg)
        self.frames.append((frame.packet, frame.data.data))


class SlowConsumer(RecordingConsumer):
    """Consumer that sleeps per frame to force backpressure in tests."""

    def __init__(self, *, delay: float = 0.002) -> None:
        super().__init__()
        self._delay = delay

    async def consume(self, frame: QueuedFrame) -> None:
        await asyncio.sleep(self._delay)
        await super().consume(frame)


def make_pipeline(
    consumer: FrameConsumer, *, maxsize: int = 16, policy: QueueFullPolicy
) -> FramePipeline:
    return FramePipeline(
        queue=BoundedFrameQueue(maxsize=maxsize, full_policy=policy),
        consumer=consumer,
    )


def assert_canonical_packets(packets: list[FramePacket]) -> None:
    """Common structural assertions — downstream input is schema-identical.

    NOTE: frame indices must start at 0 (a fresh sequence).  Callers with
    a suffix (e.g. DROP_OLDEST leftovers) must assert monotonicity
    explicitly instead.
    """
    assert packets
    assert all(isinstance(p, FramePacket) for p in packets)
    assert [p.frame_index for p in packets] == list(range(len(packets)))
    assert len({p.frame_id for p in packets}) == len(packets)
    assert all(p.session_id is not None for p in packets)
    assert all(p.event_time.tzinfo is not None for p in packets)
    assert all(p.width and p.height for p in packets)


# ---------------------------------------------------------------------------
# Both sources → the SAME downstream boundary
# ---------------------------------------------------------------------------


class TestSharedDownstreamBoundary:
    async def test_file_and_rtsp_reach_the_same_consumer_contract(self) -> None:
        """Recorded and live frames are consumed by one source-agnostic boundary."""
        consumer = RecordingConsumer()
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)

        file_source, file_decoder = await make_file_source(
            frames=[
                make_file_frame(pts_seconds=0.0, payload=1),
                make_file_frame(pts_seconds=0.5, payload=2),
                make_file_frame(pts_seconds=1.0, payload=3),
            ]
        )
        await pipeline.run(file_source)

        rtsp_source, transport, rtsp_decoder = make_rtsp_source(
            frames_per_session=[[make_rtsp_frame(payload=1), make_rtsp_frame(payload=2)]]
        )
        rtsp_pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)
        with pytest.raises(SourceTerminatedError):  # live session exhausts reconnect
            await rtsp_pipeline.run(rtsp_source)

        packets = [p for p, _ in consumer.frames]
        assert len(packets) == 5  # 3 recorded + 2 live, same consumer
        # Recorded frames came first (in order), then live frames — each
        # source's index is independently monotonic from 0.
        recorded, live = packets[:3], packets[3:]
        assert_canonical_packets(recorded)
        assert_canonical_packets(live)
        assert [p.frame_index for p in recorded] == [0, 1, 2]
        assert [p.frame_index for p in live] == [0, 1]
        # The consumer received decoded bytes for every packet.
        assert all(data for _, data in consumer.frames)
        # Both sources were closed (pipeline guarantees cleanup).
        assert file_source.state is FrameSourceState.CLOSED
        assert file_decoder.closed is True
        assert rtsp_source.state is FrameSourceState.CLOSED
        assert rtsp_decoder.closed is True
        assert transport.disconnect_calls >= 1

    async def test_rtsp_source_termination_is_observable_and_clean(self) -> None:
        consumer = RecordingConsumer()
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)
        source, _, _ = make_rtsp_source(frames_per_session=[[make_rtsp_frame(payload=1)]])
        with pytest.raises(SourceTerminatedError, match="reconnection exhausted"):
            await pipeline.run(source)
        # The one delivered frame was canonical before termination.
        assert_canonical_packets([p for p, _ in consumer.frames])
        assert source.state is FrameSourceState.CLOSED
        assert (await pipeline.queue.stats()).closed is True

    def test_frame_packet_carries_no_source_discriminator(self) -> None:
        """The CV layer literally cannot branch on live vs recorded."""
        assert "source_type" not in FramePacket.model_fields

    def test_pump_is_typed_against_abstract_frame_source(self) -> None:
        """The pipeline depends only on the abstract FrameSource contract."""
        import inspect

        signature = inspect.signature(FramePipeline.run)
        # ``from __future__ import annotations`` keeps annotations as strings.
        assert signature.parameters["source"].annotation == "FrameSource"
        source_code = inspect.getsource(FramePipeline)
        assert "FileFrameSource" not in source_code
        assert "RTSPFrameSource" not in source_code
        assert "RtspTransport" not in source_code
        assert "StoragePort" not in source_code

    def test_consumer_is_runtime_checkable_frame_consumer(self) -> None:
        """The CV boundary protocol is honored by the recording consumer."""
        assert isinstance(RecordingConsumer(), FrameConsumer)

    def test_both_sources_satisfy_the_abstract_contract(self) -> None:
        """Both concrete sources are valid FrameSource instances (runtime)."""
        import asyncio

        async def _check() -> None:
            file_source, _ = await make_file_source(frames=[make_file_frame()])
            rtsp_source, _, _ = make_rtsp_source(frames_per_session=[[make_rtsp_frame()]])
            assert isinstance(file_source, FrameSource)
            assert isinstance(rtsp_source, FrameSource)
            await file_source.aclose()
            await rtsp_source.aclose()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Queue-full policy integration
# ---------------------------------------------------------------------------


class TestQueuePolicyIntegration:
    async def test_block_policy_delivers_every_frame_under_backpressure(self) -> None:
        consumer = SlowConsumer()
        pipeline = make_pipeline(consumer, maxsize=2, policy=QueueFullPolicy.BLOCK)
        source, _ = await make_file_source(
            frames=[make_file_frame(pts_seconds=i * 0.5, payload=i) for i in range(5)]
        )
        await pipeline.run(source)
        assert len(consumer.frames) == 5  # zero loss despite tiny queue
        stats = await pipeline.queue.stats()
        assert stats.dropped_frames == 0
        assert stats.total_enqueued == 5
        assert stats.total_dequeued == 5
        assert source.dropped_frames == 0

    async def test_drop_oldest_counts_drops_on_queue_and_source(self) -> None:
        consumer = SlowConsumer()
        pipeline = make_pipeline(consumer, maxsize=2, policy=QueueFullPolicy.DROP_OLDEST)
        source, _ = await make_file_source(
            frames=[make_file_frame(pts_seconds=i * 0.5, payload=i) for i in range(20)]
        )
        await pipeline.run(source)
        stats = await pipeline.queue.stats()
        assert stats.dropped_frames > 0
        assert source.dropped_frames == stats.dropped_frames
        assert stats.total_enqueued == 20
        assert len(consumer.frames) == stats.total_dequeued
        # Every delivered frame is canonical; none lost mid-stream corrupts
        # state.  DROP_OLDEST delivers a suffix (indices may not start at 0),
        # so assert schema validity + monotonicity explicitly.
        delivered = [p.frame_index for p, _ in consumer.frames]
        packets = [p for p, _ in consumer.frames]
        assert packets
        assert all(isinstance(p, FramePacket) for p in packets)
        assert len({p.frame_id for p in packets}) == len(packets)
        assert all(p.event_time.tzinfo is not None for p in packets)
        # Oldest frames were evicted: delivered indices are strictly
        # increasing within [0, 20) — no duplicates, no reordering.
        assert delivered == sorted(delivered)
        assert len(set(delivered)) == len(delivered)
        assert all(0 <= i < 20 for i in delivered)


# ---------------------------------------------------------------------------
# Failure propagation & cleanup
# ---------------------------------------------------------------------------


class TestFailureAndCleanup:
    async def test_consumer_failure_propagates_and_queue_shuts_down(self) -> None:
        consumer = RecordingConsumer()
        consumer.fail_after = 2
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)
        source, decoder = await make_file_source(
            frames=[make_file_frame(pts_seconds=i * 0.5) for i in range(4)]
        )
        with pytest.raises(RuntimeError, match="cv consumer failure"):
            await pipeline.run(source)
        assert len(consumer.frames) == 2
        assert source.state is FrameSourceState.CLOSED
        assert decoder.closed is True
        assert (await pipeline.queue.stats()).closed is True

    async def test_source_failure_propagates_and_queue_shuts_down(self) -> None:
        consumer = RecordingConsumer()
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)
        source, _ = await make_file_source(
            frames=[make_file_frame(pts_seconds=0.0)] * 4,
            decode_error_at={0, 1, 2},
            max_consecutive_decode_errors=3,
        )
        with pytest.raises(SourceTerminatedError, match="consecutive"):
            await pipeline.run(source)
        assert source.state is FrameSourceState.CLOSED
        assert (await pipeline.queue.stats()).closed is True

    async def test_source_missing_companion_data_is_rejected(self) -> None:
        class BareSource(FrameSource):
            """A contract-violating source: emits a packet without FrameData."""

            def __init__(self) -> None:
                super().__init__(
                    session_id=make_session_id(),
                    source_type=SourceType.RECORDED,
                    source_ref=make_asset_id(),
                )

            async def _start(self) -> None:
                return None

            async def _produce_next(self) -> FramePacket:
                return self._make_packet(width=640, height=480, event_time=CAPTURE_TIME)

            async def _stop(self) -> None:
                return None

        consumer = RecordingConsumer()
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)
        with pytest.raises(FrameSourceError, match="without companion FrameData"):
            await pipeline.run(BareSource())
        assert consumer.frames == []
        assert (await pipeline.queue.stats()).closed is True

    async def test_run_rejects_already_shut_down_queue(self) -> None:
        consumer = RecordingConsumer()
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)
        await pipeline.queue.shutdown()
        source, _ = await make_file_source(frames=[make_file_frame()])
        with pytest.raises(QueueClosedError):
            await pipeline.run(source)
        assert source.state is FrameSourceState.CREATED  # never opened

    async def test_cancellation_releases_source_and_queue(self) -> None:
        class BlockingDecoder(FakeFileDecoder):
            def __init__(self) -> None:
                super().__init__([make_file_frame()])
                self._release = asyncio.Event()

            async def read(self) -> DecodedFrame | None:
                await self._release.wait()
                return None

        storage = FakeStorageAdapter()
        await storage.put_object_stream(
            FILE_KEY, _bytes_stream(b"mp4-bytes"), content_type="video/mp4", size_bytes=9
        )
        decoder = BlockingDecoder()
        source = FileFrameSource(
            session_id=make_session_id(),
            source_ref=make_asset_id(),
            storage=storage,
            object_key=FILE_KEY,
            decoder=decoder,
            capture_time=CAPTURE_TIME,
        )
        consumer = RecordingConsumer()
        pipeline = make_pipeline(consumer, policy=QueueFullPolicy.BLOCK)

        task = asyncio.create_task(pipeline.run(source))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert source.state is FrameSourceState.CLOSED
        assert decoder.closed is True
        assert (await pipeline.queue.stats()).closed is True
