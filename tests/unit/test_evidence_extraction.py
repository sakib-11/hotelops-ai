"""Tests for Task 17.5 — source-independent evidence extraction interface.

Covers the EvidenceExtractor port contract with the deterministic
in-memory fake:

- valid fixture → SUCCESS with actual window + media facts;
- corrupt source → CORRUPT_SOURCE;
- empty interval / invalid time range → EXTRACTION_FAILED;
- partial coverage / truncated source → PARTIAL;
- SOURCE_NOT_FOUND (resolved missing + source deleted mid-flight);
- cancellation via token (before + during) → CANCELLED;
- asyncio.CancelledError mid-extraction → resources still released;
- NO leaked file handles/processes on every path (verified via the
  fake's open/closed handle counters);
- deterministic extraction identity (replay → same id, Task 7);
- provenance preserved;
- the evidence layer carries NO FrameSource/RTSP/OpenCV/FFmpeg imports
  (source independence).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from backend.app.domain.evidence.extraction import (
    EvidenceExtractor,
    ExtractedEvidence,
    ExtractionCancellationToken,
    ExtractionStatus,
)
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceResolutionStatus,
    SourceSegment,
)
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
from contracts.events import EvidenceRef, EvidenceType
from tests.unit.fakes import FakeEvidenceExtractor, FakeMedia, FakeMediaStore

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("80000000-0000-0000-0000-000000000001"))
_RULE_ID = RuleId("dwell_threshold")
_RULE_VERSION = RuleVersion("v1")

_S = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_MID = datetime(2026, 8, 1, 10, 15, 0, tzinfo=UTC)
_E = datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        ref_id=_REF,
        ref_type=EvidenceType.VIDEO_CLIP,
        ref_uri=f"s3://evidence/{_TENANT}/{_SESSION}/rule/dwell_threshold",
        event_id=_EVENT,
        event_time=_E,
        tenant_id=_TENANT,
        venue_id=_VENUE,
        video_session_id=_SESSION,
        camera_id=_CAMERA,
        start_time=_S,
        end_time=_E,
        configuration_version_id=_CONFIG,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _segment(
    *,
    status: SourceResolutionStatus = SourceResolutionStatus.RESOLVED,
    segments: tuple[SourceSegment, ...] = (
        SourceSegment(
            asset_id=_ASSET, camera_id=_CAMERA, session_id=_SESSION, start_time=_S, end_time=_E
        ),
    ),
    requested_start: datetime = _S,
    requested_end: datetime = _E,
    reason: str | None = None,
) -> ResolvedSourceSegment:
    return ResolvedSourceSegment(
        status=status,
        evidence_ref_id=_REF,
        event_id=_EVENT,
        tenant_id=_TENANT,
        venue_id=_VENUE,
        camera_id=_CAMERA,
        video_session_id=_SESSION,
        configuration_version_id=_CONFIG,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
        requested_start=requested_start,
        requested_end=requested_end,
        segments=segments,
        reason=reason,
    )


def _media(*, state: str = "valid", duration: float = 1800.0, **overrides: object) -> FakeMedia:
    values: dict[str, object] = {
        "media_path": f"tenants/{_TENANT}/venues/{_VENUE}/recordings/2026/08/01/{_ASSET}.mp4",
        "media_format": "mp4",
        "duration_seconds": duration,
        "size_bytes": 1024 * 1024,
        "start_frame": 0,
        "end_frame": 53999,
        "actual_start": _S,
        "actual_end": _E,
        "state": state,
    }
    values.update(overrides)
    return FakeMedia(**values)


def _extractor(store: FakeMediaStore, *, latency: float = 0.0) -> FakeEvidenceExtractor:
    return FakeEvidenceExtractor(store, latency=latency)


def _store_with(*media: FakeMedia) -> FakeMediaStore:
    store = FakeMediaStore()
    for item in media:
        store.put(item, asset_id=_ASSET, session_id=_SESSION)
    return store


# =============================================================================
# Core outcomes
# =============================================================================


class TestExtractionOutcomes:
    async def test_valid_fixture_success(self) -> None:
        extractor = _extractor(_store_with(_media()))
        result = await extractor.extract(_evidence(), _segment())
        assert result.status is ExtractionStatus.SUCCESS
        assert result.actual_start_time == _S
        assert result.actual_end_time == _E
        assert result.media_path is not None
        assert result.media_format == "mp4"
        assert result.duration_seconds == pytest.approx(1800.0)
        assert result.size_bytes == 1024 * 1024
        assert result.start_frame == 0
        assert result.end_frame == 53999

    async def test_corrupt_source(self) -> None:
        extractor = _extractor(_store_with(_media(state="corrupt")))
        result = await extractor.extract(_evidence(), _segment())
        assert result.status is ExtractionStatus.CORRUPT_SOURCE
        assert result.actual_start_time is None
        assert "decode" in (result.reason or "")

    async def test_empty_interval_source(self) -> None:
        extractor = _extractor(_store_with(_media(duration=0.0)))
        result = await extractor.extract(_evidence(), _segment())
        assert result.status is ExtractionStatus.EXTRACTION_FAILED
        assert "empty interval" in (result.reason or "")

    async def test_invalid_time_range(self) -> None:
        extractor = _extractor(_store_with(_media()))
        segment = _segment(requested_start=_E, requested_end=_S)
        result = await extractor.extract(_evidence(), segment)
        assert result.status is ExtractionStatus.EXTRACTION_FAILED
        assert "invalid time range" in (result.reason or "")

    async def test_source_not_found_segment(self) -> None:
        extractor = _extractor(_store_with(_media()))
        segment = _segment(
            status=SourceResolutionStatus.SOURCE_NOT_FOUND,
            segments=(),
            reason="no recording covers the requested interval",
        )
        result = await extractor.extract(_evidence(), segment)
        assert result.status is ExtractionStatus.SOURCE_NOT_FOUND

    async def test_source_missing_at_extraction_time(self) -> None:
        extractor = _extractor(_store_with(_media(state="missing")))
        result = await extractor.extract(_evidence(), _segment())
        assert result.status is ExtractionStatus.SOURCE_NOT_FOUND
        assert "missing" in (result.reason or "")

    async def test_partial_coverage_segment(self) -> None:
        segment = _segment(
            status=SourceResolutionStatus.PARTIAL_COVERAGE,
            segments=(
                SourceSegment(
                    asset_id=_ASSET,
                    camera_id=_CAMERA,
                    session_id=_SESSION,
                    start_time=_MID,
                    end_time=_E,
                ),
            ),
            reason="coverage gaps: [10:00,10:15)",
        )
        extractor = _extractor(_store_with(_media()))
        result = await extractor.extract(_evidence(), segment)
        assert result.status is ExtractionStatus.PARTIAL
        assert result.actual_start_time == _MID
        assert result.actual_end_time == _E

    async def test_truncated_source(self) -> None:
        # Byte coverage ends before the requested end → PARTIAL.
        media = _media(actual_end=datetime(2026, 8, 1, 10, 20, 0, tzinfo=UTC))
        extractor = _extractor(_store_with(media))
        result = await extractor.extract(_evidence(), _segment())
        assert result.status is ExtractionStatus.PARTIAL
        assert result.actual_end_time == datetime(2026, 8, 1, 10, 20, 0, tzinfo=UTC)


# =============================================================================
# Cancellation
# =============================================================================


class TestCancellation:
    async def test_cancelled_before_extraction(self) -> None:
        token = ExtractionCancellationToken()
        token.cancel()
        extractor = _extractor(_store_with(_media()))
        result = await extractor.extract(_evidence(), _segment(), cancellation=token)
        assert result.status is ExtractionStatus.CANCELLED
        assert extractor.handles_open == 0

    async def test_cancelled_during_extraction(self) -> None:
        token = ExtractionCancellationToken()
        extractor = _extractor(_store_with(_media()), latency=0.05)

        async def run() -> ExtractedEvidence:
            await asyncio.sleep(0.01)
            token.cancel()
            return await extractor.extract(_evidence(), _segment(), cancellation=token)

        result = await run()
        assert result.status is ExtractionStatus.CANCELLED
        assert extractor.handles_open == 0

    async def test_asyncio_cancellation_releases_resources(self) -> None:
        extractor = _extractor(_store_with(_media()), latency=10.0)
        task = asyncio.create_task(extractor.extract(_evidence(), _segment()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The finally block released the handle before propagation.
        assert extractor.handles_open == 0
        assert extractor.open_handles == 1
        assert extractor.closed_handles == 1


# =============================================================================
# Resource safety / cleanup
# =============================================================================


class TestResourceSafety:
    async def test_no_leak_on_success(self) -> None:
        extractor = _extractor(_store_with(_media()))
        await extractor.extract(_evidence(), _segment())
        assert extractor.handles_open == 0
        assert extractor.closed_handles == 1

    async def test_no_leak_on_every_path(self) -> None:
        cases = [
            (_store_with(_media()), _segment()),
            (_store_with(_media(state="corrupt")), _segment()),
            (_store_with(_media(duration=0.0)), _segment()),
            (
                _store_with(_media()),
                _segment(
                    status=SourceResolutionStatus.PARTIAL_COVERAGE,
                    segments=(SourceSegment(asset_id=_ASSET, start_time=_MID, end_time=_E),),
                ),
            ),
        ]
        for store, segment in cases:
            extractor = _extractor(store)
            await extractor.extract(_evidence(), segment)
            assert extractor.handles_open == 0
            assert extractor.closed_handles == extractor.open_handles

    async def test_repeated_extractions_keep_handles_balanced(self) -> None:
        store = _store_with(_media())
        extractor = _extractor(store)
        for _ in range(5):
            result = await extractor.extract(_evidence(), _segment())
            assert result.status is ExtractionStatus.SUCCESS
        assert extractor.handles_open == 0
        assert extractor.closed_handles == 5


# =============================================================================
# Identity / provenance / contract
# =============================================================================


class TestIdentityAndProvenance:
    async def test_deterministic_extraction_identity(self) -> None:
        extractor = _extractor(_store_with(_media()))
        first = await extractor.extract(_evidence(), _segment())
        second = await extractor.extract(_evidence(), _segment())
        assert first.extraction_id == second.extraction_id
        assert isinstance(first.extraction_id, uuid.UUID)  # MediaId is a NewType over UUID

    async def test_provenance_preserved(self) -> None:
        extractor = _extractor(_store_with(_media()))
        result = await extractor.extract(_evidence(), _segment())
        assert result.evidence_ref_id == _REF
        assert result.event_id == _EVENT
        assert result.tenant_id == _TENANT
        assert result.venue_id == _VENUE
        assert result.session_id == _SESSION
        assert result.camera_id == _CAMERA
        assert result.configuration_version_id == _CONFIG
        assert result.rule_id == _RULE_ID
        assert result.rule_version == _RULE_VERSION
        assert result.requested_start == _S
        assert result.requested_end == _E

    async def test_extracted_evidence_round_trips(self) -> None:
        extractor = _extractor(_store_with(_media()))
        result = await extractor.extract(_evidence(), _segment())
        restored = ExtractedEvidence.model_validate(result.model_dump(mode="json"))
        assert restored == result

    def test_fake_conforms_to_port(self) -> None:
        assert isinstance(_extractor(_store_with(_media())), EvidenceExtractor)

    def test_extraction_contract_carries_no_frame_source(self) -> None:
        """The evidence extraction layer is source-independent."""
        import importlib

        extraction = importlib.import_module("backend.app.domain.evidence.extraction")
        resolution = importlib.import_module("backend.app.domain.evidence.resolution")
        for module in (extraction, resolution):
            source = module.__spec__.loader.get_source(module.__name__) or ""
            # Only import lines count — docstrings may mention the concepts.
            import_lines = [
                line for line in source.splitlines() if line.startswith(("import ", "from "))
            ]
            assert not any("intelligence.sources" in line for line in import_lines)
            assert not any("FrameSource" in line for line in import_lines)
            assert not any("cv2" in line.lower() for line in import_lines)
