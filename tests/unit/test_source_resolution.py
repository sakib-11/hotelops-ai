"""Tests for Task 17.4 — source asset / video session resolution.

Covers the canonical resolution policy:

- exact source: RESOLVED with the covering recording;
- missing source / wrong camera: SOURCE_NOT_FOUND — never a silent
  substitution;
- wrong tenant / wrong venue: AUTHORIZATION_FAILURE;
- overlapping recordings: deterministic disjoint segments, earliest-start
  recording owns the overlap;
- partial recording: PARTIAL_COVERAGE with deterministic gap listing;
- no recording / expired recording: SOURCE_NOT_FOUND;
- historical session: resolved to its historical asset — never "latest".

Every outcome preserves the evidence provenance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceRecordingCandidate,
    SourceResolutionStatus,
    SourceResolver,
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

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT_A = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_TENANT_B = TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001"))
_VENUE_A = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_VENUE_B = VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_SESSION_B = VideoSessionId(uuid.UUID("93000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CAMERA_B = CameraId(uuid.UUID("94000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_ASSET_B = VideoAssetId(uuid.UUID("55000000-0000-0000-0000-000000000002"))
_ASSET_OLD = VideoAssetId(uuid.UUID("56000000-0000-0000-0000-000000000003"))
_ASSET_NEW = VideoAssetId(uuid.UUID("57000000-0000-0000-0000-000000000004"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("80000000-0000-0000-0000-000000000001"))

_RULE_ID = RuleId("dwell_threshold")
_RULE_VERSION = RuleVersion("v1")

_S = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_MID = datetime(2026, 8, 1, 10, 15, 0, tzinfo=UTC)
_MID2 = datetime(2026, 8, 1, 10, 20, 0, tzinfo=UTC)
_E = datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)

_RESOLVER = SourceResolver()


def _evidence(**overrides: object) -> EvidenceRef:
    """A canonical EvidenceRef (Task 17.2 contract) with fixed IDs."""
    values: dict[str, object] = {
        "ref_id": _REF,
        "ref_type": EvidenceType.VIDEO_CLIP,
        "ref_uri": f"s3://evidence/{_TENANT_A}/{_SESSION}/rule/dwell_threshold",
        "event_id": _EVENT,
        "event_time": _E,
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "video_session_id": _SESSION,
        "camera_id": _CAMERA,
        "start_time": _S,
        "end_time": _E,
        "configuration_version_id": _CONFIG_V1,
        "rule_id": _RULE_ID,
        "rule_version": _RULE_VERSION,
    }
    values.update(overrides)
    return EvidenceRef(**values)


def _recording(
    *,
    asset_id: VideoAssetId = _ASSET,
    tenant_id: TenantId = _TENANT_A,
    venue_id: VenueId = _VENUE_A,
    camera_id: CameraId | None = _CAMERA,
    session_id: VideoSessionId | None = _SESSION,
    start_time: datetime,
    end_time: datetime,
    available: bool = True,
) -> SourceRecordingCandidate:
    return SourceRecordingCandidate(
        asset_id=asset_id,
        tenant_id=tenant_id,
        venue_id=venue_id,
        camera_id=camera_id,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        available=available,
    )


def _resolve(
    evidence: EvidenceRef,
    *recordings: SourceRecordingCandidate,
) -> ResolvedSourceSegment:
    return _RESOLVER.resolve(evidence, list(recordings))


# =============================================================================
# Core outcomes
# =============================================================================


class TestResolved:
    def test_exact_source(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert len(result.segments) == 1
        seg = result.segments[0]
        assert seg.asset_id == _ASSET
        assert seg.start_time == _S
        assert seg.end_time == _E
        assert seg.camera_id == _CAMERA

    def test_recording_wider_than_request(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(
                start_time=datetime(2026, 8, 1, 9, 50, 0, tzinfo=UTC),
                end_time=datetime(2026, 8, 1, 10, 40, 0, tzinfo=UTC),
            ),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert result.segments[0].start_time == _S
        assert result.segments[0].end_time == _E

    def test_historical_session_resolves_to_historical_asset(self) -> None:
        """Never 'latest' — a historical interval resolves to its asset."""
        old_s = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        old_e = datetime(2026, 6, 1, 10, 30, 0, tzinfo=UTC)
        evidence = _evidence(
            video_asset_id=_ASSET_OLD,
            start_time=old_s,
            end_time=old_e,
            event_time=old_e,
        )
        result = _resolve(
            evidence,
            # A NEWER recording of the same camera exists — it must NOT be used.
            _recording(asset_id=_ASSET_OLD, start_time=old_s, end_time=old_e),
            _recording(
                asset_id=_ASSET_NEW,
                start_time=datetime(2026, 8, 2, 10, 0, 0, tzinfo=UTC),
                end_time=datetime(2026, 8, 2, 10, 30, 0, tzinfo=UTC),
            ),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert result.segments[0].asset_id == _ASSET_OLD
        assert result.segments[0].start_time == old_s


class TestSourceNotFound:
    def test_missing_source(self) -> None:
        # Evidence pins an asset that no recording provides.
        result = _resolve(
            _evidence(video_asset_id=_ASSET),
            _recording(
                asset_id=_ASSET_B,
                start_time=_S,
                end_time=_E,
            ),
        )
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND
        assert result.reason is not None

    def test_no_source_identity(self) -> None:
        # An OBJECT_STORAGE ref may legitimately carry no source identity
        # (the contract requires source only for media-backed types) — but
        # it cannot be resolved to a recording.
        evidence = _evidence(
            ref_type=EvidenceType.OBJECT_STORAGE,
            video_asset_id=None,
            camera_id=None,
            video_session_id=None,
        )
        result = _resolve(evidence, _recording(start_time=_S, end_time=_E))
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND
        assert "no source identity" in (result.reason or "")

    def test_wrong_camera_no_substitution(self) -> None:
        result = _resolve(
            _evidence(camera_id=_CAMERA),
            _recording(camera_id=_CAMERA_B, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND
        assert result.segments == ()

    def test_no_recording_covers_interval(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(
                start_time=datetime(2026, 8, 1, 11, 0, 0, tzinfo=UTC),
                end_time=datetime(2026, 8, 1, 11, 30, 0, tzinfo=UTC),
            ),
        )
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND

    def test_no_recordings_at_all(self) -> None:
        result = _resolve(_evidence())
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND

    def test_expired_recording(self) -> None:
        """An expired recording is excluded — never silently replaced."""
        result = _resolve(
            _evidence(),
            _recording(start_time=_S, end_time=_E, available=False),
        )
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND
        assert "expired" in (result.reason or "")

    def test_zero_width_instant_not_covered(self) -> None:
        # The instant is before every available recording — not covered.
        evidence = _evidence(start_time=_MID, end_time=_MID)
        result = _resolve(
            evidence,
            _recording(
                start_time=datetime(2026, 8, 1, 10, 20, 0, tzinfo=UTC),
                end_time=datetime(2026, 8, 1, 10, 45, 0, tzinfo=UTC),
            ),
        )
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND


class TestAuthorization:
    def test_wrong_tenant(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(tenant_id=_TENANT_B, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.AUTHORIZATION_FAILURE
        assert "tenant" in (result.reason or "")

    def test_wrong_venue(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(venue_id=_VENUE_B, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.AUTHORIZATION_FAILURE
        assert "venue" in (result.reason or "")

    def test_mixed_scope_candidates_fail(self) -> None:
        # A single cross-tenant candidate in an otherwise valid set fails.
        result = _resolve(
            _evidence(),
            _recording(start_time=_S, end_time=_E),
            _recording(asset_id=_ASSET_B, tenant_id=_TENANT_B, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.AUTHORIZATION_FAILURE

    def test_evidence_without_scope_fails(self) -> None:
        evidence = _evidence(tenant_id=None, venue_id=None)
        result = _resolve(evidence, _recording(start_time=_S, end_time=_E))
        assert result.status is SourceResolutionStatus.AUTHORIZATION_FAILURE


# =============================================================================
# Overlap / partial coverage
# =============================================================================


class TestOverlapAndPartial:
    def test_overlapping_recordings_earliest_start_owns_overlap(self) -> None:
        """A[10:00-10:20] + B[10:10-10:30] → disjoint A[10:00-10:20], B[10:20-10:30]."""
        result = _resolve(
            _evidence(),
            _recording(asset_id=_ASSET, start_time=_S, end_time=_MID2),
            _recording(asset_id=_ASSET_B, start_time=_MID, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert len(result.segments) == 2
        first, second = result.segments
        assert (first.asset_id, first.start_time, first.end_time) == (_ASSET, _S, _MID2)
        assert (second.asset_id, second.start_time, second.end_time) == (
            _ASSET_B,
            _MID2,
            _E,
        )

    def test_tie_break_by_asset_id(self) -> None:
        """Identical start windows: the smaller asset_id owns the coverage."""
        result = _resolve(
            _evidence(),
            _recording(asset_id=_ASSET_B, start_time=_S, end_time=_E),
            _recording(asset_id=_ASSET, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert len(result.segments) == 1
        assert result.segments[0].asset_id == _ASSET  # "5..." < "55..."

    def test_partial_recording(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(start_time=_MID, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.PARTIAL_COVERAGE
        assert len(result.segments) == 1
        assert result.segments[0].start_time == _MID
        assert "10:00:00" in (result.reason or "")  # leading gap listed

    def test_internal_gap(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(start_time=_S, end_time=_MID),
            _recording(
                start_time=datetime(2026, 8, 1, 10, 20, 0, tzinfo=UTC),
                end_time=_E,
            ),
        )
        assert result.status is SourceResolutionStatus.PARTIAL_COVERAGE
        reason = result.reason or ""
        assert "10:15:00" in reason and "10:20:00" in reason  # internal gap listed

    def test_contiguous_assets_cover_fully(self) -> None:
        """A[10:00-10:15] + B[10:15-10:30] (touching) → RESOLVED, two segments."""
        result = _resolve(
            _evidence(),
            _recording(asset_id=_ASSET, start_time=_S, end_time=_MID),
            _recording(asset_id=_ASSET_B, start_time=_MID, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert len(result.segments) == 2
        assert result.segments[0].end_time == _MID
        assert result.segments[1].start_time == _MID

    def test_zero_width_instant_covered(self) -> None:
        evidence = _evidence(start_time=_MID, end_time=_MID)
        result = _resolve(
            evidence,
            _recording(start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert result.segments[0].start_time == _MID
        assert result.segments[0].end_time == _MID


# =============================================================================
# Identity matching + provenance
# =============================================================================


class TestIdentityMatching:
    def test_match_by_asset_id(self) -> None:
        evidence = _evidence(video_asset_id=_ASSET, camera_id=None)
        result = _resolve(
            evidence,
            _recording(asset_id=_ASSET, camera_id=None, start_time=_S, end_time=_E),
            _recording(asset_id=_ASSET_B, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert result.segments[0].asset_id == _ASSET

    def test_match_by_session_id(self) -> None:
        evidence = _evidence(video_session_id=_SESSION, camera_id=None, video_asset_id=None)
        result = _resolve(
            evidence,
            _recording(session_id=_SESSION, camera_id=None, start_time=_S, end_time=_E),
            _recording(session_id=_SESSION_B, camera_id=None, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.RESOLVED
        assert result.segments[0].session_id == _SESSION

    def test_asset_camera_mismatch_excluded(self) -> None:
        # Evidence pins asset A + camera A; a candidate with asset A but
        # camera B does NOT match (all identities must agree).
        evidence = _evidence(video_asset_id=_ASSET, camera_id=_CAMERA)
        result = _resolve(
            evidence,
            _recording(asset_id=_ASSET, camera_id=_CAMERA_B, start_time=_S, end_time=_E),
        )
        assert result.status is SourceResolutionStatus.SOURCE_NOT_FOUND


class TestProvenance:
    def _assert_provenance(self, result: ResolvedSourceSegment) -> None:
        assert result.evidence_ref_id == _REF
        assert result.event_id == _EVENT
        assert result.tenant_id == _TENANT_A
        assert result.venue_id == _VENUE_A
        assert result.video_session_id == _SESSION
        assert result.configuration_version_id == _CONFIG_V1
        assert result.rule_id == _RULE_ID
        assert result.rule_version == _RULE_VERSION
        assert result.requested_start == _S
        assert result.requested_end == _E

    def test_provenance_preserved_on_resolved(self) -> None:
        self._assert_provenance(_resolve(_evidence(), _recording(start_time=_S, end_time=_E)))

    def test_provenance_preserved_on_partial(self) -> None:
        self._assert_provenance(_resolve(_evidence(), _recording(start_time=_MID, end_time=_E)))

    def test_provenance_preserved_on_not_found(self) -> None:
        self._assert_provenance(_resolve(_evidence()))

    def test_provenance_preserved_on_authorization_failure(self) -> None:
        self._assert_provenance(
            _resolve(_evidence(), _recording(tenant_id=_TENANT_B, start_time=_S, end_time=_E))
        )

    def test_evidence_never_modified(self) -> None:
        evidence = _evidence()
        snapshot = evidence.model_dump(mode="json")
        _resolve(evidence, _recording(start_time=_S, end_time=_E))
        assert evidence.model_dump(mode="json") == snapshot

    def test_resolution_round_trips(self) -> None:
        result = _resolve(
            _evidence(),
            _recording(asset_id=_ASSET, start_time=_S, end_time=_MID),
            _recording(asset_id=_ASSET_B, start_time=_MID, end_time=_E),
        )
        restored = ResolvedSourceSegment.model_validate(result.model_dump(mode="json"))
        assert restored == result
        assert isinstance(restored.segments[0], SourceSegment)


class TestContractValidation:
    def test_candidate_invalid_window_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceRecordingCandidate(
                asset_id=_ASSET,
                tenant_id=_TENANT_A,
                venue_id=_VENUE_A,
                start_time=_E,
                end_time=_S,
            )
