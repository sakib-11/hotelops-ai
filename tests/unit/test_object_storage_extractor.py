"""Tests for Task 17.6 — real object-storage evidence extraction.

The ``ObjectStorageEvidenceExtractor`` is the REAL implementation of the
Task 17.5 ``EvidenceExtractor`` port: it reads the resolved recording
through ``StoragePort``, trims the interval through the ``MediaProcessor``
codec seam, and writes the evidence artifact back to object storage under
the canonical evidence key hierarchy.

Covered:

- valid fixture: SUCCESS with artifact written (key, checksum, format);
- corrupt source: CORRUPT_SOURCE (never a raw exception);
- empty interval: EXTRACTION_FAILED;
- invalid time range: EXTRACTION_FAILED;
- cancellation: CANCELLED before and during extraction — never a partial
  artifact;
- cleanup: the source stream and the processor are closed on EVERY path
  (success, failure, corruption, cancellation) — no leaked handles;
- partial coverage: PARTIAL with the contiguous covered window;
- resource release: repeated extractions keep open-handle counts at zero;
- source not found: locator miss, missing recording object, unresolved
  segment — SOURCE_NOT_FOUND;
- storage failures: deterministic EXTRACTION_FAILED outcomes;
- determinism: same inputs → same extraction identity + artifact key;
- provenance preserved on every outcome;
- port conformance: the adapter satisfies the EvidenceExtractor protocol.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.app.domain.evidence.extraction import (
    EvidenceExtractor,
    ExtractedEvidence,
    ExtractionCancellationToken,
    ExtractionStatus,
    deterministic_extraction_id,
)
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceResolutionStatus,
    SourceSegment,
)
from backend.app.infrastructure.evidence.locator import (
    RecordingLocator,
    RecordingReference,
)
from backend.app.infrastructure.evidence.object_storage_extractor import (
    ObjectStorageEvidenceExtractor,
)
from backend.app.infrastructure.evidence.processing import (
    MediaProcessingStatus,
    MediaProcessor,
    ProcessedMedia,
)
from backend.app.infrastructure.storage.exceptions import ObjectNotFoundError
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.infrastructure.storage.key_builder import build_evidence_key
from backend.app.infrastructure.storage.types import ObjectMetadata
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    EvidenceId,
    RuleId,
    RuleVersion,
    TenantId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("80000000-0000-0000-0000-000000000001"))

_RULE_ID = RuleId("dwell_threshold")
_RULE_VERSION = RuleVersion("v1")

_S = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_MID = datetime(2026, 8, 1, 10, 15, 0, tzinfo=UTC)
_E = datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)

_RECORDING_BYTES = b"\x00\x01\x02fake-recording-bytes" * 64
_RECORDING_KEY = f"tenants/{_TENANT}/venues/{_VENUE}/recordings/2026/08/01/{_ASSET}.mp4"


class _TrackingStream:
    """An async stream wrapper that records whether it was closed."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.closed = False

    def __aiter__(self) -> _TrackingStream:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration:
            self.closed = True
            raise

    async def aclose(self) -> None:
        self.closed = True
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()


class _TrackingStorage(FakeStorageAdapter):
    """FakeStorageAdapter whose streams report their closure."""

    def __init__(self) -> None:
        super().__init__()
        self.streams: list[_TrackingStream] = []

    def get_object_stream(self, object_key: str, *, byte_range: str | None = None) -> Any:
        tracked = _TrackingStream(super().get_object_stream(object_key, byte_range=byte_range))
        self.streams.append(tracked)
        return tracked

    @property
    def open_streams(self) -> int:
        return sum(1 for s in self.streams if not s.closed)


class _FakeProcessor:
    """Deterministic MediaProcessor: returns a canned result per call."""

    def __init__(self, results: list[ProcessedMedia] | ProcessedMedia) -> None:
        if isinstance(results, list):
            self._results = list(results)
        else:
            self._results = [results]
        self.process_calls = 0
        self.closed = False
        self.last_consumed = False

    async def process(
        self,
        stream: Any,
        *,
        requested_start: datetime,
        requested_end: datetime,
        source_capture_start: datetime | None = None,
    ) -> ProcessedMedia:
        self.process_calls += 1
        # Consume the stream to prove it was readable.
        async for _ in stream:
            self.last_consumed = True
        if len(self._results) > 1 and self.process_calls <= len(self._results):
            return self._results[self.process_calls - 1]
        return self._results[-1]

    async def close(self) -> None:
        self.closed = True


class _FakeLocator:
    """Deterministic RecordingLocator with call recording."""

    def __init__(self, reference: RecordingReference | None) -> None:
        self._reference = reference
        self.calls: list[tuple[TenantId, VenueId, VideoAssetId, VideoSessionId | None]] = []

    async def locate(
        self,
        *,
        tenant_id: TenantId,
        venue_id: VenueId,
        asset_id: VideoAssetId,
        session_id: VideoSessionId | None = None,
    ) -> RecordingReference | None:
        self.calls.append((tenant_id, venue_id, asset_id, session_id))
        return self._reference


def _ok_media(
    *,
    actual_start: datetime = _S,
    actual_end: datetime = _E,
    start_frame: int | None = 0,
    end_frame: int | None = 1799,
    duration_seconds: float = 1800.0,
    data: bytes = _RECORDING_BYTES,
    media_format: str = "mp4",
    metadata: dict[str, str] | None = None,
) -> ProcessedMedia:
    return ProcessedMedia(
        status=MediaProcessingStatus.OK,
        data=data,
        media_format=media_format,
        actual_start=actual_start,
        actual_end=actual_end,
        start_frame=start_frame,
        end_frame=end_frame,
        duration_seconds=duration_seconds,
        metadata=metadata or {"encoder": "libx264"},
    )


def _segment(
    *,
    status: SourceResolutionStatus = SourceResolutionStatus.RESOLVED,
    requested_start: datetime = _S,
    requested_end: datetime = _E,
    covered_start: datetime | None = None,
    covered_end: datetime | None = None,
    reason: str | None = None,
) -> ResolvedSourceSegment:
    # Segments are populated for RESOLVED and PARTIAL_COVERAGE — the
    # adapter derives the contiguous extraction window from them.
    has_segments = status in (
        SourceResolutionStatus.RESOLVED,
        SourceResolutionStatus.PARTIAL_COVERAGE,
    )
    return ResolvedSourceSegment(
        status=status,
        evidence_ref_id=_REF,
        event_id=_EVENT,
        tenant_id=_TENANT,
        venue_id=_VENUE,
        camera_id=_CAMERA,
        video_session_id=_SESSION,
        configuration_version_id=_CONFIG_V1,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
        requested_start=requested_start,
        requested_end=requested_end,
        segments=(
            (
                SourceSegment(
                    asset_id=_ASSET,
                    camera_id=_CAMERA,
                    session_id=_SESSION,
                    start_time=covered_start or requested_start,
                    end_time=covered_end or requested_end,
                ),
            )
            if has_segments
            else ()
        ),
        reason=reason,
    )


def _reference(*, capture_time: datetime | None = _S) -> RecordingReference:
    return RecordingReference(
        object_key=_RECORDING_KEY, media_format="mp4", capture_time=capture_time
    )


async def _seed_recording(storage: FakeStorageAdapter) -> None:
    await storage.put_object_stream(
        _RECORDING_KEY,
        _chunk(_RECORDING_BYTES),
        content_type="video/mp4",
        size_bytes=len(_RECORDING_BYTES),
    )


async def _chunk(data: bytes) -> Any:
    yield data


_MISSING = object()


async def _extract(
    *,
    locator_ref: RecordingReference | object | None = _MISSING,
    processor: _FakeProcessor | None = None,
    segment: ResolvedSourceSegment | None = None,
    seed: bool = True,
    storage: _TrackingStorage | None = None,
    token: ExtractionCancellationToken | None = None,
) -> tuple[ExtractedEvidence, _TrackingStorage, _FakeProcessor, _FakeLocator]:
    store = storage or _TrackingStorage()
    if seed:
        await _seed_recording(store)
    # _MISSING means "default locator reference"; None means "locator miss".
    locator = _FakeLocator(_reference() if locator_ref is _MISSING else locator_ref)
    proc = processor or _FakeProcessor(_ok_media())
    extractor = ObjectStorageEvidenceExtractor(
        storage=store,
        locator=locator,
        processor=proc,
    )
    result = await extractor.extract(
        segment or _segment(),
        segment or _segment(),
        cancellation=token,
    )
    # The evidence argument is canonical but the adapter derives everything
    # from the resolved segment; pass the same segment twice for simplicity.
    return result, store, proc, locator


# ---------------------------------------------------------------------------
# Port conformance
# ---------------------------------------------------------------------------


def test_adapter_satisfies_evidence_extractor_port() -> None:
    extractor = ObjectStorageEvidenceExtractor(
        storage=FakeStorageAdapter(),
        locator=_FakeLocator(_reference()),
        processor=_FakeProcessor(_ok_media()),
    )
    assert isinstance(extractor, EvidenceExtractor)


def test_locator_and_processor_satisfy_their_ports() -> None:
    assert isinstance(_FakeLocator(_reference()), RecordingLocator)
    assert isinstance(_FakeProcessor(_ok_media()), MediaProcessor)


# ---------------------------------------------------------------------------
# Valid fixture
# ---------------------------------------------------------------------------


async def test_valid_fixture_extracts_and_writes_artifact() -> None:
    result, store, proc, locator = await _extract()

    assert result.status is ExtractionStatus.SUCCESS
    assert result.actual_start_time == _S
    assert result.actual_end_time == _E
    assert result.start_frame == 0
    assert result.end_frame == 1799
    assert result.duration_seconds == pytest.approx(1800.0)
    assert result.size_bytes == len(_RECORDING_BYTES)
    assert result.media_format == "mp4"
    assert result.metadata.get("encoder") == "libx264"
    assert result.reason is None

    # The artifact was written under the canonical evidence key.
    expected_key = build_evidence_key(_TENANT, _VENUE, result.extraction_id, "mp4", capture_time=_S)
    assert result.media_path == expected_key
    assert await store.object_exists(expected_key)

    # Checksum round-trip: the stored object's checksum matches ours.
    meta = await store.get_object_metadata(expected_key)
    assert meta is not None
    assert meta.checksum_sha256 == result.metadata["checksum_sha256"]

    # The locator was scoped by the segment's tenant/venue — verbatim.
    assert locator.calls == [(_TENANT, _VENUE, _ASSET, _SESSION)]
    assert proc.process_calls == 1
    assert proc.last_consumed is True
    assert proc.closed is True
    assert store.open_streams == 0


async def test_valid_fixture_preserves_provenance() -> None:
    result, _, _, _ = await _extract()
    assert result.evidence_ref_id == _REF
    assert result.event_id == _EVENT
    assert result.tenant_id == _TENANT
    assert result.venue_id == _VENUE
    assert result.session_id == _SESSION
    assert result.camera_id == _CAMERA
    assert result.configuration_version_id == _CONFIG_V1
    assert result.rule_id == _RULE_ID
    assert result.rule_version == _RULE_VERSION
    assert result.requested_start == _S
    assert result.requested_end == _E


# ---------------------------------------------------------------------------
# Determinism / idempotency
# ---------------------------------------------------------------------------


async def test_same_inputs_produce_same_extraction_identity() -> None:
    first, _, _, _ = await _extract()
    second, _, _, _ = await _extract()
    assert first.extraction_id == second.extraction_id
    assert first.media_path == second.media_path
    assert first.metadata["checksum_sha256"] == second.metadata["checksum_sha256"]

    # The identity is content-derived from the resolved segment.
    assert first.extraction_id == deterministic_extraction_id(_segment())


async def test_extraction_identity_is_a_stable_uuid5() -> None:
    result, _, _, _ = await _extract()
    # MediaId is a UUID NewType — stable uuid5 over the content.
    parsed = uuid.UUID(str(result.extraction_id))
    assert parsed.version == 5


# ---------------------------------------------------------------------------
# Corrupt source / empty interval / invalid range
# ---------------------------------------------------------------------------


async def test_corrupt_source_returns_corrupt_snapshot() -> None:
    processor = _FakeProcessor(
        ProcessedMedia(
            status=MediaProcessingStatus.CORRUPT,
            reason="source bytes could not be decoded (bad moov atom)",
        )
    )
    result, store, proc, _ = await _extract(processor=processor)

    assert result.status is ExtractionStatus.CORRUPT_SOURCE
    assert "could not be decoded" in (result.reason or "")
    assert result.media_path is None
    assert result.size_bytes is None
    assert proc.closed is True
    assert store.open_streams == 0
    # Nothing was written.
    assert not any(k.startswith("tenants/") for k in store._objects if "evidence" in k)


async def test_empty_interval_returns_failure() -> None:
    processor = _FakeProcessor(
        ProcessedMedia(
            status=MediaProcessingStatus.EMPTY, reason="empty interval — no extractable media"
        )
    )
    result, store, proc, _ = await _extract(processor=processor)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "empty interval" in (result.reason or "")
    assert proc.closed is True
    assert store.open_streams == 0


async def test_processing_failed_returns_failure() -> None:
    processor = _FakeProcessor(
        ProcessedMedia(status=MediaProcessingStatus.FAILED, reason="encoder failure")
    )
    result, _, _, _ = await _extract(processor=processor)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "encoder failure" in (result.reason or "")


async def test_processor_without_actual_window_returns_failure() -> None:
    processor = _FakeProcessor(
        ProcessedMedia(status=MediaProcessingStatus.OK, data=_RECORDING_BYTES)
    )
    result, _, _, _ = await _extract(processor=processor)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "no actual window" in (result.reason or "")


async def test_invalid_time_range_returns_failure() -> None:
    segment = _segment(requested_start=_E, requested_end=_S)
    result, store, proc, locator = await _extract(segment=segment)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "invalid time range" in (result.reason or "")
    assert proc.process_calls == 0  # never touched the processor
    assert locator.calls == []  # never touched the locator
    assert proc.closed is True  # still closed safely
    assert store.open_streams == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancelled_before_extraction() -> None:
    token = ExtractionCancellationToken()
    token.cancel()
    result, store, proc, locator = await _extract(token=token)
    assert result.status is ExtractionStatus.CANCELLED
    assert "cancelled" in (result.reason or "")
    assert proc.process_calls == 0
    assert locator.calls == []
    assert proc.closed is True
    assert store.open_streams == 0


async def test_cancelled_during_extraction_after_processing() -> None:
    token = ExtractionCancellationToken()
    processor = _FakeProcessor(_ok_media())

    async def _cancelling_process(stream: Any, **_: Any) -> ProcessedMedia:
        async for _ in stream:
            token.cancel()
            break
        return _ok_media()

    processor.process = _cancelling_process  # type: ignore[method-assign]
    result, store, proc, _ = await _extract(processor=processor, token=token)
    assert result.status is ExtractionStatus.CANCELLED
    assert proc.closed is True
    assert store.open_streams == 0
    # Cancellation after processing must NOT leave a partial artifact.
    assert not any("evidence" in k for k in store._objects)


# ---------------------------------------------------------------------------
# Source not found
# ---------------------------------------------------------------------------


async def test_locator_miss_returns_source_not_found() -> None:
    result, store, proc, locator = await _extract(locator_ref=None)
    assert result.status is ExtractionStatus.SOURCE_NOT_FOUND
    assert "no recording located" in (result.reason or "")
    assert proc.process_calls == 0
    assert locator.calls == [(_TENANT, _VENUE, _ASSET, _SESSION)]
    assert proc.closed is True
    assert store.open_streams == 0


async def test_recording_object_missing_in_storage() -> None:
    result, store, proc, _ = await _extract(seed=False)
    assert result.status is ExtractionStatus.SOURCE_NOT_FOUND
    assert "not found in storage" in (result.reason or "")
    assert proc.closed is True
    assert store.open_streams == 0


async def test_unresolved_segment_returns_source_not_found() -> None:
    segment = _segment(
        status=SourceResolutionStatus.SOURCE_NOT_FOUND,
        reason="no recording matches the requested source identity and interval",
    )
    result, store, proc, locator = await _extract(segment=segment)
    assert result.status is ExtractionStatus.SOURCE_NOT_FOUND
    assert "no recording matches" in (result.reason or "")
    assert proc.process_calls == 0
    assert locator.calls == []
    assert store.open_streams == 0


async def test_authorization_failure_segment_returns_source_not_found() -> None:
    segment = _segment(
        status=SourceResolutionStatus.AUTHORIZATION_FAILURE,
        reason="candidate asset belongs to another tenant",
    )
    result, _, _, _ = await _extract(segment=segment)
    assert result.status is ExtractionStatus.SOURCE_NOT_FOUND
    assert "another tenant" in (result.reason or "")


# ---------------------------------------------------------------------------
# Partial coverage
# ---------------------------------------------------------------------------


async def test_partial_coverage_segment_returns_partial_with_contiguous_window() -> None:
    segment = _segment(
        status=SourceResolutionStatus.PARTIAL_COVERAGE,
        requested_start=_S,
        requested_end=_E,
        covered_start=_S,
        covered_end=_MID,
        reason="coverage gaps: [10:15,10:30)",
    )
    # The processor is asked for the contiguous covered window [10:00,10:15].
    captured: dict[str, datetime] = {}

    async def _capturing_process(
        stream: Any,
        *,
        requested_start: datetime,
        requested_end: datetime,
        source_capture_start: datetime | None = None,
    ) -> ProcessedMedia:
        captured["start"] = requested_start
        captured["end"] = requested_end
        async for _ in stream:
            pass
        return _ok_media(actual_start=_S, actual_end=_MID)

    processor = _FakeProcessor(_ok_media())
    processor.process = _capturing_process  # type: ignore[method-assign]

    result, store, _, _ = await _extract(segment=segment, processor=processor)
    assert result.status is ExtractionStatus.PARTIAL
    assert captured["start"] == _S
    assert captured["end"] == _MID
    assert result.actual_start_time == _S
    assert result.actual_end_time == _MID
    assert result.media_path is not None
    assert await store.object_exists(result.media_path)
    assert store.open_streams == 0


async def test_truncated_source_bytes_returns_partial() -> None:
    # The processor reports an actual window narrower than requested.
    processor = _FakeProcessor(_ok_media(actual_start=_S, actual_end=_MID, end_frame=899))
    result, _, _, _ = await _extract(processor=processor)
    assert result.status is ExtractionStatus.PARTIAL
    assert result.actual_end_time == _MID
    assert result.end_frame == 899


# ---------------------------------------------------------------------------
# Storage failures
# ---------------------------------------------------------------------------


async def test_storage_unavailable_returns_failure() -> None:
    store = _TrackingStorage()
    await _seed_recording(store)
    store.simulate_unavailable(True)
    result, _, proc, _ = await _extract(storage=store, seed=False)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert proc.closed is True


async def test_recording_object_deleted_between_check_and_read() -> None:
    store = _TrackingStorage()
    await _seed_recording(store)

    class _RacyStorage(_TrackingStorage):
        def get_object_stream(self, object_key: str, *, byte_range: str | None = None) -> Any:
            # The object vanishes between object_exists and get_object_stream.
            self._objects.pop(object_key, None)
            raise ObjectNotFoundError(object_key)

    racy = _RacyStorage()
    await _seed_recording(racy)
    result, _, proc, _ = await _extract(storage=racy)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "opening the recording stream" in (result.reason or "")
    assert proc.closed is True


async def test_artifact_write_failure_returns_failure() -> None:
    store = _TrackingStorage()
    await _seed_recording(store)

    class _BrokenWriteStorage(_TrackingStorage):
        async def put_object_stream(self, *args: Any, **kwargs: Any) -> Any:
            raise ObjectNotFoundError("simulated write failure")

    broken = _BrokenWriteStorage()
    # Seed the recording object directly — the broken writer must only
    # fail on the evidence artifact write, not on seeding.
    broken._objects[_RECORDING_KEY] = (  # type: ignore[attr-defined]
        _RECORDING_BYTES,
        ObjectMetadata(
            object_key=_RECORDING_KEY,
            size_bytes=len(_RECORDING_BYTES),
            content_type="video/mp4",
            etag='"seed"',
            last_modified=_S,
        ),
    )
    result, _, proc, _ = await _extract(storage=broken, seed=False)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "artifact write" in (result.reason or "")
    assert proc.closed is True


# ---------------------------------------------------------------------------
# Resource release — no leaked handles / processes
# ---------------------------------------------------------------------------


async def test_no_leaked_handles_across_all_paths() -> None:
    cases: list[tuple[str, _FakeProcessor, ResolvedSourceSegment | None]] = [
        ("success", _FakeProcessor(_ok_media()), None),
        (
            "corrupt",
            _FakeProcessor(ProcessedMedia(status=MediaProcessingStatus.CORRUPT, reason="corrupt")),
            None,
        ),
        (
            "empty",
            _FakeProcessor(ProcessedMedia(status=MediaProcessingStatus.EMPTY, reason="empty")),
            None,
        ),
        (
            "invalid-range",
            _FakeProcessor(_ok_media()),
            _segment(requested_start=_E, requested_end=_S),
        ),
        (
            "unresolved",
            _FakeProcessor(_ok_media()),
            _segment(
                status=SourceResolutionStatus.SOURCE_NOT_FOUND,
                reason="no recording",
            ),
        ),
        ("locator-miss", _FakeProcessor(_ok_media()), None),
        ("no-seed", _FakeProcessor(_ok_media()), None),
    ]
    for name, processor, segment in cases:
        store = _TrackingStorage()
        await _seed_recording(store)
        locator = _FakeLocator(_reference() if name not in ("locator-miss",) else None)
        extractor = ObjectStorageEvidenceExtractor(
            storage=store,
            locator=locator,
            processor=processor,
        )
        await extractor.extract(segment or _segment(), segment or _segment())
        assert processor.closed, f"{name}: processor not closed"
        assert store.open_streams == 0, f"{name}: leaked stream handles"
        # Only paths that reach the storage read open a stream.
        if name in ("success", "corrupt", "empty"):
            assert store.streams, f"{name}: expected a stream to have been opened"


async def test_repeated_extractions_do_not_accumulate_handles() -> None:
    store = _TrackingStorage()
    await _seed_recording(store)
    locator = _FakeLocator(_reference())
    processor = _FakeProcessor(_ok_media())
    extractor = ObjectStorageEvidenceExtractor(
        storage=store,
        locator=locator,
        processor=processor,
    )
    for _ in range(5):
        result = await extractor.extract(_segment(), _segment())
        assert result.status is ExtractionStatus.SUCCESS
        assert store.open_streams == 0
    assert len(store.streams) == 5  # all tracked, none leaked
    assert processor.process_calls == 5


async def test_cancellation_releases_resources() -> None:
    token = ExtractionCancellationToken()
    token.cancel()
    result, store, proc, _ = await _extract(token=token)
    assert result.status is ExtractionStatus.CANCELLED
    assert proc.closed is True
    assert store.open_streams == 0


# ---------------------------------------------------------------------------
# Scope / provenance edge cases
# ---------------------------------------------------------------------------


async def test_segment_without_tenant_venue_scope_is_rejected() -> None:
    segment = ResolvedSourceSegment(
        status=SourceResolutionStatus.RESOLVED,
        evidence_ref_id=_REF,
        event_id=_EVENT,
        tenant_id=None,
        venue_id=None,
        camera_id=_CAMERA,
        video_session_id=_SESSION,
        requested_start=_S,
        requested_end=_E,
        segments=(
            SourceSegment(
                asset_id=_ASSET, camera_id=_CAMERA, session_id=_SESSION, start_time=_S, end_time=_E
            ),
        ),
    )
    result, _, proc, locator = await _extract(segment=segment)
    assert result.status is ExtractionStatus.EXTRACTION_FAILED
    assert "tenant/venue scope" in (result.reason or "")
    assert locator.calls == []
    assert proc.closed is True


async def test_empty_segments_list_returns_source_not_found() -> None:
    segment = _segment(
        status=SourceResolutionStatus.RESOLVED,
        requested_start=_S,
        requested_end=_E,
    )
    # RESOLVED with no segments is malformed; the adapter refuses.
    segment = segment.model_copy(update={"segments": ()})
    result, _, proc, locator = await _extract(segment=segment)
    assert result.status is ExtractionStatus.SOURCE_NOT_FOUND
    assert "no resolved source segment" in (result.reason or "")
    assert locator.calls == []
    assert proc.closed is True


async def test_non_mp4_format_propagates_to_artifact() -> None:
    processor = _FakeProcessor(
        _ok_media(data=b"image-bytes", media_format="jpg", duration_seconds=None)
    )
    result, store, _, _ = await _extract(processor=processor)
    assert result.status is ExtractionStatus.SUCCESS
    assert result.media_format == "jpg"
    assert result.media_path is not None
    assert result.media_path.endswith(".jpg")
    assert await store.object_exists(result.media_path)


async def test_capture_time_drives_key_partitioning() -> None:
    # A recording captured on a different day partitions the artifact key
    # under that day — deterministic from the processor's actual window.
    late_start = datetime(2026, 8, 2, 9, 0, 0, tzinfo=UTC)
    late_end = datetime(2026, 8, 2, 9, 30, 0, tzinfo=UTC)
    # Match the requested window to the actual window so this is a full
    # SUCCESS extraction, not a PARTIAL (different window → PARTIAL).
    segment = _segment(requested_start=late_start, requested_end=late_end)
    processor = _FakeProcessor(_ok_media(actual_start=late_start, actual_end=late_end))
    result, _, _, _ = await _extract(segment=segment, processor=processor)
    assert result.status is ExtractionStatus.SUCCESS
    assert result.media_path is not None
    assert "/2026/08/02/" in result.media_path


async def test_result_round_trips_through_contract() -> None:
    result, _, _, _ = await _extract()
    rt = ExtractedEvidence.model_validate(result.model_dump())
    assert rt == result
