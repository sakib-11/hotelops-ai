"""Canonical ingestion contract: FrameSource (Task 11, Phase 3).

The single ingestion boundary shared by live (RTSP) and recorded (file)
sources.  Downstream CV consumers depend ONLY on this contract and the
canonical ``FramePacket`` (``contracts.video``) — they cannot tell
whether frames originated from a live camera or a recorded file
(ADR-005: shared live/recorded pipeline).

Lifecycle state machine (enforced by this base):

    CREATED ──open()──▶ RUNNING ──EOF──▶ DRAINING ──aclose()──▶ CLOSED
        │                  │                                      ▲
        │                  │  sustained decode failure            │
        │                  └───────────────▶ FAILED ──────────────┘
        │                        (terminal; no further frames)
        └── aclose() (idempotent, never opened)

Resource ownership:
- ``open()`` acquires resources (transport/decoder) exactly once.
- ``aclose()`` releases them exactly once and is idempotent; it is
  safe to call from any state and must be reached even when a consumer
  cancels iteration mid-stream (use ``async with``).
- Every ``await`` in the source is a cooperative cancellation point;
  cancellation propagates ``asyncio.CancelledError`` to the consumer
  and the context manager still closes the source.

Timestamps:
- ``FramePacket.event_time`` is the validated timezone-aware UTC event
  time (recorded: capture_time + pts; live: receipt time).
- ``FrameData.source_timestamp``/``pts_seconds`` carry source-level
  timing available only at ingestion.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Self
from uuid import uuid4

from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_FRAMES,
    record_pipeline_metric,
)
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    InvalidStateTransitionError,
    SourceNotOpenError,
    SourceTerminatedError,
)
from contracts.common import FrameId, VideoAssetId, VideoSessionId
from contracts.video import FramePacket, SourceType


class FrameSourceState(StrEnum):
    """Lifecycle states of a frame source."""

    CREATED = "created"
    RUNNING = "running"
    DRAINING = "draining"
    CLOSED = "closed"
    FAILED = "failed"


class DecodeStatus(StrEnum):
    """Per-frame decode outcome carried by in-process ``FrameData``."""

    OK = "ok"
    DECODE_ERROR = "decode_error"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class FrameData:
    """In-process decoded frame payload (never serialized across boundaries).

    Companion to the canonical ``FramePacket``: the packet carries the
    immutable metadata envelope, ``FrameData`` carries the decoded
    pixel bytes plus source-level timing/decode status available only
    at the ingestion boundary.
    """

    frame_index: int
    width: int | None
    height: int | None
    data: bytes
    pts_seconds: float | None = None
    source_timestamp: datetime | None = None
    decode_status: DecodeStatus = DecodeStatus.OK

    def __post_init__(self) -> None:
        if self.frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        if self.width is not None and self.width < 1:
            raise ValueError("width must be >= 1 when present")
        if self.height is not None and self.height < 1:
            raise ValueError("height must be >= 1 when present")
        if self.pts_seconds is not None and self.pts_seconds < 0:
            raise ValueError("pts_seconds must be non-negative")
        if self.source_timestamp is not None and self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware (UTC)")


class FrameSource(ABC):
    """Abstract ingestion boundary shared by live and recorded sources.

    Subclasses implement the three resource hooks:

    - ``_start()``          — acquire transport/decoder (called by ``open()``)
    - ``_produce_next()``   — return the next ``FramePacket`` or raise
                              ``StopAsyncIteration`` at EOF
    - ``_stop()``           — release all owned resources (called by
                              ``aclose()``); must be idempotent

    The base class owns the state machine, the monotonic frame index,
    frame-id generation, decode-error accounting, and the lifecycle
    guards — subclasses never bypass them.
    """

    def __init__(
        self,
        *,
        session_id: VideoSessionId,
        source_type: SourceType,
        source_ref: VideoAssetId | None = None,
        max_consecutive_decode_errors: int = 100,
    ) -> None:
        self._session_id = session_id
        self._source_type = source_type
        self._source_ref = source_ref
        self._max_consecutive_decode_errors = max_consecutive_decode_errors
        self._state = FrameSourceState.CREATED
        self._frame_index = 0
        self._dropped_frames = 0
        self._decode_errors = 0
        self._consecutive_decode_errors = 0
        self._last_frame_data: FrameData | None = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> VideoSessionId:
        """The VideoSession this source feeds (immutable for the source lifetime)."""
        return self._session_id

    @property
    def source_type(self) -> SourceType:
        """LIVE or RECORDED — informational only; downstream must not branch on it."""
        return self._source_type

    @property
    def source_ref(self) -> VideoAssetId | None:
        """Optional asset identity referenced by emitted frames."""
        return self._source_ref

    @property
    def last_frame_data(self) -> FrameData | None:
        """Companion in-process decoded payload of the most recent FramePacket.

        Subclasses populate this alongside every emitted ``FramePacket`` so
        the bounded queue / CV pipeline can deliver the decoded bytes with
        the canonical metadata envelope.  Never serialized; ``None`` until
        the first frame is produced.
        """
        return self._last_frame_data

    @property
    def state(self) -> FrameSourceState:
        """Current lifecycle state of the source."""
        return self._state

    @property
    def dropped_frames(self) -> int:
        """Total frames dropped by the bounded-queue/backpressure policy."""
        return self._dropped_frames

    @property
    def decode_errors(self) -> int:
        """Total frames skipped due to decode failures."""
        return self._decode_errors

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Acquire source resources and transition CREATED → RUNNING.

        Raises:
            InvalidStateTransitionError: if the source is not CREATED.
        """
        if self._state is not FrameSourceState.CREATED:
            raise InvalidStateTransitionError(
                f"open() is only valid from CREATED, current state={self._state.value}"
            )
        try:
            await self._start()
        except BaseException:
            # Best-effort release of any resources _start() acquired before
            # failing.  Python does NOT call __aexit__ when __aenter__ raises,
            # so this is the only guaranteed cleanup point for a partial start.
            # _stop() is contractually idempotent, so this is always safe.
            self._state = FrameSourceState.FAILED
            with suppress(BaseException):  # preserve the original start failure
                await self._stop()
            raise
        self._state = FrameSourceState.RUNNING

    async def aclose(self) -> None:
        """Release all owned resources; idempotent and cancellation-safe.

        Safe to call from any state, including CREATED, DRAINING, and
        FAILED.  The state is set to CLOSED before ``_stop()`` runs so
        resources are released even if ``_stop()`` raises.
        """
        if self._state is FrameSourceState.CLOSED:
            return
        self._state = FrameSourceState.CLOSED
        await self._stop()

    # ------------------------------------------------------------------
    # Async iteration protocol (produces canonical FramePackets)
    # ------------------------------------------------------------------

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> FramePacket:
        """Return the next canonical FramePacket.

        Raises:
            StopAsyncIteration: at clean EOF (RUNNING → DRAINING).
            SourceNotOpenError: iteration attempted before open() or after close.
            SourceTerminatedError: the source is in the terminal FAILED state.
        """
        if self._state is FrameSourceState.CLOSED:
            raise SourceNotOpenError("source is closed")
        if self._state is FrameSourceState.FAILED:
            raise SourceTerminatedError("source terminated in FAILED state")
        if self._state not in (FrameSourceState.RUNNING, FrameSourceState.DRAINING):
            raise SourceNotOpenError(
                f"source not open (state={self._state.value}); call open() first"
            )
        while True:
            try:
                packet = await self._produce_next()
            except StopAsyncIteration:
                if self._state is FrameSourceState.RUNNING:
                    self._state = FrameSourceState.DRAINING
                raise
            except FrameDecodeError as exc:
                self._decode_errors += 1
                self._consecutive_decode_errors += 1
                if self._consecutive_decode_errors >= self._max_consecutive_decode_errors:
                    self._state = FrameSourceState.FAILED
                    raise SourceTerminatedError(
                        f"{self._consecutive_decode_errors} consecutive decode failures"
                    ) from exc
                continue
            self._consecutive_decode_errors = 0
            # Task 18.18 — one frame crossed the ingestion boundary.
            record_pipeline_metric(PIPELINE_METRIC_FRAMES)
            return packet

    # ------------------------------------------------------------------
    # Async context manager (guaranteed resource release)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abstractmethod
    async def _start(self) -> None:
        """Acquire source resources (transport, decoder). Called once by open()."""

    @abstractmethod
    async def _produce_next(self) -> FramePacket:
        """Produce the next canonical FramePacket or raise StopAsyncIteration at EOF.

        May raise ``FrameDecodeError`` for a corrupt frame — the base
        counts it, skips it, and continues until the consecutive-error
        limit terminates the source.
        """

    @abstractmethod
    async def _stop(self) -> None:
        """Release all owned resources. Must be idempotent; called once by aclose()."""

    # ------------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------------

    def _make_packet(
        self,
        *,
        width: int,
        height: int,
        event_time: datetime,
        source_ref: VideoAssetId | None = None,
    ) -> FramePacket:
        """Build the next canonical FramePacket with a fresh frame_id.

        The frame index advances monotonically per emitted frame (decode
        failures do not consume an index), and ``event_time`` is
        validated as timezone-aware UTC by the FramePacket contract.
        """
        packet = FramePacket(
            frame_id=FrameId(uuid4()),
            session_id=self._session_id,
            frame_index=self._frame_index,
            event_time=event_time,
            width=width,
            height=height,
            source_ref=source_ref if source_ref is not None else self._source_ref,
        )
        self._frame_index += 1
        return packet

    def note_dropped(self) -> None:
        """Record one frame dropped by the bounded-queue/backpressure policy.

        Called by the pipeline when the queue evicts a frame (e.g. under
        the DROP_OLDEST full policy) so the source's observability counter
        stays in sync with the queue's own drop accounting.
        """
        self._dropped_frames += 1

    def _fail(self) -> None:
        """Mark the source as terminally FAILED (no further frames will be produced)."""
        self._state = FrameSourceState.FAILED


__all__ = [
    "DecodeStatus",
    "FrameData",
    "FrameSource",
    "FrameSourceState",
]
