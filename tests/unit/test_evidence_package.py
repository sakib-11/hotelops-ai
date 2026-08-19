"""Tests for Task 17.7 — the complete auditable EvidencePackage.

The package composes the canonical ``EvidenceRef`` (Task 17.2),
``ResolvedSourceSegment`` (Task 17.4) and ``ExtractedEvidence``
(Task 17.5/17.6) into one aggregate root whose provenance can never be
lost or contradicted. It exposes the ordered audit trail:

    Material Event
        → EvidenceRef
        → Source Asset
        → Video Session
        → Camera
        → Frame/Time
        → Processing Versions
        → Configuration Version
        → Rule Version
        → Checksum
        → Stored Evidence

Covered:

- the required linkage tests: event → evidence, event → source,
  evidence → session, evidence → camera, evidence → configuration,
  evidence → detector, evidence → tracker, evidence → rule,
  evidence → checksum;
- the complete provenance-chain test (all hops present and correct);
- provenance preservation: every provenance field survives finalize;
- no-provenance-loss: finalize REFUSES a completed extraction without a
  storage reference or checksum;
- cross-model consistency: finalize REFUSES contradicting tenant, venue,
  session, camera, configuration, rule, event, or interval across the
  chain;
- determinism (Task 7): identical inputs → identical package identity
  and chain (replay), different extraction → different package;
- scope isolation: tenant/venue identity cannot be swapped;
- round-trip: the package serializes and validates through the contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.domain.evidence.extraction import (
    ExtractedEvidence,
    ExtractionStatus,
)
from backend.app.domain.evidence.package import (
    EvidencePackage,
    EvidencePackageBuilder,
    ProvenanceHop,
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
    MediaId,
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
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("80000000-0000-0000-0000-000000000001"))
_EXTRACTION = MediaId(uuid.UUID("81000000-0000-0000-0000-000000000001"))

_RULE_ID = RuleId("dwell_threshold")
_RULE_VERSION = RuleVersion("v1")
_DETECTOR = "8.1.0"
_TRACKER = "1.3.2"
_CHECKSUM = "a" * 64

_S = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_MID = datetime(2026, 8, 1, 10, 15, 0, tzinfo=UTC)
_E = datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)

_BUILDER = EvidencePackageBuilder()


def _evidence_ref(**overrides: object) -> EvidenceRef:
    values: dict[str, object] = {
        "ref_id": _REF,
        "ref_type": EvidenceType.VIDEO_CLIP,
        "ref_uri": f"tenants/{_TENANT_A}/venues/{_VENUE_A}/evidence/2026/08/01/{_REF}.mp4",
        "event_id": _EVENT,
        "event_time": _E,
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "video_asset_id": _ASSET,
        "video_session_id": _SESSION,
        "camera_id": _CAMERA,
        "start_time": _S,
        "end_time": _E,
        "start_frame": 0,
        "end_frame": 1799,
        "configuration_version_id": _CONFIG_V1,
        "detector_version": _DETECTOR,
        "tracker_version": _TRACKER,
        "rule_id": _RULE_ID,
        "rule_version": _RULE_VERSION,
        "checksum": _CHECKSUM,
    }
    values.update(overrides)
    return EvidenceRef(**values)


def _resolved_source(**overrides: object) -> ResolvedSourceSegment:
    values: dict[str, object] = {
        "status": SourceResolutionStatus.RESOLVED,
        "evidence_ref_id": _REF,
        "event_id": _EVENT,
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "camera_id": _CAMERA,
        "video_session_id": _SESSION,
        "configuration_version_id": _CONFIG_V1,
        "rule_id": _RULE_ID,
        "rule_version": _RULE_VERSION,
        "requested_start": _S,
        "requested_end": _E,
        "segments": (
            SourceSegment(
                asset_id=_ASSET,
                camera_id=_CAMERA,
                session_id=_SESSION,
                start_time=_S,
                end_time=_E,
            ),
        ),
    }
    values.update(overrides)
    return ResolvedSourceSegment(**values)


def _extraction(**overrides: object) -> ExtractedEvidence:
    values: dict[str, object] = {
        "extraction_id": _EXTRACTION,
        "status": ExtractionStatus.SUCCESS,
        "evidence_ref_id": _REF,
        "event_id": _EVENT,
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "session_id": _SESSION,
        "camera_id": _CAMERA,
        "configuration_version_id": _CONFIG_V1,
        "rule_id": _RULE_ID,
        "rule_version": _RULE_VERSION,
        "requested_start": _S,
        "requested_end": _E,
        "actual_start_time": _S,
        "actual_end_time": _E,
        "start_frame": 0,
        "end_frame": 1799,
        "media_path": f"tenants/{_TENANT_A}/venues/{_VENUE_A}/evidence/2026/08/01/{_EXTRACTION}.mp4",
        "media_format": "mp4",
        "duration_seconds": 1800.0,
        "size_bytes": 123456,
        "metadata": {"checksum_sha256": _CHECKSUM, "encoder": "libx264"},
    }
    values.update(overrides)
    return ExtractedEvidence(**values)


def _finalize(
    *,
    evidence_ref: EvidenceRef | None = None,
    resolved_source: ResolvedSourceSegment | None = None,
    extraction: ExtractedEvidence | None = None,
    created_at: datetime | None = None,
) -> EvidencePackage:
    return _BUILDER.finalize(
        evidence_ref=evidence_ref or _evidence_ref(),
        resolved_source=resolved_source or _resolved_source(),
        extraction=extraction or _extraction(),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# The required linkage tests
# ---------------------------------------------------------------------------


def test_event_to_evidence_link() -> None:
    pkg = _finalize()
    assert pkg.event_id == _EVENT
    assert pkg.evidence_id == _REF
    assert pkg.chain_value("event -> evidence") == str(_EVENT)


def test_event_to_source_link() -> None:
    pkg = _finalize()
    assert pkg.source_asset_id == _ASSET
    assert pkg.chain_value("evidence -> source") == str(_ASSET)


def test_evidence_to_session_link() -> None:
    pkg = _finalize()
    assert pkg.video_session_id == _SESSION
    assert pkg.chain_value("source -> session") == str(_SESSION)


def test_evidence_to_camera_link() -> None:
    pkg = _finalize()
    assert pkg.camera_id == _CAMERA
    assert pkg.chain_value("session -> camera") == str(_CAMERA)


def test_evidence_to_configuration_link() -> None:
    pkg = _finalize()
    assert pkg.configuration_version_id == _CONFIG_V1
    assert pkg.chain_value("processing -> configuration") == str(_CONFIG_V1)


def test_evidence_to_detector_link() -> None:
    pkg = _finalize()
    assert pkg.detector_version == _DETECTOR
    assert pkg.chain_value("time -> detector_version") == _DETECTOR


def test_evidence_to_tracker_link() -> None:
    pkg = _finalize()
    assert pkg.tracker_version == _TRACKER
    assert pkg.chain_value("time -> tracker_version") == _TRACKER


def test_evidence_to_rule_link() -> None:
    pkg = _finalize()
    assert pkg.rule_id == _RULE_ID
    assert pkg.rule_version == _RULE_VERSION
    assert pkg.chain_value("configuration -> rule") == f"{_RULE_ID}:{_RULE_VERSION}"


def test_evidence_to_checksum_link() -> None:
    pkg = _finalize()
    assert pkg.checksum == _CHECKSUM
    assert pkg.chain_value("rule -> checksum") == _CHECKSUM


# ---------------------------------------------------------------------------
# The complete provenance chain
# ---------------------------------------------------------------------------


def test_complete_provenance_chain() -> None:
    pkg = _finalize()
    chain = pkg.provenance_chain()

    expected_links = [
        "event -> evidence",
        "evidence -> source",
        "source -> session",
        "session -> camera",
        "camera -> requested_time",
        "camera -> frame_range",
        "time -> detector_version",
        "time -> tracker_version",
        "processing -> configuration",
        "configuration -> rule",
        "rule -> checksum",
        "checksum -> stored_evidence",
    ]
    assert [hop.link for hop in chain] == expected_links
    # Positions are the deterministic 1-based hop indices.
    assert [hop.position for hop in chain] == list(range(1, len(chain) + 1))

    # Every hop's value is present and canonical.
    assert chain[0].value == str(_EVENT)  # event -> evidence
    assert chain[1].value == str(_ASSET)  # evidence -> source
    assert chain[2].value == str(_SESSION)  # source -> session
    assert chain[3].value == str(_CAMERA)  # session -> camera
    assert chain[4].value == f"[{_S.isoformat()},{_E.isoformat()}]"  # requested_time
    assert chain[5].value == "[0,1799]"  # frame_range
    assert chain[6].value == _DETECTOR
    assert chain[7].value == _TRACKER
    assert chain[8].value == str(_CONFIG_V1)
    assert chain[9].value == f"{_RULE_ID}:{_RULE_VERSION}"
    assert chain[10].value == _CHECKSUM
    assert chain[11].value == pkg.storage_reference  # stored_evidence

    # Storage reference is the actual artifact object key (not a guess).
    assert str(_EXTRACTION) in (pkg.storage_reference or "")
    assert pkg.media_format == "mp4"
    assert pkg.duration_seconds == pytest.approx(1800.0)
    assert pkg.size_bytes == 123456


def test_chain_is_deterministic_across_replay() -> None:
    first = _finalize()
    second = _finalize()
    assert [hop for hop in first.provenance_chain()] == [hop for hop in second.provenance_chain()]
    assert first.provenance_chain() == second.provenance_chain()


# ---------------------------------------------------------------------------
# Provenance preservation — nothing is lost through finalize
# ---------------------------------------------------------------------------


def test_all_provenance_fields_preserved() -> None:
    pkg = _finalize()
    assert pkg.event_id == _EVENT
    assert pkg.evidence_id == _REF
    assert pkg.tenant_id == _TENANT_A
    assert pkg.venue_id == _VENUE_A
    assert pkg.source_asset_id == _ASSET
    assert pkg.camera_id == _CAMERA
    assert pkg.video_session_id == _SESSION
    assert pkg.configuration_version_id == _CONFIG_V1
    assert pkg.detector_version == _DETECTOR
    assert pkg.tracker_version == _TRACKER
    assert pkg.rule_id == _RULE_ID
    assert pkg.rule_version == _RULE_VERSION
    assert pkg.checksum == _CHECKSUM
    assert pkg.storage_reference is not None
    assert pkg.extraction_status is ExtractionStatus.SUCCESS
    assert pkg.requested_start == _S
    assert pkg.requested_end == _E
    assert pkg.actual_start_time == _S
    assert pkg.actual_end_time == _E
    assert pkg.start_frame == 0
    assert pkg.end_frame == 1799
    assert pkg.media_format == "mp4"
    assert pkg.duration_seconds == pytest.approx(1800.0)
    assert pkg.size_bytes == 123456
    # Provenance metadata (extraction-level) survives.
    assert pkg.extraction.metadata["encoder"] == "libx264"


def test_no_provenance_loss_completed_extraction_requires_storage() -> None:
    # A SUCCESS extraction without its ACTUAL storage reference is a
    # provenance loss — finalize refuses even though the request ref_uri
    # exists (the request provenance is not the stored artifact).
    extraction = _extraction(media_path=None)
    with pytest.raises(ValueError, match="storage reference"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=extraction,
        )


def test_no_provenance_loss_completed_extraction_requires_checksum() -> None:
    # A SUCCESS extraction without its ACTUAL integrity checksum is a
    # provenance loss — the request's checksum is not the artifact proof.
    extraction = _extraction(metadata={"encoder": "libx264"})
    with pytest.raises(ValueError, match="checksum"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=extraction,
        )


def test_extraction_checksum_wins_over_request_checksum() -> None:
    # The ACTUAL artifact checksum (from extraction) is authoritative; the
    # request's checksum is the fallback.
    actual = "b" * 64
    extraction = _extraction(metadata={"checksum_sha256": actual})
    pkg = _finalize(extraction=extraction)
    assert pkg.checksum == actual
    assert pkg.chain_value("rule -> checksum") == actual


# ---------------------------------------------------------------------------
# Cross-model consistency — finalized evidence can never contradict itself
# ---------------------------------------------------------------------------


def test_finalize_rejects_inconsistent_event() -> None:
    with pytest.raises(ValueError, match="event id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(
                event_id=EventId(uuid.UUID("99999999-0000-0000-0000-000000000001"))
            ),
        )


def test_finalize_rejects_inconsistent_ref() -> None:
    with pytest.raises(ValueError, match="evidence ref id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(
                evidence_ref_id=EvidenceId(uuid.UUID("99999999-0000-0000-0000-000000000002"))
            ),
        )


def test_finalize_rejects_inconsistent_tenant() -> None:
    with pytest.raises(ValueError, match="tenant id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(tenant_id=_TENANT_B),
        )


def test_finalize_rejects_inconsistent_venue() -> None:
    with pytest.raises(ValueError, match="venue id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(venue_id=_VENUE_B),
            extraction=_extraction(),
        )


def test_finalize_rejects_inconsistent_session() -> None:
    with pytest.raises(ValueError, match="session id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(
                session_id=VideoSessionId(uuid.UUID("99999999-0000-0000-0000-000000000003"))
            ),
        )


def test_finalize_rejects_inconsistent_camera() -> None:
    with pytest.raises(ValueError, match="camera id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(
                camera_id=CameraId(uuid.UUID("99999999-0000-0000-0000-000000000004"))
            ),
            extraction=_extraction(),
        )


def test_finalize_rejects_inconsistent_configuration() -> None:
    with pytest.raises(ValueError, match="configuration version id"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(
                configuration_version_id=ConfigurationVersionId(
                    uuid.UUID("99999999-0000-0000-0000-000000000005")
                )
            ),
        )


def test_finalize_rejects_inconsistent_rule() -> None:
    with pytest.raises(ValueError, match="rule version"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(rule_version=RuleVersion("v2")),
        )


def test_finalize_rejects_inconsistent_requested_interval() -> None:
    with pytest.raises(ValueError, match="requested start"):
        _BUILDER.finalize(
            evidence_ref=_evidence_ref(),
            resolved_source=_resolved_source(),
            extraction=_extraction(requested_start=_MID),
        )


# ---------------------------------------------------------------------------
# Determinism (Task 7 idempotency)
# ---------------------------------------------------------------------------


def test_replay_produces_identical_package() -> None:
    first = _finalize()
    second = _finalize()
    assert first.package_id == second.package_id
    assert first.evidence_ref == second.evidence_ref
    assert first.resolved_source == second.resolved_source
    assert first.extraction == second.extraction
    assert first == second


def test_different_extraction_produces_different_package() -> None:
    first = _finalize()
    other = _extraction(
        extraction_id=MediaId(uuid.UUID("82000000-0000-0000-0000-000000000001")),
        status=ExtractionStatus.PARTIAL,
        actual_end_time=_MID,
    )
    second = _BUILDER.finalize(
        evidence_ref=_evidence_ref(),
        resolved_source=_resolved_source(),
        extraction=other,
    )
    assert first.package_id != second.package_id
    assert second.extraction_status is ExtractionStatus.PARTIAL


def test_package_id_is_a_stable_uuid5() -> None:
    pkg = _finalize()
    parsed = uuid.UUID(str(pkg.package_id))
    assert parsed.version == 5


# ---------------------------------------------------------------------------
# Tenant / venue isolation — identity cannot be swapped through the package
# ---------------------------------------------------------------------------


def test_tenant_identity_is_preserved_and_scoped() -> None:
    pkg = _finalize()
    assert pkg.tenant_id == _TENANT_A
    # The chain never leaks another tenant's identity.
    assert _TENANT_B not in [hop.value for hop in pkg.provenance_chain()]


def test_venue_identity_is_preserved_and_scoped() -> None:
    pkg = _finalize()
    assert pkg.venue_id == _VENUE_A
    assert _VENUE_B not in [hop.value for hop in pkg.provenance_chain()]


# ---------------------------------------------------------------------------
# Round-trip / contract validation
# ---------------------------------------------------------------------------


def test_package_round_trips_through_contract() -> None:
    pkg = _finalize()
    rt = EvidencePackage.model_validate(pkg.model_dump())
    assert rt == pkg
    assert rt.provenance_chain() == pkg.provenance_chain()


def test_package_rejects_unknown_fields() -> None:
    pkg = _finalize()
    with pytest.raises(ValidationError):
        EvidencePackage.model_validate({**pkg.model_dump(), "unexpected": True})


def test_provenance_hop_is_a_typed_audit_record() -> None:
    hop = ProvenanceHop(position=1, link="event -> evidence", value="abc")
    assert str(hop) == "1. event -> evidence: abc"


def test_chain_value_returns_none_for_unknown_link() -> None:
    pkg = _finalize()
    assert pkg.chain_value("does -> not -> exist") is None


# ---------------------------------------------------------------------------
# Partial / failed extraction still preserves what exists
# ---------------------------------------------------------------------------


def test_partial_extraction_preserves_actual_window() -> None:
    partial = _extraction(
        status=ExtractionStatus.PARTIAL,
        actual_end_time=_MID,
        end_frame=899,
        media_path=f"tenants/{_TENANT_A}/venues/{_VENUE_A}/evidence/2026/08/01/{_EXTRACTION}.mp4",
    )
    pkg = _BUILDER.finalize(
        evidence_ref=_evidence_ref(),
        resolved_source=_resolved_source(),
        extraction=partial,
    )
    assert pkg.extraction_status is ExtractionStatus.PARTIAL
    assert pkg.actual_end_time == _MID
    assert pkg.end_frame == 899
    # The requested interval is still preserved.
    assert pkg.requested_start == _S
    assert pkg.requested_end == _E


def test_failed_extraction_preserves_evidence_ref_provenance() -> None:
    failed = _extraction(
        status=ExtractionStatus.EXTRACTION_FAILED,
        media_path=None,
        metadata={},
        actual_start_time=None,
        actual_end_time=None,
    )
    # A FAILED extraction is allowed to lack a storage reference — the
    # evidence ref still carries the request provenance (ref_uri fallback).
    pkg = _BUILDER.finalize(
        evidence_ref=_evidence_ref(),
        resolved_source=_resolved_source(),
        extraction=failed,
    )
    assert pkg.extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert pkg.checksum == _CHECKSUM  # from the evidence ref
    assert pkg.storage_reference == pkg.evidence_ref.ref_uri  # the request ref
    # The full chain is still audit-able (every hop the ref carries).
    chain = pkg.provenance_chain()
    assert chain[0].value == str(_EVENT)
    assert chain[-1].value == pkg.evidence_ref.ref_uri
