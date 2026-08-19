"""FileFrameSource — recorded-video ingestion from Task 9 object storage.

Reads recorded media bytes through the existing ``StoragePort``
abstraction (never a provider SDK directly), decodes them through the
``FrameDecoder`` protocol, and emits canonical ``FramePacket`` values
via the Phase 3 ``FrameSource`` lifecycle contract.

Timestamp policy (recorded):
- ``event_time = capture_time + pts_seconds`` when both are available;
- otherwise the decoder's per-frame ``source_timestamp``;
- otherwise the source FAILS explicitly — a valid event time is never
  fabricated.

Failure policy:
- missing/unreadable storage object, or an unreadable container, fails
  ``open()`` (state FAILED; partially acquired resources released);
- a corrupt frame raises ``FrameDecodeError`` — the base counts it,
  skips it, and terminates only after sustained consecutive failures;
- EOF is signalled with ``StopAsyncIteration`` (RUNNING → DRAINING).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime, timedelta

from backend.app.infrastructure.storage.exceptions import StorageError
from backend.app.infrastructure.storage.protocol import StoragePort
from backend.app.intelligence.sources.base import FrameData, FrameSource
from backend.app.intelligence.sources.decoder import DecodedFrame, FrameDecoder
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    SourceNotOpenError,
)
from contracts.common import VideoAssetId, VideoSessionId
from contracts.video import FramePacket, SourceType

__all__ = ["FileFrameSource"]


class FileFrameSource(FrameSource):
    """A recorded-video frame source backed by a Task 9 storage object."""

    def __init__(
        self,
        *,
        session_id: VideoSessionId,
        source_ref: VideoAssetId | None = None,
        storage: StoragePort,
        object_key: str,
        decoder: FrameDecoder,
        capture_time: datetime | None = None,
        max_consecutive_decode_errors: int = 100,
    ) -> None:
        super().__init__(
            session_id=session_id,
            source_type=SourceType.RECORDED,
            source_ref=source_ref,
            max_consecutive_decode_errors=max_consecutive_decode_errors,
        )
        if not object_key:
            msg = "object_key must be a non-empty string"
            raise ValueError(msg)
        self._storage = storage
        self._object_key = object_key
        self._decoder = decoder
        self._capture_time = capture_time
        self._stream: AsyncIterator[bytes] | None = None
        self._decoder_started = False

    @property
    def object_key(self) -> str:
        """The storage object key this source reads from."""
        return self._object_key

    # ------------------------------------------------------------------
    # FrameSource hooks
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        try:
            exists = await self._storage.object_exists(self._object_key)
        except StorageError as exc:
            raise FrameSourceError(
                f"cannot access storage object '{self._object_key}'", cause=exc
            ) from exc
        if not exists:
            raise FrameSourceError(f"storage object not found: '{self._object_key}'")
        try:
            self._stream = self._storage.get_object_stream(self._object_key)
            await self._decoder.open(self._stream)
        except StorageError as exc:
            raise FrameSourceError(
                f"cannot open storage object '{self._object_key}'", cause=exc
            ) from exc
        except FrameDecodeError:
            # Unreadable container — explicit open failure (state FAILED).
            raise
        self._decoder_started = True

    async def _produce_next(self) -> FramePacket:
        if not self._decoder_started or self._stream is None:
            raise SourceNotOpenError("source not open; call open() before iteration")
        try:
            frame = await self._decoder.read()
        except FrameDecodeError:
            raise  # base counts, skips, or terminates
        except FrameSourceError:
            raise  # decoder emitted a typed boundary error — do not re-wrap
        except Exception as exc:
            raise FrameSourceError(
                f"decoder failure for object '{self._object_key}'", cause=exc
            ) from exc
        if frame is None:
            raise StopAsyncIteration
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
        # Close the decoder unconditionally: the protocol guarantees
        # close() is idempotent and safe before open(), so a partially
        # initialized decoder from a failed _start() is also released.
        with suppress(BaseException):  # preserve any original failure
            await self._decoder.close()
        self._decoder_started = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    # ------------------------------------------------------------------
    # Timestamp resolution
    # ------------------------------------------------------------------

    def _resolve_event_time(self, frame: DecodedFrame) -> datetime:
        if self._capture_time is not None and frame.pts_seconds is not None:
            return self._capture_time + timedelta(seconds=frame.pts_seconds)
        if frame.source_timestamp is not None:
            return frame.source_timestamp
        # Unresolvable timing is a terminal source defect: every frame would
        # fail identically, so mark FAILED instead of raising per frame.
        self._fail()
        raise FrameSourceError(
            "cannot resolve event_time: capture_time+pts and source_timestamp are both unavailable"
        )
