"""Unit tests for FileFrameSource (Task 11, Phase 4).

Uses the Task 9 ``FakeStorageAdapter`` (in-memory StoragePort) and a
fake ``FrameDecoder`` — no video library dependency is required because
the decoder SDK is isolated behind the ``FrameDecoder`` protocol.

Covers: valid video, unreadable video, empty video, corrupt frames,
EOF, cancellation, and cleanup after exceptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.infrastructure.storage.types import ObjectMetadata
from backend.app.intelligence.sources.base import FrameSourceState
from backend.app.intelligence.sources.decoder import DecodedFrame, FrameDecoder
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    SourceTerminatedError,
)
from backend.app.intelligence.sources.file import FileFrameSource
from contracts.common import VideoAssetId, VideoSessionId

CAPTURE_TIME = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
OBJECT_KEY = "tenants/t1/venues/v1/recordings/2026/07/29/cam1.mp4"


def make_session_id() -> VideoSessionId:
    return VideoSessionId(uuid4())


def make_asset_id() -> VideoAssetId:
    return VideoAssetId(uuid4())


# ---------------------------------------------------------------------------
# Fake decoder (implements FrameDecoder protocol without a video library)
# ---------------------------------------------------------------------------


class FakeFrameDecoder:
    """Deterministic in-memory decoder.

    ``frames`` is the decoded frame sequence; ``decode_error_at`` marks
    source-frame positions that fail to decode (FrameDecodeError).
    """

    def __init__(
        self,
        frames: list[DecodedFrame],
        *,
        decode_error_at: set[int] | None = None,
        fail_open: bool = False,
    ) -> None:
        self._frames = list(frames)
        self._decode_error_at = decode_error_at or set()
        self._fail_open = fail_open
        self._read_calls = 0
        self.opened = False
        self.closed = False
        self.received_stream: AsyncIterator[bytes] | None = None

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        if self._fail_open:
            msg = "cannot read container header (not a video)"
            raise FrameDecodeError(msg)
        self.opened = True
        self.received_stream = stream

    async def read(self) -> DecodedFrame | None:
        if not self.opened:
            msg = "decoder not opened"
            raise RuntimeError(msg)
        position = self._read_calls
        self._read_calls += 1
        if position in self._decode_error_at:
            msg = f"corrupt frame at source position {position}"
            raise FrameDecodeError(msg)
        if position >= len(self._frames):
            return None
        return self._frames[position]

    async def close(self) -> None:
        self.closed = True


async def _bytes_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


def make_frame(
    width: int = 1920,
    height: int = 1080,
    *,
    pts_seconds: float | None = None,
    payload: int = 0,
) -> DecodedFrame:
    return DecodedFrame(
        width=width,
        height=height,
        data=bytes([payload]) * 16,
        pts_seconds=pts_seconds,
    )


def make_source(
    *,
    storage: FakeStorageAdapter,
    decoder: FakeFrameDecoder,
    capture_time: datetime | None = CAPTURE_TIME,
    object_key: str = OBJECT_KEY,
    max_consecutive_decode_errors: int = 100,
) -> FileFrameSource:
    return FileFrameSource(
        session_id=make_session_id(),
        source_ref=make_asset_id(),
        storage=storage,
        object_key=object_key,
        decoder=decoder,
        capture_time=capture_time,
        max_consecutive_decode_errors=max_consecutive_decode_errors,
    )


async def seed_storage(storage: FakeStorageAdapter, *, data: bytes) -> ObjectMetadata:
    return await storage.put_object_stream(
        OBJECT_KEY,
        _bytes_stream(data),
        content_type="video/mp4",
        size_bytes=len(data),
    )


def frame_payloads(frames: list[DecodedFrame]) -> list[bytes]:
    return [f.data for f in frames]


# ---------------------------------------------------------------------------
# Valid video
# ---------------------------------------------------------------------------


class TestValidVideo:
    async def test_valid_video_yields_canonical_packets(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([
            make_frame(pts_seconds=0.0, payload=1),
            make_frame(pts_seconds=0.5, payload=2),
            make_frame(pts_seconds=1.0, payload=3),
        ])
        source = make_source(storage=storage, decoder=decoder)

        await source.open()
        packets = [await anext(source) for _ in range(3)]
        with pytest.raises(StopAsyncIteration):  # EOF reached
            await anext(source)
        assert source.state is FrameSourceState.DRAINING
        await source.aclose()

        assert [p.frame_index for p in packets] == [0, 1, 2]
        assert all(p.session_id == source.session_id for p in packets)
        assert all(p.source_ref == source.source_ref for p in packets)
        assert all(p.width == 1920 and p.height == 1080 for p in packets)
        assert len({p.frame_id for p in packets}) == 3

    async def test_event_time_is_capture_time_plus_pts(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([
            make_frame(pts_seconds=0.5, payload=1),
            make_frame(pts_seconds=2.0, payload=2),
        ])
        source = make_source(storage=storage, decoder=decoder)

        async with source:
            first = await anext(source)
            second = await anext(source)

        assert first.event_time == CAPTURE_TIME + timedelta(seconds=0.5)
        assert second.event_time == CAPTURE_TIME + timedelta(seconds=2.0)

    async def test_last_frame_data_carries_payload(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        frames = [
            make_frame(pts_seconds=0.0, payload=1),
            make_frame(pts_seconds=0.5, payload=2),
        ]
        decoder = FakeFrameDecoder(frames)
        source = make_source(storage=storage, decoder=decoder)

        async with source:
            await anext(source)
            await anext(source)

        assert source.last_frame_data is not None
        assert source.last_frame_data.frame_index == 1
        assert source.last_frame_data.data == frame_payloads(frames)[1]
        assert source.last_frame_data.pts_seconds == pytest.approx(0.5)

    async def test_decoder_receives_storage_stream(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)])
        source = make_source(storage=storage, decoder=decoder)

        async with source:
            await anext(source)

        assert decoder.opened is True
        assert decoder.received_stream is not None

    async def test_eof_raises_stop_async_iteration(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)])
        source = make_source(storage=storage, decoder=decoder)

        async with source:
            await anext(source)
            with pytest.raises(StopAsyncIteration):
                await anext(source)
            assert source.state is FrameSourceState.DRAINING


# ---------------------------------------------------------------------------
# Unreadable / missing video
# ---------------------------------------------------------------------------


class TestUnreadableVideo:
    async def test_missing_object_fails_open(self) -> None:
        storage = FakeStorageAdapter()  # nothing seeded
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)])
        source = make_source(storage=storage, decoder=decoder)

        with pytest.raises(FrameSourceError, match="not found"):
            await source.open()

        assert source.state is FrameSourceState.FAILED

    async def test_unreadable_container_fails_open(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"not-a-video")
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)], fail_open=True)
        source = make_source(storage=storage, decoder=decoder)

        with pytest.raises(FrameDecodeError, match="container"):
            await source.open()

        assert source.state is FrameSourceState.FAILED
        # A partially-initialized decoder is still released on failed open.
        assert decoder.closed is True

    async def test_storage_outage_fails_open(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        storage.simulate_unavailable(True)
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)])
        source = make_source(storage=storage, decoder=decoder)

        with pytest.raises(FrameSourceError):
            await source.open()

        assert source.state is FrameSourceState.FAILED

    async def test_unresolvable_event_time_marks_failed(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([make_frame(pts_seconds=None)])
        source = make_source(storage=storage, decoder=decoder, capture_time=None)

        await source.open()
        with pytest.raises(FrameSourceError, match="event_time"):
            await anext(source)

        assert source.state is FrameSourceState.FAILED
        await source.aclose()

    async def test_typed_decoder_error_is_not_double_wrapped(self) -> None:
        class TypedErrorDecoder(FakeFrameDecoder):
            async def read(self) -> DecodedFrame | None:
                raise FrameSourceError("typed decoder boundary error")

        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        source = make_source(storage=storage, decoder=TypedErrorDecoder([]))

        await source.open()
        with pytest.raises(FrameSourceError, match=r"^typed decoder boundary error$"):
            await anext(source)
        await source.aclose()

    async def test_empty_object_yields_no_frames(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"")
        decoder = FakeFrameDecoder([])
        source = make_source(storage=storage, decoder=decoder)

        async with source:
            with pytest.raises(StopAsyncIteration):
                await anext(source)

        assert source.decode_errors == 0


# ---------------------------------------------------------------------------
# Corrupt frames
# ---------------------------------------------------------------------------


class TestCorruptFrames:
    async def test_corrupt_frame_is_counted_and_skipped(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder(
            [
                make_frame(pts_seconds=0.0, payload=1),
                make_frame(pts_seconds=0.5, payload=2),
                make_frame(pts_seconds=1.0, payload=3),
                make_frame(pts_seconds=1.5, payload=4),
            ],
            decode_error_at={1},  # source frame 1 is corrupt
        )
        source = make_source(storage=storage, decoder=decoder)

        await source.open()
        indices = []
        async for packet in source:
            indices.append(packet.frame_index)

        assert indices == [0, 1, 2]  # corrupt frame skipped; emitted indices contiguous
        assert source.decode_errors == 1
        assert source.state is FrameSourceState.DRAINING
        await source.aclose()

    async def test_sustained_corruption_terminates_source(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder(
            [make_frame(pts_seconds=0.0)] * 10,
            decode_error_at={0, 1, 2, 3},
        )
        source = make_source(storage=storage, decoder=decoder, max_consecutive_decode_errors=3)
        await source.open()
        with pytest.raises(SourceTerminatedError, match="consecutive"):
            async for _ in source:
                pass

        assert source.state is FrameSourceState.FAILED
        assert source.decode_errors == 3
        await source.aclose()

    async def test_decode_error_then_recovery(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder(
            [
                make_frame(pts_seconds=0.0, payload=1),
                make_frame(pts_seconds=0.5, payload=2),
                make_frame(pts_seconds=1.0, payload=3),
            ],
            decode_error_at={0, 1},
        )
        source = make_source(storage=storage, decoder=decoder, max_consecutive_decode_errors=5)
        await source.open()
        indices = []
        async for packet in source:
            indices.append(packet.frame_index)

        assert indices == [0]  # only frame 2 emitted after two skipped
        assert source.decode_errors == 2
        assert source.state is FrameSourceState.DRAINING
        await source.aclose()


# ---------------------------------------------------------------------------
# Cancellation & cleanup
# ---------------------------------------------------------------------------


class _BlockingFrameDecoder:
    """Decoder whose read() blocks until released — for cancellation tests."""

    def __init__(self) -> None:
        self._release = asyncio.Event()
        self.closed = False

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        self.received_stream = stream

    async def read(self) -> DecodedFrame | None:
        await self._release.wait()
        return None

    async def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self._release.set()


class TestCancellationAndCleanup:
    async def test_cancellation_still_closes_source(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = _BlockingFrameDecoder()
        source = make_source(storage=storage, decoder=decoder)

        task: asyncio.Task[object] | None = None

        async def consume() -> object:
            async for _ in source:
                pass
            return None

        await source.open()
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await source.aclose()

        assert decoder.closed is True
        assert source.state is FrameSourceState.CLOSED

    async def test_cleanup_after_body_exception(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)])
        source = make_source(storage=storage, decoder=decoder)

        with pytest.raises(RuntimeError, match="pipeline failed"):
            async with source:
                msg = "pipeline failed"
                raise RuntimeError(msg)

        assert decoder.closed is True
        assert source.state is FrameSourceState.CLOSED

    async def test_cleanup_releases_storage_stream(self) -> None:
        storage = FakeStorageAdapter()
        await seed_storage(storage, data=b"mp4-bytes")
        decoder = FakeFrameDecoder([make_frame(pts_seconds=0.0)])
        source = make_source(storage=storage, decoder=decoder)

        async with source:
            await anext(source)

        assert decoder.closed is True
        assert source.state is FrameSourceState.CLOSED
        # aclose() ran _stop(): decoder closed and stream detached
        assert source._stream is None  # test hook


# ---------------------------------------------------------------------------
# FrameDecoder contract validation
# ---------------------------------------------------------------------------


class TestDecodedFrameValidation:
    def test_invalid_dimensions_rejected(self) -> None:
        with pytest.raises(ValueError, match="width"):
            DecodedFrame(width=0, height=1080, data=b"")

    def test_negative_pts_rejected(self) -> None:
        with pytest.raises(ValueError, match="pts_seconds"):
            DecodedFrame(width=1, height=1, data=b"", pts_seconds=-1.0)

    def test_naive_source_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_timestamp"):
            DecodedFrame(
                width=1,
                height=1,
                data=b"",
                source_timestamp=datetime(2026, 7, 29, 12, 0, 0),  # naive
            )

    def test_decoder_protocol_runtime_checkable(self) -> None:
        assert isinstance(FakeFrameDecoder([]), FrameDecoder)
