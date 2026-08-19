"""Tests for Task 17.14 — enterprise provenance verification.

Proves that a material Task 16 event (dwell_threshold) can be
independently traced to its source and processing provenance:

    EventEnvelope → EvidenceRef → VideoAsset → VideoSession → Camera
        → Event Time → Frame/Clip Range → Checksum → Object Storage
        → Detector Version → Tracker Version → Configuration Version
        → Rule Version → EvidencePackage

Covered:

- FULL CHAIN: every required link is VERIFIED for a canonical
  dwell_threshold event — tenant-scoped, venue-scoped, versioned,
  consistent with the material event, and the package identity is
  reproducible from its composed inputs.
- NEGATIVE MATRIX: substituting another tenant, venue, camera, session,
  configuration version, rule version, or source asset FAILS with a
  typed SUBSTITUTED / MISSING / INCONSISTENT status — never silently
  accepted.
- REPLAY: regenerating the evidence reference from the same event
  produces the same logical provenance, event identity, source identity,
  configuration version, rule version, and (identical media) the same
  checksum + package identity.
- FINAL GATE: an event whose contract requires evidence has no valid
  evidence path → verified=False with the missing links reported.

All fixtures use the REAL canonical contracts with fixed deterministic
IDs so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.app.domain.evidence.extraction import (
    ExtractedEvidence,
    ExtractionStatus,
)
from backend.app.domain.evidence.package import EvidencePackage, EvidencePackageBuilder
from backend.app.domain.evidence.provenance import (
    ProvenanceCheckStatus,
    ProvenanceVerification,
    ProvenanceVerifier,
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
from contracts.events import EventEnvelope, EvidenceRef, EvidenceType
from contracts.rules import DwellThresholdPayload, RuleEventType

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
_ASSET_B = VideoAssetId(uuid.UUID("95000000-0000-0000-0000-000000000001"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_CONFIG_V2 = ConfigurationVersionId(uuid.UUID("96000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("80000000-0000-0000-0000-000000000001"))
_EXTRACTION = MediaId(uuid.UUID("81000000-0000-0000-0000-000000000001"))

_RULE_ID = RuleId("dwell_threshold")
_RULE_VERSION = RuleVersion("v1")
_DETECTOR = "8.1.0"
_TRACKER = "1.3.2"
_CHECKSUM = "a" * 64

_S = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_CROSS = datetime(2026, 8, 1, 10, 5, 0, tzinfo=UTC)
_PRODUCED = datetime(2026, 8, 1, 10, 5, 1, tzinfo=UTC)

_VERIFIER = ProvenanceVerifier()
_BUILDER = EvidencePackageBuilder()


# =============================================================================
# Fixtures — a canonical dwell_threshold material event + evidence chain
# =============================================================================


def _payload(**overrides: object) -> DwellThresholdPayload:
    values: dict[str, object] = {
        "interval_id": _EVENT,
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "session_id": _SESSION,
        "camera_id": _CAMERA,
        "spatial_context_id": "zone-lobby",
        "dwell_start_time": _S,
        "threshold_crossing_time": _CROSS,
        "dwell_duration": 300.0,
        "threshold_seconds": 300.0,
        "configuration_version_id": _CONFIG_V1,
        "rule_id": _RULE_ID,
        "rule_version": _RULE_VERSION,
    }
    values.update(overrides)
    return DwellThresholdPayload(**values)


def _envelope(**overrides: object) -> EventEnvelope[Any]:
    return EventEnvelope(
        event_id=_EVENT,
        event_type=RuleEventType.DWELL_THRESHOLD.value,
        event_time=_CROSS,
        produced_at=_PRODUCED,
        source=f"rule:{_RULE_ID}:{_RULE_VERSION}",
        payload=_payload(),
    )


def _evidence_ref(**overrides: object) -> EvidenceRef:
    values: dict[str, object] = {
        "ref_id": _REF,
        "ref_type": EvidenceType.VIDEO_CLIP,
        "ref_uri": f"s3://evidence/{_TENANT_A}/{_SESSION}/rule/{_RULE_ID}",
        "event_id": _EVENT,
        "event_time": _CROSS,
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "video_asset_id": _ASSET,
        "video_session_id": _SESSION,
        "camera_id": _CAMERA,
        "start_time": _S,
        "end_time": _CROSS,
        "start_frame": 0,
        "end_frame": 899,
        "configuration_version_id": _CONFIG_V1,
        "detector_version": _DETECTOR,
        "tracker_version": _TRACKER,
        "rule_id": _RULE_ID,
        "rule_version": _RULE_VERSION,
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
        "requested_end": _CROSS,
        "segments": (
            SourceSegment(
                asset_id=_ASSET,
                camera_id=_CAMERA,
                session_id=_SESSION,
                start_time=_S,
                end_time=_CROSS,
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
        "requested_end": _CROSS,
        "actual_start_time": _S,
        "actual_end_time": _CROSS,
        "start_frame": 0,
        "end_frame": 899,
        "media_path": f"tenants/{_TENANT_A}/venues/{_VENUE_A}/evidence/{_EXTRACTION}.mp4",
        "media_format": "mp4",
        "duration_seconds": 300.0,
        "size_bytes": 4096,
        "metadata": {"checksum_sha256": _CHECKSUM, "encoder": "libx264"},
    }
    values.update(overrides)
    return ExtractedEvidence(**values)


def _package(
    *,
    evidence_ref: EvidenceRef | None = None,
    resolved_source: ResolvedSourceSegment | None = None,
    extraction: ExtractedEvidence | None = None,
) -> EvidencePackage:
    return _BUILDER.finalize(
        evidence_ref=evidence_ref or _evidence_ref(),
        resolved_source=resolved_source or _resolved_source(),
        extraction=extraction or _extraction(),
    )


# =============================================================================
# Full chain — every required link VERIFIED
# =============================================================================


class TestFullChainVerification:
    def test_every_required_link_is_verified(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert verification.verified is True
        assert verification.failures() == ()

        # The auditable record covers the complete chain.
        links = {check.link for check in verification.checks}
        expected_links = {
            "event -> evidence",
            "event -> evidence_id",
            "scope -> tenant",
            "scope -> venue",
            "evidence -> source",
            "source -> session",
            "session -> camera",
            "camera -> event_time",
            "camera -> frame_range",
            "time -> detector_version",
            "time -> tracker_version",
            "processing -> configuration",
            "configuration -> rule",
            "rule -> checksum",
            "checksum -> stored_evidence",
            "evidence -> package_identity",
        }
        assert expected_links <= links

    def test_all_checks_carry_expected_and_actual(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        for check in verification.checks:
            assert check.status is ProvenanceCheckStatus.VERIFIED
            # ``camera -> event_time`` compares an instant (event_time)
            # against the interval representation — expected and actual
            # differ by design there; every other verified check must
            # carry equal expected/actual.
            if check.link != "camera -> event_time":
                assert check.expected == check.actual, check.link

    def test_event_identity_preserved(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert verification.event_id == _EVENT
        assert verification.check("event -> evidence") is not None
        assert verification.check("event -> evidence").actual == str(_EVENT)  # type: ignore[union-attr]

    def test_source_identity_preserved(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert verification.check("evidence -> source").actual == str(_ASSET)  # type: ignore[union-attr]

    def test_configuration_version_preserved(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        check = verification.check("processing -> configuration")
        assert check is not None and check.actual == str(_CONFIG_V1)

    def test_rule_version_preserved(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        check = verification.check("configuration -> rule")
        assert check is not None and check.actual == f"{_RULE_ID}:{_RULE_VERSION}"

    def test_detector_and_tracker_versions_preserved(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert verification.check("time -> detector_version").actual == _DETECTOR  # type: ignore[union-attr]
        assert verification.check("time -> tracker_version").actual == _TRACKER  # type: ignore[union-attr]

    def test_checksum_and_storage_verified(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert verification.check("rule -> checksum").actual == _CHECKSUM  # type: ignore[union-attr]
        assert verification.check("checksum -> stored_evidence").passed  # type: ignore[union-attr]

    def test_package_identity_reproducible(self) -> None:
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        check = verification.check("evidence -> package_identity")
        assert check is not None and check.passed


# =============================================================================
# Negative matrix — every unauthorized substitution must fail
# =============================================================================


class TestSubstitutionRejected:
    """Substitute one hop across the WHOLE chain (all three composed
    models agree — so the package builder accepts them) and verify the
    verifier — the independent line of defense against the material
    event — flags the substitution."""

    def _verify_substituted(self, package: EvidencePackage) -> ProvenanceVerification:
        return _VERIFIER.verify(envelope=_envelope(), package=package)

    def test_another_tenant_fails(self) -> None:
        ref = _evidence_ref(tenant_id=_TENANT_B)
        package = _package(
            evidence_ref=ref,
            resolved_source=_resolved_source(tenant_id=_TENANT_B),
            extraction=_extraction(tenant_id=_TENANT_B),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        scope = verification.check("scope -> tenant")
        assert scope is not None and scope.status is ProvenanceCheckStatus.SUBSTITUTED
        assert scope.expected == str(_TENANT_A)
        assert scope.actual == str(_TENANT_B)

    def test_another_venue_fails(self) -> None:
        package = _package(
            evidence_ref=_evidence_ref(venue_id=_VENUE_B),
            resolved_source=_resolved_source(venue_id=_VENUE_B),
            extraction=_extraction(venue_id=_VENUE_B),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        scope = verification.check("scope -> venue")
        assert scope is not None and scope.status is ProvenanceCheckStatus.SUBSTITUTED
        assert scope.expected == str(_VENUE_A)
        assert scope.actual == str(_VENUE_B)

    def test_another_camera_fails(self) -> None:
        package = _package(
            evidence_ref=_evidence_ref(camera_id=_CAMERA_B),
            resolved_source=_resolved_source(camera_id=_CAMERA_B),
            extraction=_extraction(camera_id=_CAMERA_B),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        camera = verification.check("session -> camera")
        assert camera is not None and camera.status is ProvenanceCheckStatus.SUBSTITUTED
        assert camera.expected == str(_CAMERA)
        assert camera.actual == str(_CAMERA_B)

    def test_another_session_fails(self) -> None:
        package = _package(
            evidence_ref=_evidence_ref(video_session_id=_SESSION_B),
            resolved_source=_resolved_source(video_session_id=_SESSION_B),
            extraction=_extraction(session_id=_SESSION_B),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        session = verification.check("source -> session")
        assert session is not None and session.status is ProvenanceCheckStatus.SUBSTITUTED
        assert session.expected == str(_SESSION)
        assert session.actual == str(_SESSION_B)

    def test_another_configuration_version_fails(self) -> None:
        package = _package(
            evidence_ref=_evidence_ref(configuration_version_id=_CONFIG_V2),
            resolved_source=_resolved_source(configuration_version_id=_CONFIG_V2),
            extraction=_extraction(configuration_version_id=_CONFIG_V2),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        config = verification.check("processing -> configuration")
        assert config is not None and config.status is ProvenanceCheckStatus.SUBSTITUTED
        assert config.expected == str(_CONFIG_V1)
        assert config.actual == str(_CONFIG_V2)

    def test_another_rule_version_fails(self) -> None:
        package = _package(
            evidence_ref=_evidence_ref(rule_version=RuleVersion("v2")),
            resolved_source=_resolved_source(rule_version=RuleVersion("v2")),
            extraction=_extraction(rule_version=RuleVersion("v2")),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        rule = verification.check("configuration -> rule")
        assert rule is not None and rule.status is ProvenanceCheckStatus.SUBSTITUTED
        assert rule.expected == f"{_RULE_ID}:v1"
        assert rule.actual == f"{_RULE_ID}:v2"

    def test_another_source_asset_fails(self) -> None:
        """Swap ONLY the ref's asset — the resolved source segment is the
        independent source of truth for which asset covers the interval;
        a ref that claims a different asset is flagged as substituted."""
        package = _package(
            evidence_ref=_evidence_ref(video_asset_id=_ASSET_B),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        source = verification.check("evidence -> source")
        assert source is not None and source.status is ProvenanceCheckStatus.SUBSTITUTED
        assert source.expected == str(_ASSET)  # resolved segment asset
        assert source.actual == str(_ASSET_B)  # ref's (substituted) asset

    def test_another_event_id_fails(self) -> None:
        other_event = EventId(uuid.UUID("99999999-0000-0000-0000-000000000001"))
        package = _package(
            evidence_ref=_evidence_ref(event_id=other_event),
            resolved_source=_resolved_source(event_id=other_event),
            extraction=_extraction(event_id=other_event),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        event = verification.check("event -> evidence")
        assert event is not None and event.status is ProvenanceCheckStatus.SUBSTITUTED
        assert event.expected == str(_EVENT)
        assert event.actual == str(other_event)

    def test_inverted_frame_range_fails(self) -> None:
        """Every typed construction path rejects inverted ranges (the
        EvidenceRef contract, the package builder, and EvidencePackage
        re-validation). The verifier independently flags an unvalidated
        package carrying an inverted range — defense in depth against
        legacy/corrupt persisted data."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _evidence_ref(start_frame=900, end_frame=100)
        # ``model_construct`` skips validation — the only way an inverted
        # range can reach the verifier (legacy/corrupt data).
        base = _package()
        package = EvidencePackage.model_construct(
            package_id=base.package_id,
            evidence_ref=_evidence_ref().model_construct(**{
                **_evidence_ref().model_dump(),
                "start_frame": 900,
                "end_frame": 100,
            }),
            resolved_source=base.resolved_source,
            extraction=base.extraction,
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        frames = verification.check("camera -> frame_range")
        assert frames is not None and frames.status is ProvenanceCheckStatus.INCONSISTENT

    def test_checksum_missing_on_completed_evidence_fails(self) -> None:
        """The package builder refuses checksum-less completed evidence as
        its own first line of defense; the verifier independently flags a
        package whose composed extraction lacks the checksum."""
        from backend.app.domain.evidence.package import EvidencePackage

        with pytest.raises(ValueError, match="checksum"):
            _package(extraction=_extraction(metadata={"encoder": "libx264"}))
        base = _package()
        package = EvidencePackage(
            package_id=base.package_id,
            evidence_ref=base.evidence_ref,
            resolved_source=base.resolved_source,
            extraction=base.extraction.model_copy(update={"metadata": {"encoder": "libx264"}}),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        checksum = verification.check("rule -> checksum")
        assert checksum is not None and checksum.status is ProvenanceCheckStatus.MISSING

    def test_invalid_checksum_format_fails(self) -> None:
        package = _package(
            extraction=_extraction(metadata={"checksum_sha256": "zz-not-a-digest"}),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        checksum = verification.check("rule -> checksum")
        assert checksum is not None and checksum.status is ProvenanceCheckStatus.INCONSISTENT

    def test_storage_reference_missing_on_completed_evidence_fails(self) -> None:
        """The builder refuses storage-less completed evidence; the verifier
        independently flags a package whose extraction lost its media path."""
        from backend.app.domain.evidence.package import EvidencePackage

        with pytest.raises(ValueError, match="storage reference"):
            _package(
                extraction=_extraction(
                    media_path=None,
                    metadata={"checksum_sha256": _CHECKSUM},
                )
            )
        base = _package()
        package = EvidencePackage(
            package_id=base.package_id,
            evidence_ref=base.evidence_ref,
            resolved_source=base.resolved_source,
            extraction=base.extraction.model_copy(
                update={"media_path": None, "metadata": {"checksum_sha256": _CHECKSUM}}
            ),
        )
        verification = self._verify_substituted(package)
        assert verification.verified is False
        storage = verification.check("checksum -> stored_evidence")
        assert storage is not None and storage.status is ProvenanceCheckStatus.MISSING


# =============================================================================
# Replay — same event regenerates the same logical provenance
# =============================================================================


class TestReplay:
    def test_replay_preserves_every_identity(self) -> None:
        first = _VERIFIER.verify(envelope=_envelope(), package=_package())
        second = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert first.verified is True and second.verified is True
        assert first.checks == second.checks
        assert first.event_id == second.event_id

    def test_identical_media_produces_identical_checksum(self) -> None:
        first = _package()
        second = _package()
        assert first.checksum == second.checksum == _CHECKSUM
        assert first.package_id == second.package_id

    def test_regenerated_evidence_ref_matches(self) -> None:
        """The same event regenerates the same EvidenceRef (builder replay)."""
        from backend.app.intelligence.rules import EvidenceRequestBuilder, EvidenceRequestParams

        builder = EvidenceRequestBuilder()
        params = EvidenceRequestParams(
            tenant_id=_TENANT_A,
            venue_id=_VENUE_A,
            video_session_id=_SESSION,
            camera_id=_CAMERA,
        )
        first = builder.build(_envelope(), params=params)
        second = builder.build(_envelope(), params=params)
        assert first is not None and second is not None
        assert first.ref_id == second.ref_id
        assert first == second
        # The identity is content-derived (UUID5, deterministic scheme).
        parsed = uuid.UUID(str(first.ref_id))
        assert parsed.version == 5

    def test_different_media_produces_different_checksum(self) -> None:
        other = _extraction(
            extraction_id=MediaId(uuid.UUID("82000000-0000-0000-0000-000000000001")),
            metadata={"checksum_sha256": "b" * 64},
            media_path=f"tenants/{_TENANT_A}/venues/{_VENUE_A}/evidence/other.mp4",
        )
        first = _package()
        second = _package(extraction=other)
        assert first.checksum != second.checksum
        assert first.package_id != second.package_id


# =============================================================================
# Final gate — no material event without a valid evidence path
# =============================================================================


class TestFinalGate:
    def test_verified_evidence_is_required_for_material_events(self) -> None:
        """A fully-verified package exists for the material event → gate OK."""
        verification = _VERIFIER.verify(envelope=_envelope(), package=_package())
        assert verification.verified is True
        assert verification.missing_links == ()

    def test_missing_evidence_path_is_reported(self) -> None:
        """No evidence path (a ref without source identity) → the gate
        reports exactly which links are missing. The package is built
        unvalidated (the builder refuses to finalize a broken path) to
        prove the VERIFIER — the independent line of defense — catches
        the gaps."""
        ref = _evidence_ref(
            video_asset_id=None,
            camera_id=None,
            start_frame=None,
            end_frame=None,
            detector_version=None,
            tracker_version=None,
        )
        # A completed extraction without a media path is refused by the
        # builder — model_construct simulates the legacy/corrupt persisted
        # row that the verifier must catch independently.
        with pytest.raises(ValueError, match="storage reference"):
            _package(
                evidence_ref=ref,
                resolved_source=_resolved_source(
                    camera_id=None,
                    segments=(),
                ),
                extraction=_extraction(
                    camera_id=None,
                    media_path=None,
                    metadata={"checksum_sha256": _CHECKSUM},
                    actual_start_time=None,
                    actual_end_time=None,
                ),
            )
        package = EvidencePackage.model_construct(
            package_id=_REF,
            evidence_ref=ref,
            resolved_source=_resolved_source(camera_id=None, segments=()),
            extraction=_extraction(
                camera_id=None,
                media_path=None,
                metadata={"checksum_sha256": _CHECKSUM},
                actual_start_time=None,
                actual_end_time=None,
            ),
        )
        verification = _VERIFIER.verify(envelope=_envelope(), package=package)
        assert verification.verified is False
        assert verification.missing_links  # the audit record names the gaps

    def test_verification_is_deterministic_and_side_effect_free(self) -> None:
        envelope = _envelope()
        package = _package()
        snapshot = package.model_dump(mode="json")
        _VERIFIER.verify(envelope=envelope, package=package)
        # Neither input is mutated by verification.
        assert package.model_dump(mode="json") == snapshot
        assert envelope.model_dump(mode="json") == _envelope().model_dump(mode="json")


def _assert_verified(verification: ProvenanceVerification) -> None:
    assert verification.verified is True, verification.failures()
