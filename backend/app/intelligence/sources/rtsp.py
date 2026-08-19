"""RTSPFrameSource — live camera ingestion (Task 11, Phase 6).

The live counterpart to ``FileFrameSource``: it produces the SAME
canonical ``FramePacket`` semantics through the SAME ``FrameSource``
contract and the SAME ``FrameDecoder`` boundary — there is no separate
RTSP pipeline or second frame schema.

Dependency boundary: no RTSP SDK is imported here.  Connection is
delegated to the ``RtspTransport`` protocol (implemented by a future
provider adapter, e.g. aiortsp/PyAV, mirroring how ``StoragePort``
isolates object storage and ``FrameDecoder`` isolates decoding).  This
contract boundary is production-quality even though the concrete
transport adapter is not yet wired, per the Task 11 dependency-state
rule: no fake functionality is invented.

Connection lifecycle:

    _start(): transport.connect() → decoder.open(stream)
        (CREATED → RUNNING; a connect/decode-open failure leaves FAILED)

    _produce_next(): decoder.read()
        - FramePacket built exactly like FileFrameSource (fresh frame_id,
          monotonic frame_index, session/source identity, UTC event_time)
        - None (live stream ended) or RtspConnectionError → connection
          lost → bounded reconnect per ReconnectPolicy → back to RUNNING
        - reconnection exhausted → FAILED + SourceTerminatedError
        - FrameDecodeError → counted/skipped by the base contract

    _stop(): decoder.close() + transport.disconnect() (idempotent)

Reconnect policy: bounded exponential backoff reusing the Task 7
reliability primitive ``compute_backoff_delay`` (never a hand-rolled
timer).  The attempt budget is per connection episode and resets only
after a frame is successfully delivered, so a permanently broken stream
terminates instead of spinning.

Timestamps (live): ``event_time`` is the decoder-provided
``source_timestamp`` when present, otherwise the UTC receipt time
(``utc_now``) — a live stream has no capture_time and PTS is relative.

Credentials: RTSP URLs may embed credentials; the source never logs the
raw URL and exposes ``redacted_url`` (userinfo stripped).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from backend.app.infrastructure.reliability.backoff import compute_backoff_delay
from backend.app.intelligence.sources.base import FrameData, FrameSource
from backend.app.intelligence.sources.decoder import DecodedFrame, FrameDecoder
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    RtspConnectionError,
    SourceTerminatedError,
)
from contracts.common import VideoAssetId, VideoSessionId, utc_now
from contracts.video import FramePacket, SourceType

__all__ = [
    "RTSPFrameSource",
    "ReconnectPolicy",
    "RtspTransport",
    "redact_rtsp_url",
]


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Bounded exponential backoff policy for RTSP reconnection."""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            msg = f"max_attempts must be >= 1, got {self.max_attempts}"
            raise ValueError(msg)
        if self.base_delay_seconds <= 0:
            msg = f"base_delay_seconds must be > 0, got {self.base_delay_seconds}"
            raise ValueError(msg)
        if self.max_delay_seconds < self.base_delay_seconds:
            msg = (
                f"max_delay_seconds ({self.max_delay_seconds}) must be >= "
                f"base_delay_seconds ({self.base_delay_seconds})"
            )
            raise ValueError(msg)
        if not 0 <= self.jitter < 1:
            msg = f"jitter must satisfy 0 <= jitter < 1, got {self.jitter}"
            raise ValueError(msg)


@runtime_checkable
class RtspTransport(Protocol):
    """Provider-isolated boundary for an RTSP connection.

    Implementations MUST:

    - raise ``RtspConnectionError`` for connect/stream failures (never a
      bare SDK exception);
    - return a fresh byte stream from each ``connect()`` call (reconnect
      obtains a new stream);
    - make ``disconnect()`` idempotent and safe before ``connect()``.
    """

    async def connect(self) -> AsyncIterator[bytes]:
        """Establish the RTSP session and return the live byte stream."""
        ...

    async def disconnect(self) -> None:
        """Tear down the session; idempotent and safe when never connected."""
        ...


def redact_rtsp_url(url: str) -> str:
    """Strip userinfo (credentials) from an RTSP URL for safe logging.

    ``rtsp://user:secret@cam.local:554/stream`` →
    ``rtsp://cam.local:554/stream``.  Non-credential URLs are returned
    unchanged.
    """
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


class RTSPFrameSource(FrameSource):
    """A live-video frame source backed by an RTSP transport + shared decoder."""

    def __init__(
        self,
        *,
        session_id: VideoSessionId,
        source_ref: VideoAssetId | None = None,
        transport: RtspTransport,
        decoder: FrameDecoder,
        source_url: str = "",
        reconnect_policy: ReconnectPolicy,
        max_consecutive_decode_errors: int = 100,
    ) -> None:
        super().__init__(
            session_id=session_id,
            source_type=SourceType.LIVE,
            source_ref=source_ref,
            max_consecutive_decode_errors=max_consecutive_decode_errors,
        )
        self._transport = transport
        self._decoder = decoder
        self._source_url = source_url
        self._reconnect_policy = reconnect_policy
        self._stream: AsyncIterator[bytes] | None = None
        self._reconnect_attempts = 0
        self._successful_reconnects = 0

    # ------------------------------------------------------------------
    # Identity / observability
    # ------------------------------------------------------------------

    @property
    def source_url(self) -> str:
        """The configured RTSP URL (server-resolved; never client-controlled)."""
        return self._source_url

    @property
    def redacted_url(self) -> str:
        """The RTSP URL with credentials stripped — safe for logs/errors."""
        return redact_rtsp_url(self._source_url)

    @property
    def reconnect_policy(self) -> ReconnectPolicy:
        return self._reconnect_policy

    @property
    def reconnects(self) -> int:
        """Total reconnection attempts that established a session (lifetime).

        Counts connect+open success, even if the reconnected session
        yielded no frames before ending again (the episode budget still
        protects against a stream that never delivers).
        """
        return self._successful_reconnects

    # ------------------------------------------------------------------
    # FrameSource hooks
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        try:
            self._stream = await self._transport.connect()
            await self._decoder.open(self._stream)
        except RtspConnectionError:
            raise  # base marks FAILED; explicit connection failure
        except FrameDecodeError:
            raise  # unreadable stream — explicit open failure (state FAILED)

    async def _produce_next(self) -> FramePacket:
        while True:
            if self._stream is None:
                await self._reconnect_or_fail()
            try:
                frame = await self._decoder.read()
            except FrameDecodeError:
                raise  # base counts, skips, or terminates
            except RtspConnectionError:
                self._stream = None
                continue
            except FrameSourceError:
                raise  # typed boundary error — do not re-wrap
            except Exception as exc:
                raise FrameSourceError(
                    f"decoder failure for stream {self.redacted_url}", cause=exc
                ) from exc
            if frame is None:
                # Live stream ended = connection lost; reconnect (bounded).
                self._stream = None
                continue
            self._reconnect_attempts = 0  # episode recovered — budget resets
            event_time = self._resolve_event_time(frame)
            packet = self._make_packet(
                width=frame.width,
                height=frame.height,
                event_time=event_time,
            )
            self._last_frame_data = FrameData(
                frame_index=packet.frame_index,
                width=frame.width,
                height=frame.height,
                data=frame.data,
                pts_seconds=frame.pts_seconds,
                source_timestamp=frame.source_timestamp,
            )
            return packet

    async def _stop(self) -> None:
        with suppress(BaseException):  # preserve any original failure
            await self._decoder.close()
        self._stream = None
        with suppress(BaseException):
            await self._transport.disconnect()

    # ------------------------------------------------------------------
    # Reconnect policy boundary
    # ------------------------------------------------------------------

    async def _reconnect_or_fail(self) -> None:
        """Reconnect per policy; terminate the source once exhausted.

        Raises:
            SourceTerminatedError: after ``max_attempts`` consecutive
                failed reconnections (state FAILED).
        """
        while self._reconnect_attempts < self._reconnect_policy.max_attempts:
            self._reconnect_attempts += 1
            delay = compute_backoff_delay(
                self._reconnect_attempts,
                base_seconds=self._reconnect_policy.base_delay_seconds,
                max_seconds=self._reconnect_policy.max_delay_seconds,
                jitter=self._reconnect_policy.jitter,
            )
            await asyncio.sleep(delay.total_seconds())
            try:
                stream = await self._transport.connect()
                await self._decoder.open(stream)
            except RtspConnectionError, FrameDecodeError:
                # Best-effort teardown of a session that connected but could
                # not be decoded — never leak an orphaned RTSP session.
                with suppress(BaseException):
                    await self._transport.disconnect()
                self._stream = None
                continue
            self._stream = stream
            self._successful_reconnects += 1
            return
        self._fail()
        raise SourceTerminatedError(
            f"reconnection exhausted after {self._reconnect_policy.max_attempts} "
            f"attempts for {self.redacted_url}"
        )

    # ------------------------------------------------------------------
    # Timestamp resolution (live)
    # ------------------------------------------------------------------

    def _resolve_event_time(self, frame: DecodedFrame) -> datetime:
        if frame.source_timestamp is not None:
            return frame.source_timestamp
        return utc_now()
