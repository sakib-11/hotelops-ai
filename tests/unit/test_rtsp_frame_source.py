"""Unit tests for RTSPFrameSource (Task 11, Phase 6).

Exercises the connection lifecycle, connection failure, the bounded
reconnect policy, cancellation, shutdown, frame decoding, timestamps,
source/session identity, credential redaction, and resource cleanup —
all through the canonical ``FrameSource`` contract, with a scriptable
fake transport and a session-advancing fake decoder.  No RTSP SDK is
required.

Note: for a LIVE source, a decoder EOF is a connection loss (there is no
clean drain like recorded files), so EOF always drives the bounded
reconnect policy — never ``DRAINING``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.intelligence.sources.base import FrameSourceState
from backend.app.intelligence.sources.decoder import DecodedFrame
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    RtspConnectionError,
    SourceNotOpenError,
    SourceTerminatedError,
)
from backend.app.intelligence.sources.rtsp import (
    ReconnectPolicy,
    RTSPFrameSource,
    redact_rtsp_url,
)
from contracts.common import VideoAssetId, VideoSessionId
from contracts.video import SourceType

URL_WITH_CREDS = "rtsp://admin:secret@cam1.local:554/live"
URL_CLEAN = "rtsp://cam1.local:554/live"


def make_session_id() -> VideoSessionId:
    return VideoSessionId(uuid4())


def make_asset_id() -> VideoAssetId:
    return VideoAssetId(uuid4())


def make_frame(
    width: int = 1280,
    height: int = 720,
    *,
    source_timestamp: datetime | None = None,
    payload: int = 0,
) -> DecodedFrame:
    return DecodedFrame(
        width=width,
        height=height,
        data=bytes([payload]) * 16,
        source_timestamp=source_timestamp,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRtspTransport:
    """Scriptable RtspTransport: fails specific connect call numbers.

    ``fail_on_calls`` is a set of 1-based connect() call numbers that
    raise ``RtspConnectionError`` (an initial connect or a specific
    reconnect attempt can be failed independently).  Each successful
    connect returns the next session stream.
    """

    def __init__(
        self,
        *,
        fail_on_calls: set[int] | None = None,
        session_streams: list[AsyncIterator[bytes]] | None = None,
    ) -> None:
        self._fail_on_calls = set(fail_on_calls or set())
        self._session_streams = list(session_streams or [])
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.fail_open = False

    async def connect(self) -> AsyncIterator[bytes]:
        self.connect_calls += 1
        if self.fail_open:
            msg = "RTSP session negotiation failed"
            raise RtspConnectionError(msg)
        if self.connect_calls in self._fail_on_calls:
            msg = f"connect attempt {self.connect_calls} failed"
            raise RtspConnectionError(msg)
        idx = self.connect_calls - 1
        if idx < len(self._session_streams):
            return self._session_streams[idx]
        return _empty_stream()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


async def _empty_stream() -> AsyncIterator[bytes]:
    yield b""
    return


class FakeRtspDecoder:
    """Session-advancing decoder.

    Each successful ``open()`` (initial connect or reconnect) advances to
    the next session's frame list.  When a session's frames are
    exhausted, ``read()`` returns None — the source treats that as a
    connection loss and applies its reconnect policy.
    """

    def __init__(self, frames_per_session: list[list[DecodedFrame]]) -> None:
        self._frames_per_session = list(frames_per_session)
        self._session_index = 0
        self._frame_index = 0
        self.opened = 0
        self.closed = False
        self.received_streams: list[AsyncIterator[bytes] | None] = []
        self.fail_open = False
        self.decode_error_at: set[tuple[int, int]] = set()

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        if self.fail_open:
            msg = "cannot decode RTSP stream"
            raise FrameDecodeError(msg)
        self.opened += 1
        self.received_streams.append(stream)
        if self.opened > 1:
            # Reconnect = a new session; advance and reset the frame cursor.
            self._session_index += 1
            self._frame_index = 0

    async def read(self) -> DecodedFrame | None:
        if self.opened == 0:
            msg = "decoder not opened"
            raise RuntimeError(msg)
        if self._session_index >= len(self._frames_per_session):
            return None
        session = self._frames_per_session[self._session_index]
        if self._frame_index >= len(session):
            return None  # session ended → reconnect
        if (self._session_index, self._frame_index) in self.decode_error_at:
            self._frame_index += 1
            msg = f"corrupt frame session={self._session_index}"
            raise FrameDecodeError(msg)
        frame = session[self._frame_index]
        self._frame_index += 1
        return frame

    async def close(self) -> None:
        self.closed = True


def make_source(
    *,
    transport: FakeRtspTransport,
    decoder: FakeRtspDecoder,
    policy: ReconnectPolicy,
    source_url: str = URL_WITH_CREDS,
) -> RTSPFrameSource:
    return RTSPFrameSource(
        session_id=make_session_id(),
        source_ref=make_asset_id(),
        transport=transport,
        decoder=decoder,
        source_url=source_url,
        reconnect_policy=policy,
    )


def tiny_policy(max_attempts: int = 3) -> ReconnectPolicy:
    return ReconnectPolicy(
        max_attempts=max_attempts,
        base_delay_seconds=0.001,
        max_delay_seconds=0.002,
        jitter=0.0,
    )


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_credentials_stripped(self) -> None:
        assert redact_rtsp_url(URL_WITH_CREDS) == URL_CLEAN

    def test_clean_url_unchanged(self) -> None:
        assert redact_rtsp_url(URL_CLEAN) == URL_CLEAN

    def test_redacted_url_property(self) -> None:
        transport = FakeRtspTransport()
        source = make_source(
            transport=transport,
            decoder=FakeRtspDecoder([[]]),
            policy=tiny_policy(),
        )
        assert source.redacted_url == URL_CLEAN
        assert "secret" not in source.redacted_url

    def test_reconnect_policy_validation(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            ReconnectPolicy(max_attempts=0, base_delay_seconds=1.0, max_delay_seconds=2.0)
        with pytest.raises(ValueError, match="base_delay_seconds"):
            ReconnectPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=2.0)
        with pytest.raises(ValueError, match="max_delay_seconds"):
            ReconnectPolicy(max_attempts=1, base_delay_seconds=2.0, max_delay_seconds=1.0)
        with pytest.raises(ValueError, match="jitter"):
            ReconnectPolicy(
                max_attempts=1, base_delay_seconds=1.0, max_delay_seconds=2.0, jitter=1.0
            )

    def test_transport_protocol_runtime_checkable(self) -> None:
        from backend.app.intelligence.sources.rtsp import RtspTransport

        assert isinstance(FakeRtspTransport(), RtspTransport)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    async def test_connects_and_yields_canonical_packets(self) -> None:
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[make_frame(payload=1), make_frame(payload=2)]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        async with source:
            first = await anext(source)
            second = await anext(source)

        assert transport.connect_calls == 1
        assert source.state is FrameSourceState.CLOSED
        assert [first.frame_index, second.frame_index] == [0, 1]
        assert all(p.session_id == source.session_id for p in (first, second))
        assert all(p.source_ref == source.source_ref for p in (first, second))
        assert first.width == 1280 and first.height == 720
        assert len({first.frame_id, second.frame_id}) == 2

    async def test_source_type_is_live(self) -> None:
        transport = FakeRtspTransport()
        source = make_source(
            transport=transport,
            decoder=FakeRtspDecoder([[]]),
            policy=tiny_policy(),
        )
        assert source.source_type is SourceType.LIVE

    async def test_connect_failure_fails_open(self) -> None:
        transport = FakeRtspTransport(fail_on_calls={1})
        decoder = FakeRtspDecoder([[make_frame()]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        with pytest.raises(RtspConnectionError, match="connect attempt"):
            await source.open()

        assert source.state is FrameSourceState.FAILED

    async def test_undecodable_stream_fails_open(self) -> None:
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[make_frame()]])
        decoder.fail_open = True
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        with pytest.raises(FrameDecodeError):
            await source.open()

        assert source.state is FrameSourceState.FAILED

    async def test_anext_before_open_rejected(self) -> None:
        transport = FakeRtspTransport()
        source = make_source(
            transport=transport,
            decoder=FakeRtspDecoder([[]]),
            policy=tiny_policy(),
        )
        with pytest.raises(SourceNotOpenError):
            await anext(source)


# ---------------------------------------------------------------------------
# Reconnect policy
# ---------------------------------------------------------------------------


class TestReconnect:
    async def test_mid_stream_disconnect_reconnects(self) -> None:
        """Session 1 ends after 1 frame; reconnect delivers session 2."""
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[make_frame(payload=1)], [make_frame(payload=2)]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        await source.open()
        first = await anext(source)  # session 1 frame
        second = await anext(source)  # reconnect → session 2 frame

        assert first.frame_index == 0
        assert second.frame_index == 1
        assert transport.connect_calls == 2
        assert source.reconnects == 1
        assert source.state is FrameSourceState.RUNNING
        await source.aclose()

    async def test_reconnect_exhausted_terminates(self) -> None:
        """A stream that connects but never delivers frames terminates."""
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[]])
        source = make_source(
            transport=transport,
            decoder=decoder,
            policy=ReconnectPolicy(
                max_attempts=2, base_delay_seconds=0.001, max_delay_seconds=0.002
            ),
        )

        await source.open()
        with pytest.raises(SourceTerminatedError, match="reconnection exhausted"):
            async for _ in source:
                pass

        assert source.state is FrameSourceState.FAILED
        await source.aclose()

    async def test_reconnect_failures_then_success(self) -> None:
        """Initial connect succeeds; first reconnect fails; second succeeds."""
        transport = FakeRtspTransport(fail_on_calls={2})
        decoder = FakeRtspDecoder([[make_frame(payload=1)], [make_frame(payload=2)]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        await source.open()  # connect call 1
        first = await anext(source)  # session 1 frame
        # reconnect attempt 1 (call 2) fails, attempt 2 (call 3) succeeds
        second = await anext(source)

        assert first.frame_index == 0
        assert second.frame_index == 1
        assert transport.connect_calls == 3
        assert source.reconnects == 1
        await source.aclose()

    async def test_decode_error_counts_without_reconnect(self) -> None:
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([
            [make_frame(payload=1), make_frame(payload=2), make_frame(payload=3)]
        ])
        decoder.decode_error_at = {(0, 0)}  # first frame of session 1 corrupt
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        await source.open()
        first = await anext(source)  # corrupt frame skipped → frame 1
        second = await anext(source)  # frame 2

        assert first.frame_index == 0
        assert second.frame_index == 1
        assert source.decode_errors == 1
        assert source.reconnects == 0  # never hit EOF; no reconnect needed
        await source.aclose()

    async def test_reconnect_decoder_open_failure_disconnects_orphan(self) -> None:
        """A session that connects but fails decode-open is torn down, not leaked."""

        class OpenFailDecoder(FakeRtspDecoder):
            fail_open_after = 2  # initial open OK; reconnects fail to decode

            async def open(self, stream: AsyncIterator[bytes]) -> None:
                if self.opened + 1 >= self.fail_open_after:
                    msg = "cannot decode reconnected session"
                    raise FrameDecodeError(msg)
                await super().open(stream)

        transport = FakeRtspTransport()
        decoder = OpenFailDecoder([[make_frame(payload=1)], [make_frame(payload=2)]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        await source.open()
        await anext(source)  # session 1 frame
        # Reconnect: connect call 2 succeeds, decoder.open fails → orphan
        # session must be disconnected, then the next attempt also fails
        # decode-open, exhausting the 3-attempt policy.
        with pytest.raises(SourceTerminatedError, match="reconnection exhausted"):
            await anext(source)

        # Every connect that failed decode-open was disconnected (no leak).
        assert transport.disconnect_calls >= 2
        assert source.state is FrameSourceState.FAILED
        await source.aclose()

    async def test_typed_boundary_error_not_double_wrapped(self) -> None:
        class TypedErrorDecoder(FakeRtspDecoder):
            async def read(self) -> DecodedFrame | None:
                raise FrameSourceError("typed decoder boundary error")

        transport = FakeRtspTransport()
        source = make_source(
            transport=transport, decoder=TypedErrorDecoder([[]]), policy=tiny_policy()
        )

        await source.open()
        with pytest.raises(FrameSourceError, match=r"^typed decoder boundary error$"):
            await anext(source)
        await source.aclose()


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TestTimestamps:
    async def test_uses_decoder_source_timestamp(self) -> None:
        ts = datetime(2026, 7, 29, 12, 0, 5, tzinfo=UTC)
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[make_frame(source_timestamp=ts)]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        async with source:
            packet = await anext(source)

        assert packet.event_time == ts

    async def test_falls_back_to_receipt_time_utc(self) -> None:
        before = datetime.now(UTC)
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[make_frame()]])  # no source timestamp
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        async with source:
            packet = await anext(source)
        after = datetime.now(UTC)

        assert packet.event_time.tzinfo is not None
        assert before <= packet.event_time <= after


# ---------------------------------------------------------------------------
# Cancellation & cleanup
# ---------------------------------------------------------------------------


class TestCancellationAndCleanup:
    async def test_cancellation_still_closes_transport(self) -> None:
        class BlockingDecoder(FakeRtspDecoder):
            def __init__(self) -> None:
                super().__init__([[make_frame()]])
                self._release = asyncio.Event()

            async def read(self) -> DecodedFrame | None:
                await self._release.wait()
                return None

        transport = FakeRtspTransport()
        decoder = BlockingDecoder()
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        await source.open()
        task = asyncio.create_task(anext(source))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await source.aclose()

        assert decoder.closed is True
        assert transport.disconnect_calls == 1
        assert source.state is FrameSourceState.CLOSED

    async def test_cleanup_after_body_exception(self) -> None:
        transport = FakeRtspTransport()
        decoder = FakeRtspDecoder([[make_frame()]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        with pytest.raises(RuntimeError, match="pipeline failed"):
            async with source:
                msg = "pipeline failed"
                raise RuntimeError(msg)

        assert decoder.closed is True
        assert transport.disconnect_calls == 1
        assert source.state is FrameSourceState.CLOSED

    async def test_aclose_before_open_is_safe(self) -> None:
        transport = FakeRtspTransport()
        source = make_source(
            transport=transport,
            decoder=FakeRtspDecoder([[]]),
            policy=tiny_policy(),
        )
        await source.aclose()
        assert source.state is FrameSourceState.CLOSED
        assert transport.disconnect_calls == 1

    async def test_cleanup_on_failed_open(self) -> None:
        transport = FakeRtspTransport(fail_on_calls={1})
        decoder = FakeRtspDecoder([[make_frame()]])
        source = make_source(transport=transport, decoder=decoder, policy=tiny_policy())

        with pytest.raises(RtspConnectionError):
            await source.open()
        assert source.state is FrameSourceState.FAILED
        # aclose() from FAILED still releases resources (idempotent).
        await source.aclose()
        assert transport.disconnect_calls >= 1
        assert source.state is FrameSourceState.CLOSED
