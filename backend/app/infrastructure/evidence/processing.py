"""Media processing seam for evidence extraction (Task 17.6).

``MediaProcessor`` is the codec/encoder boundary — the ONLY place the
video-processing SDK (PyAV / OpenCV / FFmpeg) may appear, exactly as
``FrameDecoder`` isolates the decode SDK for Task 11 ingestion. The
evidence layer (and the ``ObjectStorageEvidenceExtractor``) depends only
on this protocol: no RTSP branching, no file-specific logic, no FFmpeg
commands scattered anywhere.

A processor consumes the recording's byte stream, selects the requested
interval (event-time window / frame range where available), and returns
the extracted artifact bytes + the ACTUAL window that was produced:

    MediaProcessor
        ├── process(stream, requested interval)  → ProcessedMedia
        └── close()  — release resources; idempotent, safe before use

Outcomes (``MediaProcessingStatus``):

- ``OK`` — the interval was processed; ``ProcessedMedia.data`` holds the
  artifact bytes and ``actual_start``/``actual_end`` the ACTUAL window
  (narrower than requested when the source ended early → the extractor
  reports PARTIAL).
- ``CORRUPT`` — the source bytes could not be decoded.
- ``EMPTY`` — the source contains no extractable media for the interval.
- ``FAILED`` — any other processing failure (with a deterministic
  ``reason``).

A real processor must be RESOURCE-SAFE: ``close()`` in ``finally`` by its
caller on every path, including cancellation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "MediaProcessingStatus",
    "MediaProcessor",
    "ProcessedMedia",
]


class MediaProcessingStatus(StrEnum):
    """Deterministic outcome of one media-processing operation."""

    OK = "ok"
    CORRUPT = "corrupt"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessedMedia:
    """The extracted artifact bytes + ACTUAL window produced by a processor."""

    status: MediaProcessingStatus
    data: bytes = b""
    media_format: str = "mp4"
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    start_frame: int | None = None
    end_frame: int | None = None
    duration_seconds: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    reason: str | None = None


@runtime_checkable
class MediaProcessor(Protocol):
    """Trim/extract the requested interval from a recording byte stream.

    Implementations MUST:
    - consume the stream and return the extracted artifact bytes;
    - raise nothing for a corrupt source — report ``CORRUPT`` instead;
    - make ``close()`` idempotent and safe before any processing.
    """

    async def process(
        self,
        stream: AsyncIterator[bytes],
        *,
        requested_start: datetime,
        requested_end: datetime,
        source_capture_start: datetime | None = None,
    ) -> ProcessedMedia: ...

    async def close(self) -> None:
        """Release processor resources. Idempotent; safe before use."""
        ...
