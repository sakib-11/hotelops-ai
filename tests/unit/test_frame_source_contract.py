"""Unit tests for the Task 11 Phase 3 canonical ingestion contract.

Covers the ``FrameSource`` lifecycle state machine (open/iterate/EOF/
close/fail), cancellation-safe resource release, monotonic frame
indexing, decode-error accounting, and ``FrameData`` validation.

The canonical ``FramePacket`` / ``VideoSession`` contracts are tested
in ``tests/contract`` — this file exercises the ingestion boundary that
produces them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.app.intelligence.sources.base import (
    DecodeStatus,
    FrameData,
    FrameSource,
    FrameSourceState,
)
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    InvalidStateTransitionError,
    SourceNotOpenError,
    SourceTerminatedError,
)
from contracts.common import FrameId, VideoAssetId, VideoSessionId
from contracts.video import FramePacket, SourceType


def make_session_id() -> VideoSessionId:
    return VideoSessionId(uuid4())


def make_asset_id() -> VideoAssetId:
    return VideoAssetId(uuid4())


class RecordingFrameSource(FrameSource):
    """Test double: a recorded source emitting one packet per event time.

    ``decode_error_at`` configures which frame indices raise
    ``FrameDecodeError`` so the base's skip/count/terminate logic can be
    exercised deterministically.
    """

    def __init__(
        self,
        *,
        session_id: VideoSessionId,
        source_type: SourceType = SourceType.RECORDED,
        source_ref: VideoAssetId | None = None,
        event_times: list[datetime] | None = None,
        decode_error_at: set[int] | None = None,
        max_consecutive_decode_errors: int = 100,
    ) -> None:
        super().__init__(
            session_id=session_id,
            source_type=source_type,
            source_ref=source_ref,
            max_consecutive_decode_errors=max_consecutive_decode_errors,
        )
        self.event_times = event_times or []
        self.decode_error_at = decode_error_at or set()
        self._produced = 0
        self.started = False
        self.stopped = False

    async def _start(self) -> None:
        self.started = True

    async def _produce_next(self) -> FramePacket:
        if self._produced >= len(self.event_times):
            raise StopAsyncIteration
        index = self._produced
        self._produced += 1
        if index in self.decode_error_at:
            raise FrameDecodeError(f"simulated decode failure at frame {index}")
        return self._make_packet(
            width=1920,
            height=1080,
            event_time=self.event_times[index],
        )

    async def _stop(self) -> None:
        self.stopped = True


def sample_times(count: int, *, start: datetime | None = None) -> list[datetime]:
    start = start or datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
    return [start.replace(microsecond=i * 33_333) for i in range(count)]


# ---------------------------------------------------------------------------
# FrameData validation
# ---------------------------------------------------------------------------


class TestFrameData:
    def test_valid_frame_data(self) -> None:
        frame = FrameData(
            frame_index=0,
            width=1920,
            height=1080,
            data=b"\x00" * 64,
            pts_seconds=1.5,
            source_timestamp=datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC),
        )
        assert frame.decode_status is DecodeStatus.OK
        assert frame.pts_seconds == pytest.approx(1.5)

    def test_negative_frame_index_rejected(self) -> None:
        with pytest.raises(ValueError, match="frame_index"):
            FrameData(frame_index=-1, width=1, height=1, data=b"")

    def test_non_positive_dimensions_rejected(self) -> None:
        with pytest.raises(ValueError, match="width"):
            FrameData(frame_index=0, width=0, height=1080, data=b"")

    def test_negative_pts_rejected(self) -> None:
        with pytest.raises(ValueError, match="pts_seconds"):
            FrameData(frame_index=0, width=1, height=1, data=b"", pts_seconds=-0.1)

    def test_naive_source_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_timestamp"):
            FrameData(
                frame_index=0,
                width=1,
                height=1,
                data=b"",
                source_timestamp=datetime(2026, 7, 29, 12, 0, 0),  # naive
            )

    def test_decode_status_explicit(self) -> None:
        frame = FrameData(
            frame_index=3,
            width=None,
            height=None,
            data=b"",
            decode_status=DecodeStatus.DECODE_ERROR,
        )
        assert frame.decode_status is DecodeStatus.DECODE_ERROR


# ---------------------------------------------------------------------------
# FrameSource lifecycle
# ---------------------------------------------------------------------------


class TestFrameSourceLifecycle:
    async def test_initial_state_is_created(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        assert source.state is FrameSourceState.CREATED
        assert source.dropped_frames == 0
        assert source.decode_errors == 0

    async def test_open_transitions_to_running_and_starts(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        await source.open()
        assert source.state is FrameSourceState.RUNNING
        assert source.started is True

    async def test_open_twice_rejected(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        await source.open()
        with pytest.raises(InvalidStateTransitionError):
            await source.open()

    async def test_open_after_close_rejected(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        await source.open()
        await source.aclose()
        with pytest.raises(InvalidStateTransitionError):
            await source.open()

    async def test_open_failure_marks_failed(self) -> None:
        class FailingStartSource(RecordingFrameSource):
            async def _start(self) -> None:
                msg = "cannot acquire transport"
                raise RuntimeError(msg)

        source = FailingStartSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        with pytest.raises(RuntimeError, match="acquire transport"):
            await source.open()
        assert source.state is FrameSourceState.FAILED

    async def test_open_failure_releases_partially_acquired_resources(self) -> None:
        """A failed _start() must still release resources it acquired.

        Python does not call __aexit__ when __aenter__ raises, so open()
        itself is the only guaranteed cleanup point for a partial start.
        """

        class PartialStartSource(RecordingFrameSource):
            async def _start(self) -> None:
                self.started = True  # simulated: socket/decoder partially acquired
                msg = "decoder init failed"
                raise RuntimeError(msg)

        source = PartialStartSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        with pytest.raises(RuntimeError, match="decoder init failed"):
            await source.open()
        assert source.state is FrameSourceState.FAILED
        assert source.started is True
        assert source.stopped is True  # cleanup ran despite the failed start

    async def test_anext_before_open_rejected(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        with pytest.raises(SourceNotOpenError):
            await anext(source)

    async def test_anext_after_close_rejected(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        await source.open()
        await source.aclose()
        with pytest.raises(SourceNotOpenError):
            await anext(source)

    async def test_aclose_is_idempotent_and_releases_resources(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(2),
        )
        await source.open()
        await source.aclose()
        await source.aclose()  # second close is a no-op
        assert source.state is FrameSourceState.CLOSED
        assert source.stopped is True

    async def test_aclose_before_open_is_safe(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        await source.aclose()
        assert source.state is FrameSourceState.CLOSED
        assert source.started is False

    async def test_context_manager_opens_and_closes(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(2),
        )
        async with source:
            assert source.state is FrameSourceState.RUNNING
        assert source.state is FrameSourceState.CLOSED
        assert source.started is True
        assert source.stopped is True

    async def test_context_manager_closes_even_on_body_exception(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(2),
        )
        with pytest.raises(RuntimeError, match="boom"):
            async with source:
                msg = "boom"
                raise RuntimeError(msg)
        assert source.state is FrameSourceState.CLOSED
        assert source.stopped is True

    async def test_cancellation_inside_context_still_closes(self) -> None:
        import asyncio

        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(5),
        )
        with pytest.raises(asyncio.CancelledError):
            async with source:
                raise asyncio.CancelledError
        assert source.state is FrameSourceState.CLOSED
        assert source.stopped is True

    async def test_iteration_after_failure_raises_terminated(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(2),
        )
        source._fail()  # simulate a subclass-detected fatal transport error
        with pytest.raises(SourceTerminatedError):
            await anext(source)


# ---------------------------------------------------------------------------
# Frame emission: identity, index, timestamps
# ---------------------------------------------------------------------------


class TestFrameEmission:
    async def test_packets_carry_session_and_monotonic_indices(self) -> None:
        session_id = make_session_id()
        source = RecordingFrameSource(
            session_id=session_id,
            source_type=SourceType.LIVE,
            event_times=sample_times(3),
        )
        async with source:
            packets = [await anext(source) for _ in range(3)]
        assert [p.frame_index for p in packets] == [0, 1, 2]
        assert all(p.session_id == session_id for p in packets)
        assert len({p.frame_id for p in packets}) == 3

    async def test_event_time_preserved_utc(self) -> None:
        times = sample_times(2)
        source = RecordingFrameSource(session_id=make_session_id(), event_times=times)
        async with source:
            first = await anext(source)
        assert first.event_time == times[0]
        assert first.event_time.tzinfo is not None

    async def test_source_ref_stamped_on_packets(self) -> None:
        asset_id = make_asset_id()
        source = RecordingFrameSource(
            session_id=make_session_id(),
            source_ref=asset_id,
            event_times=sample_times(1),
        )
        async with source:
            packet = await anext(source)
        assert packet.source_ref == asset_id

    async def test_dimensions_carried(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        async with source:
            packet = await anext(source)
        assert packet.width == 1920
        assert packet.height == 1080

    async def test_naive_event_time_rejected_by_contract(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=[datetime(2026, 7, 29, 12, 0, 0)],  # naive
        )
        await source.open()
        with pytest.raises(ValidationError):
            await anext(source)

    async def test_eof_transitions_to_draining_and_raises_stop(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(2),
        )
        async with source:
            await anext(source)
            await anext(source)
            with pytest.raises(StopAsyncIteration):
                await anext(source)
            assert source.state is FrameSourceState.DRAINING

    async def test_eof_before_close_is_repeatable(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        async with source:
            await anext(source)
            with pytest.raises(StopAsyncIteration):
                await anext(source)
            with pytest.raises(StopAsyncIteration):
                await anext(source)


# ---------------------------------------------------------------------------
# Decode-error accounting
# ---------------------------------------------------------------------------


class TestDecodeAccounting:
    async def test_decode_error_is_counted_and_skipped(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(4),
            decode_error_at={1},
        )
        async with source:
            indices = []
            async for packet in source:
                indices.append(packet.frame_index)
        # The source emits 3 packets (source frames 0, 2, 3). Decode
        # failures do not consume an emitted frame index, so indices stay
        # contiguous and monotonic.
        assert indices == [0, 1, 2]
        assert source.decode_errors == 1

    async def test_consecutive_decode_errors_terminate_source(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(10),
            decode_error_at={0, 1, 2},
            max_consecutive_decode_errors=3,
        )
        await source.open()
        with pytest.raises(SourceTerminatedError, match="consecutive"):
            async for _ in source:
                pass
        assert source.state is FrameSourceState.FAILED
        assert source.decode_errors == 3
        await source.aclose()

    async def test_decode_error_then_success_resets_consecutive_count(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(6),
            decode_error_at={0, 1},
            max_consecutive_decode_errors=3,
        )
        await source.open()
        indices = []
        async for packet in source:
            indices.append(packet.frame_index)
        # Two consecutive errors (source frames 0, 1), then successes reset
        # the consecutive counter and the remaining frames are emitted — the
        # source does NOT terminate.
        assert indices == [0, 1, 2, 3]
        assert source.decode_errors == 2
        assert source.state is FrameSourceState.DRAINING
        await source.aclose()

    async def test_dropped_frames_counter(self) -> None:
        source = RecordingFrameSource(
            session_id=make_session_id(),
            event_times=sample_times(1),
        )
        source.note_dropped()
        source.note_dropped()
        assert source.dropped_frames == 2


# ---------------------------------------------------------------------------
# FramePacket identity sanity (no duplication of Task 4 contract tests)
# ---------------------------------------------------------------------------


class TestFramePacketIdentity:
    def test_frame_id_is_uuid(self) -> None:
        packet = FramePacket(
            frame_id=FrameId(uuid4()),
            session_id=make_session_id(),
            frame_index=0,
            event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        )
        assert isinstance(packet.frame_id, UUID)

    def test_contract_is_frozen(self) -> None:
        packet = FramePacket(
            frame_id=FrameId(uuid4()),
            session_id=make_session_id(),
            frame_index=0,
            event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            packet.frame_index = 5  # type: ignore[misc]  # frozen model


@pytest.mark.asyncio
async def test_source_used_as_async_generator() -> None:
    """The FrameSource contract integrates with ``async for``."""
    source = RecordingFrameSource(
        session_id=make_session_id(),
        event_times=sample_times(3),
    )
    seen: list[int] = []
    async with source:
        async for packet in source:
            seen.append(packet.frame_index)
    assert seen == [0, 1, 2]
    assert source.state is FrameSourceState.CLOSED
