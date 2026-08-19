"""Object-storage evidence extractor (Task 17.6).

The REAL implementation of the ``EvidenceExtractor`` port: reads the
resolved recording bytes through ``StoragePort`` (never a provider SDK),
trims the requested interval through the ``MediaProcessor`` codec seam
(the only place video-processing logic may exist — mirroring Task 11's
``FrameDecoder``), and writes the extracted evidence artifact back to
object storage under the canonical evidence key hierarchy.

Flow:

    EvidenceRef + ResolvedSourceSegment
        → RecordingLocator (exact object; segment scope passed verbatim)
        → StoragePort.object_exists / get_object_stream
        → MediaProcessor (codec seam; the actual trim)
        → StoragePort.put_object_stream (evidence artifact, sha-256)
        → ExtractedEvidence (actual window, media facts, provenance)

Guarantees:

- SOURCE-INDEPENDENT: no RTSP, no file-specific logic, no OpenCV/FFmpeg
  commands in the evidence layer; the codec lives behind MediaProcessor.
- SCOPED: the locator is called with the segment's tenant/venue — never
  "latest", never outside scope, never a camera substitution.
- RESOURCE-SAFE: the source stream and the processor are closed in a
  ``finally`` on EVERY path — success, failure, corruption, and
  ``asyncio.CancelledError`` — so no file handles/processes leak.
- CANCELLABLE: the token is honored before, during (after processing),
  and the cooperative awaits are asyncio cancellation points.
- DETERMINISTIC: the artifact identity + evidence object key derive from
  the evidence request + resolved interval (Task 7); the same inputs
  produce the same artifact.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from backend.app.domain.evidence.extraction import (
    ExtractedEvidence,
    ExtractionCancellationToken,
    ExtractionStatus,
    deterministic_extraction_id,
)
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceResolutionStatus,
)
from backend.app.infrastructure.evidence.locator import RecordingLocator
from backend.app.infrastructure.evidence.processing import (
    MediaProcessingStatus,
    MediaProcessor,
)
from backend.app.infrastructure.storage.exceptions import StorageError
from backend.app.infrastructure.storage.key_builder import build_evidence_key
from backend.app.infrastructure.storage.protocol import StoragePort
from contracts.events import EvidenceRef

__all__ = ["ObjectStorageEvidenceExtractor"]


def _content_type_for(media_format: str) -> str:
    """Map a canonical media format to a MIME content type."""
    return {
        "mp4": "video/mp4",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "pdf": "application/pdf",
    }.get(media_format, "application/octet-stream")


async def _chunks(data: bytes, size: int = 1 << 20) -> AsyncIterator[bytes]:  # ruff: ignore[unused-async] -- must be AsyncIterator for StoragePort
    """Bounded-size chunked view of the artifact bytes for storage.

    Deliberately an async generator (no awaits): the StoragePort consumes
    streams with ``async for``, so the type must be ``AsyncIterator``.
    """
    for index in range(0, len(data), size):
        yield data[index : index + size]


class ObjectStorageEvidenceExtractor:
    """Real evidence extraction adapter (Task 17.6)."""

    def __init__(
        self,
        *,
        storage: StoragePort,
        locator: RecordingLocator,
        processor: MediaProcessor,
    ) -> None:
        self._storage = storage
        self._locator = locator
        self._processor = processor

    async def extract(
        self,
        evidence: EvidenceRef,
        segment: ResolvedSourceSegment,
        *,
        cancellation: ExtractionCancellationToken | None = None,
    ) -> ExtractedEvidence:
        # The processor's lifecycle is owned by this call: close() is
        # invoked on EVERY path (success, failure, corruption,
        # cancellation, invalid input) — the contract guarantees close()
        # is idempotent and safe before any processing.
        stream: AsyncIterator[bytes] | None = None
        try:
            # --- Input validation + pre-cancellation ---
            if segment.requested_end < segment.requested_start:
                return self._result(
                    segment, ExtractionStatus.EXTRACTION_FAILED, reason="invalid time range"
                )
            if self._is_cancelled(cancellation):
                return self._result(
                    segment, ExtractionStatus.CANCELLED, reason="cancelled before extraction"
                )
            if segment.status in (
                SourceResolutionStatus.SOURCE_NOT_FOUND,
                SourceResolutionStatus.AUTHORIZATION_FAILURE,
            ):
                return self._result(
                    segment,
                    ExtractionStatus.SOURCE_NOT_FOUND,
                    reason=segment.reason or segment.status.value,
                )
            if not segment.segments:
                return self._result(
                    segment,
                    ExtractionStatus.SOURCE_NOT_FOUND,
                    reason="no resolved source segment",
                )

            if segment.tenant_id is None or segment.venue_id is None:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="resolved segment missing tenant/venue scope — cannot key the artifact",
                )

            if segment.status is SourceResolutionStatus.PARTIAL_COVERAGE:
                requested_start = segment.segments[0].start_time
                requested_end = segment.segments[-1].end_time
            else:
                requested_start = segment.requested_start
                requested_end = segment.requested_end

            first = segment.segments[0]

            # --- Locate the exact recording (segment scope passed verbatim) ---
            reference = await self._locator.locate(
                tenant_id=segment.tenant_id,
                venue_id=segment.venue_id,
                asset_id=first.asset_id,
                session_id=first.session_id,
            )
            if self._is_cancelled(cancellation):
                return self._result(
                    segment, ExtractionStatus.CANCELLED, reason="cancelled during extraction"
                )
            if reference is None:
                return self._result(
                    segment,
                    ExtractionStatus.SOURCE_NOT_FOUND,
                    reason="no recording located for the resolved source",
                )

            try:
                exists = await self._storage.object_exists(reference.object_key)
            except StorageError:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="storage failure during extraction",
                )
            if not exists:
                return self._result(
                    segment,
                    ExtractionStatus.SOURCE_NOT_FOUND,
                    reason="recording object not found in storage",
                )

            try:
                stream = self._storage.get_object_stream(reference.object_key)
            except StorageError:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="storage failure while opening the recording stream",
                )
            try:
                processed = await self._processor.process(
                    stream,
                    requested_start=requested_start,
                    requested_end=requested_end,
                    source_capture_start=reference.capture_time,
                )
            except StorageError:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="storage failure while reading the recording stream",
                )
            if self._is_cancelled(cancellation):
                return self._result(
                    segment,
                    ExtractionStatus.CANCELLED,
                    reason="cancelled during extraction",
                )

            if processed.status is MediaProcessingStatus.CORRUPT:
                return self._result(
                    segment,
                    ExtractionStatus.CORRUPT_SOURCE,
                    reason=processed.reason or "source bytes could not be decoded",
                )
            if processed.status is MediaProcessingStatus.EMPTY:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason=processed.reason or "empty interval — no extractable media",
                )
            if processed.status is MediaProcessingStatus.FAILED:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason=processed.reason or "media processing failed",
                )
            if processed.actual_start is None or processed.actual_end is None:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="media processing produced no actual window",
                )

            # --- Write the evidence artifact (canonical evidence key) ---
            extraction_id = deterministic_extraction_id(segment)
            evidence_key = build_evidence_key(
                segment.tenant_id,
                segment.venue_id,
                extraction_id,
                processed.media_format,
                capture_time=processed.actual_start,
            )
            checksum = hashlib.sha256(processed.data).hexdigest()
            try:
                await self._storage.put_object_stream(
                    evidence_key,
                    _chunks(processed.data),
                    content_type=_content_type_for(processed.media_format),
                    size_bytes=len(processed.data),
                    checksum_sha256=checksum,
                    custom_metadata={
                        "evidence_ref_id": str(segment.evidence_ref_id),
                        "source_asset_id": str(first.asset_id),
                    },
                )
            except StorageError:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="storage failure during artifact write",
                )

            partial = (
                segment.status is SourceResolutionStatus.PARTIAL_COVERAGE
                or processed.actual_start > requested_start
                or processed.actual_end < requested_end
            )
            return self._result(
                segment,
                ExtractionStatus.PARTIAL if partial else ExtractionStatus.SUCCESS,
                actual_start=processed.actual_start,
                actual_end=processed.actual_end,
                start_frame=processed.start_frame,
                end_frame=processed.end_frame,
                media_path=evidence_key,
                media_format=processed.media_format,
                duration_seconds=processed.duration_seconds,
                size_bytes=len(processed.data),
                checksum=checksum,
                metadata=dict(processed.metadata),
            )
        finally:
            if stream is not None:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
            # close() is idempotent and safe before any processing — invoke
            # it on EVERY path (early returns included), never leak.
            await self._processor.close()

    @staticmethod
    def _is_cancelled(cancellation: ExtractionCancellationToken | None) -> bool:
        return cancellation is not None and cancellation.is_cancelled

    @staticmethod
    def _result(
        segment: ResolvedSourceSegment,
        status: ExtractionStatus,
        *,
        reason: str | None = None,
        actual_start: datetime | None = None,
        actual_end: datetime | None = None,
        start_frame: int | None = None,
        end_frame: int | None = None,
        media_path: str | None = None,
        media_format: str | None = None,
        duration_seconds: float | None = None,
        size_bytes: int | None = None,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExtractedEvidence:
        meta: dict[str, Any] = dict(metadata or {})
        if checksum is not None:
            meta["checksum_sha256"] = checksum
        return ExtractedEvidence(
            extraction_id=deterministic_extraction_id(segment),
            status=status,
            evidence_ref_id=segment.evidence_ref_id,
            event_id=segment.event_id,
            tenant_id=segment.tenant_id,
            venue_id=segment.venue_id,
            session_id=segment.video_session_id,
            camera_id=segment.camera_id,
            configuration_version_id=segment.configuration_version_id,
            rule_id=segment.rule_id,
            rule_version=segment.rule_version,
            requested_start=segment.requested_start,
            requested_end=segment.requested_end,
            actual_start_time=actual_start,
            actual_end_time=actual_end,
            start_frame=start_frame,
            end_frame=end_frame,
            media_path=media_path,
            media_format=media_format,
            duration_seconds=duration_seconds,
            size_bytes=size_bytes,
            metadata=meta,
            reason=reason,
        )
