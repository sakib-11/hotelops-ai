"""FrameDecoder protocol — isolates the video-decoding library.

``FileFrameSource`` (and future ``RTSPFrameSource``) depend only on this
protocol, never on a decoder SDK (PyAV/OpenCV/ffmpeg).  The concrete
decoder implementation lives behind this boundary so the ingestion
contract stays provider-independent (same convention as
``backend/app/infrastructure/storage/protocol.py``).

A decoder consumes a byte stream (from Task 9 object storage) and
produces decoded frames one at a time:

    FrameDecoder
        ├── open(stream)      — validate the container/stream; may raise
        │                        FrameDecodeError for an unreadable source
        ├── read()            — next DecodedFrame, or None at EOF
        └── close()           — release decoder resources; idempotent
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.app.intelligence.sources.exceptions import FrameDecodeError

__all__ = ["DecodedFrame", "FrameDecodeError", "FrameDecoder"]


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """A single decoded frame emitted by a FrameDecoder.

    Source-level payload: pixel data plus presentation timing.  The
    canonical ``FramePacket`` is built from this by the frame source
    (which owns frame_index/session identity); this type is never
    serialized and never crosses the ingestion boundary.
    """

    width: int
    height: int
    data: bytes
    pts_seconds: float | None = None
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("width must be >= 1")
        if self.height < 1:
            raise ValueError("height must be >= 1")
        if self.pts_seconds is not None and self.pts_seconds < 0:
            raise ValueError("pts_seconds must be non-negative")
        if self.source_timestamp is not None and self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware (UTC)")


@runtime_checkable
class FrameDecoder(Protocol):
    """Decode an arbitrary byte stream into frames.

    Implementations MUST:

    - raise ``FrameDecodeError`` for a corrupt/unreadable frame or
      container (never a bare SDK exception);
    - return ``None`` from ``read()`` exactly once the stream is fully
      consumed (EOF);
    - make ``close()`` idempotent and safe to call before ``open()``.
    """

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        """Validate and open the container; may raise FrameDecodeError."""
        ...

    async def read(self) -> DecodedFrame | None:
        """Return the next decoded frame, or None at EOF."""
        ...

    async def close(self) -> None:
        """Release decoder resources. Idempotent; safe before open()."""
        ...
