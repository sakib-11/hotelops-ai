"""Contract tests for the finalized canonical EvidenceRef (Task 17.2).

Verifies the REQUIRED / OPTIONAL / NOT_APPLICABLE field policy and the
validation matrix:

- event linkage (event_id + event_time) is REQUIRED;
- media-backed types (FRAME / IMAGE / VIDEO_CLIP) require source
  provenance (video_asset_id | video_session_id | camera_id);
- time / frame ranges are ordered (end >= start, non-negative frames);
- checksum is a canonical SHA-256;
- tenant/venue scope is structurally valid (venue requires tenant);
- rule provenance preserves its configuration version;
- version formats are controlled (v1 / 8.1.0).

Also verifies the canonical chain

    EVENT → SOURCE → SESSION → CAMERA → TIME/FRAME RANGE
                                          → PROCESSING PROVENANCE

round-trips byte-identically, and that a rule-emitted REQUEST stays
wall-clock free (``created_at`` unset) so replay is deterministic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    EventId,
    EvidenceId,
    RuleId,
    TenantId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
)
from contracts.events import EvidenceRef, EvidenceType

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("80000000-0000-0000-0000-000000000001"))

_EVENT_TIME = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)

_SHA256_OK = "a" * 64


def _valid(**overrides: object) -> EvidenceRef:
    """A fully valid evidence ref; override any field per test."""
    values: dict[str, object] = {
        "ref_id": _REF,
        "ref_type": EvidenceType.VIDEO_CLIP,
        "ref_uri": "s3://evidence/tenants/1/clip.mp4",
        "event_id": _EVENT,
        "event_time": _EVENT_TIME,
        "video_session_id": _SESSION,
    }
    values.update(overrides)
    return EvidenceRef(**values)


# =============================================================================
# REQUIRED fields / event linkage
# =============================================================================


class TestRequiredFields:
    def test_valid_evidence_ref(self) -> None:
        ref = _valid()
        assert ref.schema_version == SCHEMA_VERSION
        assert ref.ref_type is EvidenceType.VIDEO_CLIP

    def test_missing_ref_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid(ref_id=None)  # type: ignore[arg-type]

    def test_missing_ref_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid(ref_type=None)  # type: ignore[arg-type]

    def test_missing_ref_uri_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid(ref_uri="")

    def test_missing_event_id_rejected(self) -> None:
        """Task 17.2 test 2 — missing event linkage."""
        with pytest.raises(ValidationError, match="event_id"):
            _valid(event_id=None)  # type: ignore[arg-type]

    def test_missing_event_time_rejected(self) -> None:
        with pytest.raises(ValidationError, match="event_time"):
            _valid(event_time=None)  # type: ignore[arg-type]


# =============================================================================
# Source provenance (media-backed types)
# =============================================================================


class TestSourceProvenance:
    def test_missing_source_rejected(self) -> None:
        """Task 17.2 test 3 — media evidence without source provenance."""
        for ref_type in (
            EvidenceType.FRAME,
            EvidenceType.IMAGE,
            EvidenceType.VIDEO_CLIP,
        ):
            with pytest.raises(ValidationError, match="source provenance"):
                _valid(ref_type=ref_type, video_session_id=None)

    def test_session_alone_satisfies_source(self) -> None:
        ref = _valid(video_session_id=_SESSION, camera_id=None, video_asset_id=None)
        assert ref.video_session_id == _SESSION

    def test_camera_alone_satisfies_source(self) -> None:
        ref = _valid(video_session_id=None, camera_id=_CAMERA, video_asset_id=None)
        assert ref.camera_id == _CAMERA

    def test_asset_alone_satisfies_source(self) -> None:
        ref = _valid(video_session_id=None, camera_id=None, video_asset_id=_ASSET)
        assert ref.video_asset_id == _ASSET

    def test_object_storage_needs_no_source(self) -> None:
        ref = _valid(
            ref_type=EvidenceType.OBJECT_STORAGE,
            video_session_id=None,
            camera_id=None,
            video_asset_id=None,
        )
        assert ref.ref_type is EvidenceType.OBJECT_STORAGE

    def test_analytical_artifact_needs_no_source(self) -> None:
        ref = _valid(
            ref_type=EvidenceType.ANALYTICAL_ARTIFACT,
            video_session_id=None,
            camera_id=None,
            video_asset_id=None,
        )
        assert ref.ref_type is EvidenceType.ANALYTICAL_ARTIFACT


# =============================================================================
# Time / frame range
# =============================================================================


class TestRangeValidation:
    def test_end_time_before_start_time_rejected(self) -> None:
        """Task 17.2 test 4 — invalid time range."""
        start = _EVENT_TIME
        end = datetime(2026, 8, 1, 9, 59, 59, tzinfo=UTC)
        with pytest.raises(ValidationError, match="end_time"):
            _valid(start_time=start, end_time=end)

    def test_end_time_equals_start_time_accepted(self) -> None:
        ref = _valid(start_time=_EVENT_TIME, end_time=_EVENT_TIME)
        assert ref.start_time == ref.end_time

    def test_zero_duration_window_accepted(self) -> None:
        ref = _valid(start_time=_EVENT_TIME, end_time=_EVENT_TIME)
        assert ref.end_time >= ref.start_time

    def test_negative_start_frame_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid(start_frame=-1)

    def test_inverted_frame_range_rejected(self) -> None:
        """Task 17.2 test 5 — invalid frame range."""
        with pytest.raises(ValidationError, match="end_frame"):
            _valid(start_frame=100, end_frame=99)

    def test_frame_range_partial_accepted(self) -> None:
        # Open-ended ranges are legitimate for clip requests.
        ref = _valid(start_frame=42, end_frame=None)
        assert ref.start_frame == 42
        assert ref.end_frame is None

    def test_full_frame_range_accepted(self) -> None:
        ref = _valid(start_frame=10, end_frame=120)
        assert (ref.start_frame, ref.end_frame) == (10, 120)


# =============================================================================
# Checksum
# =============================================================================


class TestChecksum:
    def test_valid_checksum_accepted(self) -> None:
        ref = _valid(checksum=_SHA256_OK)
        assert ref.checksum == _SHA256_OK

    def test_checksum_normalized_to_lowercase(self) -> None:
        ref = _valid(checksum="A" * 64)
        assert ref.checksum == _SHA256_OK

    def test_invalid_checksum_length_rejected(self) -> None:
        """Task 17.2 test 6 — invalid checksum format."""
        with pytest.raises(ValidationError, match="checksum"):
            _valid(checksum="abc123")

    def test_invalid_checksum_characters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="checksum"):
            _valid(checksum="z" + "0" * 63)


# =============================================================================
# Tenant / venue scope
# =============================================================================


class TestTenantVenueScope:
    def test_venue_without_tenant_rejected(self) -> None:
        """Task 17.2 test 7 — invalid tenant scope."""
        with pytest.raises(ValidationError, match="venue_id"):
            _valid(tenant_id=None, venue_id=_VENUE)

    def test_tenant_without_venue_accepted(self) -> None:
        ref = _valid(tenant_id=_TENANT, venue_id=None)
        assert ref.tenant_id == _TENANT

    def test_tenant_and_venue_accepted(self) -> None:
        ref = _valid(tenant_id=_TENANT, venue_id=_VENUE)
        assert ref.tenant_id == _TENANT
        assert ref.venue_id == _VENUE


# =============================================================================
# Processing provenance (rule / configuration / versions)
# =============================================================================


class TestProcessingProvenance:
    def test_rule_requires_configuration_version(self) -> None:
        """Task 17.2 test 8 — missing configuration provenance."""
        with pytest.raises(ValidationError, match="configuration_version_id"):
            _valid(rule_id=RuleId("dwell_threshold"), rule_version="v1")

    def test_rule_requires_rule_version(self) -> None:
        with pytest.raises(ValidationError, match="rule_version"):
            _valid(rule_id=RuleId("dwell_threshold"), configuration_version_id=_CONFIG)

    def test_full_rule_provenance_accepted(self) -> None:
        ref = _valid(
            rule_id=RuleId("dwell_threshold"),
            rule_version="v1",
            configuration_version_id=_CONFIG,
        )
        assert ref.rule_version == "v1"
        assert ref.configuration_version_id == _CONFIG

    def test_configuration_without_rule_accepted(self) -> None:
        # A non-rule ref may still carry its configuration context.
        ref = _valid(configuration_version_id=_CONFIG)
        assert ref.configuration_version_id == _CONFIG

    def test_invalid_rule_version_format_rejected(self) -> None:
        for bad in ("latest", "1", "V1", "v1-rc", ""):
            with pytest.raises(ValidationError, match="rule_version"):
                _valid(rule_id=RuleId("x"), rule_version=bad, configuration_version_id=_CONFIG)

    def test_valid_rule_version_formats_accepted(self) -> None:
        for good in ("v1", "v2", "v1.2", "v1.2.3"):
            ref = _valid(
                rule_id=RuleId("x"),
                rule_version=good,
                configuration_version_id=_CONFIG,
            )
            assert ref.rule_version == good

    def test_invalid_detector_version_format_rejected(self) -> None:
        with pytest.raises(ValidationError, match="version"):
            _valid(detector_version="latest")
        with pytest.raises(ValidationError, match="version"):
            _valid(detector_version="v8.1.0")

    def test_valid_detector_tracker_versions_accepted(self) -> None:
        ref = _valid(detector_version="8.1.0", tracker_version="1.0.0-rc1")
        assert ref.detector_version == "8.1.0"
        assert ref.tracker_version == "1.0.0-rc1"


# =============================================================================
# Timestamps / determinism
# =============================================================================


class TestTimestamps:
    def test_naive_event_time_rejected(self) -> None:
        with pytest.raises(ValidationError, match="naive"):
            _valid(event_time=datetime(2026, 8, 1, 10, 0, 0))

    def test_naive_window_times_rejected(self) -> None:
        with pytest.raises(ValidationError, match="naive"):
            _valid(start_time=datetime(2026, 8, 1, 10, 0, 0), end_time=_EVENT_TIME)

    def test_created_at_optional_for_requests(self) -> None:
        # A rule-emitted REQUEST must stay wall-clock free for replay
        # determinism — created_at is set by the fulfillment layer only.
        ref = _valid()
        assert ref.created_at is None


# =============================================================================
# Canonical chain / round-trip
# =============================================================================


class TestCanonicalChain:
    def test_full_chain_round_trip(self) -> None:
        """EVENT → SOURCE → SESSION → CAMERA → TIME/FRAME → PROVENANCE."""
        original = _valid(
            ref_type=EvidenceType.VIDEO_CLIP,
            tenant_id=_TENANT,
            venue_id=_VENUE,
            video_asset_id=_ASSET,
            video_session_id=_SESSION,
            camera_id=_CAMERA,
            event_time=_EVENT_TIME,
            start_time=_EVENT_TIME,
            end_time=datetime(2026, 8, 1, 10, 5, 0, tzinfo=UTC),
            start_frame=0,
            end_frame=8999,
            configuration_version_id=_CONFIG,
            detector_version="8.1.0",
            tracker_version="1.0.0",
            rule_id=RuleId("dwell_threshold"),
            rule_version="v1",
            checksum=_SHA256_OK,
            created_at=datetime(2026, 8, 1, 10, 5, 1, tzinfo=UTC),
            metadata={"track_id": "60000000-0000-0000-0000-000000000001"},
        )
        data = original.model_dump(mode="json")
        restored = EvidenceRef.model_validate(data)
        assert restored == original
        assert restored.event_id == _EVENT
        assert restored.video_session_id == _SESSION
        assert restored.camera_id == _CAMERA
        assert restored.configuration_version_id == _CONFIG
        assert restored.rule_id == RuleId("dwell_threshold")

    def test_extra_fields_rejected(self) -> None:
        data = _valid().model_dump()
        data["fabricated_field"] = True
        with pytest.raises(ValidationError):
            EvidenceRef.model_validate(data)

    def test_unknown_evidence_type_rejected(self) -> None:
        data = _valid().model_dump()
        data["ref_type"] = "not_a_type"
        with pytest.raises(ValidationError):
            EvidenceRef.model_validate(data)

    def test_metadata_remains_free_form(self) -> None:
        ref = _valid(metadata={"track_id": "track-1", "semantic_context": "zone-lobby"})
        assert ref.metadata["semantic_context"] == "zone-lobby"
