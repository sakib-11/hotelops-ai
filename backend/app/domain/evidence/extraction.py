"""Source-independent evidence extraction interface (Task 17.5).

The evidence layer depends ONLY on this abstraction — it never contains
RTSP-specific branching, file-specific business logic, OpenCV-specific
logic, or scattered FFmpeg commands:

    EvidenceRef + ResolvedSourceSegment
            ↓ EvidenceExtractor (port)
        ExtractedEvidence

``EvidenceExtractor`` is the provider-independent port. Concrete
extractors (file-based, object-storage-based, ...) live behind it and may
internally use the Task 11 ingestion abstractions (e.g. the recorded
``FrameSource`` from ``intelligence/sources/file.py``) — the evidence
layer never sees a ``FrameSource``, decoder, or transport. Task 11
semantics are untouched; nothing here duplicates ``FrameSource``.

The extractor is COOPERATIVELY CANCELLABLE and RESOURCE-SAFE:

- ``ExtractionCancellationToken`` — an explicit, deterministic
  cancellation signal the caller can set; the extractor checks it at
  defined points and returns ``CANCELLED`` (not a partial artifact).
- Every path (success, failure, corruption, cancellation, exception)
  MUST release its resources — the contract is that ``extract()`` never
  leaks file handles or processes. ``asyncio.CancelledError`` raised
  mid-extraction still releases resources (``finally``) before
  propagating, per Task 11 cancellation semantics.

Outcomes (``ExtractionStatus``):

- ``SUCCESS`` — the full requested interval was extracted.
- ``PARTIAL`` — only part was extracted (resolved partial coverage, or
  the source bytes ended early).
- ``SOURCE_NOT_FOUND`` — no source exists (never resolved, or deleted
  after resolution).
- ``CORRUPT_SOURCE`` — the source bytes could not be decoded.
- ``EXTRACTION_FAILED`` — invalid input (reversed time range, empty
  source) or any other failure.
- ``CANCELLED`` — the cancellation token was set before/during
  extraction.

``ExtractedEvidence`` carries the ACTUAL extracted window (times, frame
range where available), the media reference (path/format/duration/size),
the deterministic extraction identity (content-derived UUID5 — the same
input replays to the same identity, Task 7 idempotency), and the evidence
provenance.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from backend.app.domain.evidence.resolution import ResolvedSourceSegment
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    EvidenceId,
    MediaId,
    RuleId,
    RuleVersion,
    TenantId,
    VenueId,
    VideoSessionId,
    validate_utc,
)
from contracts.events import EvidenceRef
from contracts.temporal import TEMPORAL_ID_NAMESPACE

__all__ = [
    "EvidenceExtractor",
    "ExtractedEvidence",
    "ExtractionCancellationToken",
    "ExtractionStatus",
]


class ExtractionStatus(StrEnum):
    """Deterministic outcome of one evidence extraction (Task 17.5)."""

    SUCCESS = "success"
    PARTIAL = "partial"
    SOURCE_NOT_FOUND = "source_not_found"
    CORRUPT_SOURCE = "corrupt_source"
    EXTRACTION_FAILED = "extraction_failed"
    CANCELLED = "cancelled"


class ExtractionCancellationToken:
    """Cooperative, single-writer cancellation signal for an extraction.

    The caller holds the token and may ``cancel()`` it at any time; the
    extractor checks ``is_cancelled`` at defined points and returns a
    ``CANCELLED`` outcome instead of a partial artifact. Single-threaded
    (asyncio) contract: one writer, no locking required.
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation (idempotent)."""
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """True once cancellation has been requested."""
        return self._cancelled


class ExtractedEvidence(BaseModel, frozen=True):
    """The deterministic result of one evidence extraction.

    ``actual_start_time``/``actual_end_time`` are what was ACTUALLY
    extracted (may be narrower than the requested window for PARTIAL).
    ``media_path`` is the media reference (object key / file path) —
    never the bytes themselves. ``extraction_id`` is content-derived so
    replaying the same inputs produces the same artifact identity.
    """

    model_config = {"extra": "forbid"}

    extraction_id: MediaId
    status: ExtractionStatus
    # Provenance — preserved from the evidence request / resolved segment.
    evidence_ref_id: EvidenceId
    event_id: EventId | None = None
    tenant_id: TenantId | None = None
    venue_id: VenueId | None = None
    session_id: VideoSessionId | None = None
    camera_id: CameraId | None = None
    configuration_version_id: ConfigurationVersionId | None = None
    rule_id: RuleId | None = None
    rule_version: RuleVersion | None = None
    # Requested window (from the resolved segment).
    requested_start: datetime
    requested_end: datetime
    # Actual extracted window + media facts (None when nothing extracted).
    actual_start_time: datetime | None = None
    actual_end_time: datetime | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    media_path: str | None = None
    media_format: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None

    _validate_requested_start = field_validator("requested_start")(validate_utc)
    _validate_requested_end = field_validator("requested_end")(validate_utc)
    _validate_actual_start = field_validator("actual_start_time")(validate_utc)
    _validate_actual_end = field_validator("actual_end_time")(validate_utc)


def deterministic_extraction_id(segment: ResolvedSourceSegment) -> MediaId:
    """Content-derived extraction identity (Task 7 idempotency).

    The same evidence request + resolved source interval always maps to
    the same artifact identity — replay and duplicate delivery collapse
    to one logical extraction, never a fresh UUID.
    """
    from uuid import uuid5

    asset_id = segment.segments[0].asset_id if segment.segments else ""
    canonical = (
        f"evidence_extraction|{segment.evidence_ref_id}|{asset_id}|"
        f"{segment.requested_start.isoformat()}|{segment.requested_end.isoformat()}"
    )
    return MediaId(uuid5(TEMPORAL_ID_NAMESPACE, canonical))


@runtime_checkable
class EvidenceExtractor(Protocol):
    """Provider-independent evidence extraction port (Task 17.5).

    Implementations extract the media for the resolved interval without
    the evidence layer knowing the source kind (RTSP / file / storage).
    Must be resource-safe on every path and cooperate with cancellation.
    """

    async def extract(
        self,
        evidence: EvidenceRef,
        segment: ResolvedSourceSegment,
        *,
        cancellation: ExtractionCancellationToken | None = None,
    ) -> ExtractedEvidence: ...
