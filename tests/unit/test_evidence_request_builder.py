"""Tests for Task 17.3 — deterministic Event → Evidence linkage.

Covers the EvidenceRequestBuilder contract:

- event with evidence: full provenance chain preserved (event_id,
  tenant_id, venue_id, session_id, source, event_time,
  configuration_version, rule_id, rule_version) + derived interval;
- event without evidence requirement: NO request is produced;
- duplicate event / replay: one logical request identity (Task 7);
- missing source: rejected — evidence is never linked without knowing
  which camera/asset it came from;
- wrong tenant / wrong venue / missing session: rejected — evidence is
  never linked to a scope that disagrees with the event payload;
- configuration + rule version preservation;
- the pipeline request IS the engine-attached request (same ref_id).

All fixtures use the REAL canonical contracts with fixed deterministic
IDs so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.app.intelligence.rules import (
    DWELL_THRESHOLD_CONFIG_KEY,
    EvidenceRequestBuilder,
    EvidenceRequestParams,
    InvalidEvidenceRequestError,
    build_operational_engine,
)
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    RuleId,
    RuleVersion,
    TenantId,
    TrackId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
)
from contracts.events import EventEnvelope, EvidenceRef, EvidenceType
from contracts.rules import (
    DataQualityPayload,
    DataQualitySeverity,
    DwellThresholdPayload,
    EvidenceRequirement,
    OccupancySessionPayload,
    OccupancySessionPhase,
    QualityFinding,
    QueueCandidatePayload,
    RuleEvaluationInput,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
    ServiceGapCandidatePayload,
    TurnoverDelayPayload,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    DwellInterval,
    TemporalStateKey,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT_A = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_TENANT_B = TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001"))
_VENUE_A = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_VENUE_B = VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_SESSION_B = VideoSessionId(uuid.UUID("93000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CAMERA_B = CameraId(uuid.UUID("94000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("95000000-0000-0000-0000-000000000001"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_V2 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_TRACK_A = TrackId(uuid.UUID("61000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("70000000-0000-0000-0000-000000000001"))

_RULE_ID = RuleId(RuleIdentifier.DWELL_THRESHOLD.value)
_RULE_VERSION = RuleVersion("v1")

_START = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_CROSS = datetime(2026, 8, 1, 10, 5, 0, tzinfo=UTC)
_PRODUCED = datetime(2026, 8, 1, 10, 5, 1, tzinfo=UTC)

_BUILDER = EvidenceRequestBuilder()


# =============================================================================
# Payload + envelope + params fixtures
# =============================================================================


def _dwell_payload(
    *,
    config_version: ConfigurationVersionId = _CONFIG_V1,
) -> DwellThresholdPayload:
    return DwellThresholdPayload(
        interval_id=_EVENT,
        tenant_id=_TENANT_A,
        venue_id=_VENUE_A,
        session_id=_SESSION,
        camera_id=_CAMERA,
        spatial_context_id="zone-lobby",
        dwell_start_time=_START,
        threshold_crossing_time=_CROSS,
        dwell_duration=300.0,
        threshold_seconds=300.0,
        configuration_version_id=config_version,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _queue_payload() -> QueueCandidatePayload:
    return QueueCandidatePayload(
        interval_id=_EVENT,
        tenant_id=_TENANT_A,
        venue_id=_VENUE_A,
        session_id=_SESSION,
        camera_id=_CAMERA,
        track_id=_TRACK_A,
        spatial_context_id="zone-queue-a",
        waiting_start_time=_START,
        qualification_time=_CROSS,
        waiting_duration=300.0,
        threshold_seconds=300.0,
        configuration_version_id=_CONFIG_V1,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _gap_payload() -> ServiceGapCandidatePayload:
    return ServiceGapCandidatePayload(
        interval_id=_EVENT,
        tenant_id=_TENANT_A,
        venue_id=_VENUE_A,
        session_id=_SESSION,
        camera_id=_CAMERA,
        track_id=_TRACK_A,
        service_area_id="service-front-desk",
        gap_start_time=_START,
        qualification_time=_CROSS,
        gap_duration=300.0,
        threshold_seconds=300.0,
        configuration_version_id=_CONFIG_V1,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _turnover_payload() -> TurnoverDelayPayload:
    return TurnoverDelayPayload(
        interval_id=_EVENT,
        tenant_id=_TENANT_A,
        venue_id=_VENUE_A,
        session_id=_SESSION,
        camera_id=_CAMERA,
        track_id=_TRACK_A,
        spatial_context_id="table-12",
        turnover_start_time=_START,
        threshold_crossing_time=_CROSS,
        turnover_duration=300.0,
        service_window_seconds=120.0,
        threshold_seconds=180.0,
        configuration_version_id=_CONFIG_V1,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _occupancy_payload() -> OccupancySessionPayload:
    return OccupancySessionPayload(
        phase=OccupancySessionPhase.STARTED,
        tenant_id=_TENANT_A,
        venue_id=_VENUE_A,
        session_id=_SESSION,
        camera_id=_CAMERA,
        spatial_context_id="zone-lobby",
        occupancy_count=1,
        occupied_tracks=(_TRACK_A,),
        occupancy_time=_CROSS,
        configuration_version_id=_CONFIG_V1,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _quality_payload(*, camera_id: CameraId | None = None) -> DataQualityPayload:
    return DataQualityPayload(
        findings=(
            QualityFinding(
                quality_code="DATA_NEGATIVE_DURATION",
                severity=DataQualitySeverity.ERROR,
                description="negative duration",
                affected_fact_type="dwell_interval",
                affected_fact_id="dwell-1",
                check_version="v1",
            ),
        ),
        primary_quality_code="DATA_NEGATIVE_DURATION",
        primary_severity=DataQualitySeverity.ERROR,
        affected_fact_type="dwell_interval",
        affected_fact_id="dwell-1",
        tenant_id=_TENANT_A,
        venue_id=_VENUE_A,
        session_id=_SESSION,
        camera_id=camera_id,
        event_time=_CROSS,
        configuration_version_id=_CONFIG_V1,
        rule_id=_RULE_ID,
        rule_version=_RULE_VERSION,
    )


def _envelope(
    event_type: str,
    payload: Any,
    *,
    event_id: EventId = _EVENT,
    event_time: datetime = _CROSS,
) -> EventEnvelope[Any]:
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        event_time=event_time,
        produced_at=_PRODUCED,
        source=f"rule:{_RULE_ID}:{_RULE_VERSION}",
        payload=payload,
    )


def _dwell_envelope() -> EventEnvelope[Any]:
    return _envelope(RuleEventType.DWELL_THRESHOLD.value, _dwell_payload())


def _params(**overrides: object) -> EvidenceRequestParams:
    values: dict[str, object] = {
        "tenant_id": _TENANT_A,
        "venue_id": _VENUE_A,
        "video_session_id": _SESSION,
        "camera_id": _CAMERA,
    }
    values.update(overrides)
    return EvidenceRequestParams(**values)


def _build(
    envelope: EventEnvelope[Any],
    *,
    params: EvidenceRequestParams | None = None,
    requirement: EvidenceRequirement = EvidenceRequirement.REQUIRED,
) -> EvidenceRef | None:
    return _BUILDER.build(
        envelope,
        params=params if params is not None else _params(),
        evidence_requirement=requirement,
    )


# =============================================================================
# Event with evidence / without requirement
# =============================================================================


class TestBuild:
    def test_event_with_evidence_preserves_full_chain(self) -> None:
        ref = _build(_dwell_envelope())
        assert ref is not None
        assert isinstance(ref, EvidenceRef)
        assert ref.ref_type is EvidenceType.VIDEO_CLIP
        # event linkage
        assert ref.event_id == _EVENT
        assert ref.event_time == _CROSS
        # scope
        assert ref.tenant_id == _TENANT_A
        assert ref.venue_id == _VENUE_A
        assert ref.video_session_id == _SESSION
        assert ref.camera_id == _CAMERA
        # provenance
        assert ref.configuration_version_id == _CONFIG_V1
        assert ref.rule_id == _RULE_ID
        assert ref.rule_version == _RULE_VERSION
        # derived interval: [dwell_start_time, event_time]
        assert ref.start_time == _START
        assert ref.end_time == _CROSS

    def test_event_without_evidence_requirement_returns_none(self) -> None:
        ref = _build(_dwell_envelope(), requirement=EvidenceRequirement.NONE)
        assert ref is None

    def test_optional_requirement_builds(self) -> None:
        ref = _build(_dwell_envelope(), requirement=EvidenceRequirement.OPTIONAL)
        assert ref is not None

    def test_non_canonical_envelope_rejected(self) -> None:
        with pytest.raises(InvalidEvidenceRequestError, match="EventEnvelope"):
            _BUILDER.build(object(), params=_params())  # type: ignore[arg-type]

    def test_unknown_event_type_rejected(self) -> None:
        envelope = _envelope("frame.detected", {"count": 1})
        with pytest.raises(InvalidEvidenceRequestError, match="event_type"):
            _build(envelope)

    def test_dict_payload_accepted(self) -> None:
        # A deserialized envelope carries the payload as a dict — the
        # builder resolves it against the canonical payload contract.
        envelope = _dwell_envelope()
        restored = EventEnvelope.model_validate(envelope.model_dump(mode="json"))
        ref = _build(restored)
        assert ref is not None
        assert ref.rule_id == _RULE_ID
        assert ref.start_time == _START


class TestAllRuleEventTypes:
    @pytest.mark.parametrize(
        ("event_type", "payload"),
        [
            (RuleEventType.DWELL_THRESHOLD.value, _dwell_payload()),
            (RuleEventType.QUEUE_CANDIDATE.value, _queue_payload()),
            (RuleEventType.SERVICE_GAP_CANDIDATE.value, _gap_payload()),
            (RuleEventType.TURNOVER_DELAY.value, _turnover_payload()),
            (RuleEventType.OCCUPANCY_SESSION.value, _occupancy_payload()),
            (RuleEventType.DATA_QUALITY.value, _quality_payload()),
        ],
    )
    def test_canonical_event_types_link(self, event_type: str, payload: Any) -> None:
        ref = _build(_envelope(event_type, payload))
        assert ref is not None
        assert ref.ref_type is EvidenceType.VIDEO_CLIP
        assert ref.video_session_id == _SESSION
        assert ref.configuration_version_id == _CONFIG_V1
        assert ref.rule_id == _RULE_ID

    def test_occupancy_interval_degenerates_to_instant(self) -> None:
        ref = _build(_envelope(RuleEventType.OCCUPANCY_SESSION.value, _occupancy_payload()))
        assert ref is not None
        assert ref.start_time == _CROSS
        assert ref.end_time == _CROSS


# =============================================================================
# Idempotency / replay (Task 7)
# =============================================================================


class TestIdempotency:
    def test_duplicate_event_one_logical_request(self) -> None:
        first = _build(_dwell_envelope())
        second = _build(_dwell_envelope())
        assert first is not None and second is not None
        assert first.ref_id == second.ref_id
        assert first == second

    def test_replay_one_logical_request(self) -> None:
        refs = [_build(_dwell_envelope(), params=_params()) for _ in range(3)]
        assert all(r is not None for r in refs)
        assert len({r.ref_id for r in refs}) == 1

    def test_identity_matches_engine_attached_request(self) -> None:
        """The pipeline request IS the engine-attached request (same ref_id)."""
        interval = DwellInterval(
            interval_id=_EVENT,
            fsm_kind="dwell",
            key=TemporalStateKey(
                fsm_kind="dwell",
                tenant_id=_TENANT_A,
                venue_id=_VENUE_A,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG_V1,
                track_id=_TRACK_A,
                semantic_context="zone-lobby",
            ),
            dwell_start=_START,
            dwell_end=None,
            last_seen=_CROSS,
            duration_seconds=300.0,
            qualified=True,
            minimum_dwell_seconds=0.0,
            reason=None,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        engine = build_operational_engine()
        result = engine.evaluate(
            _RULE_ID,
            _RULE_VERSION,
            RuleEvaluationInput(
                facts=(interval,),
                configuration={DWELL_THRESHOLD_CONFIG_KEY: 300.0},
                configuration_version_id=_CONFIG_V1,
                rule_version=_RULE_VERSION,
                event_time=_CROSS,
            ),
        )
        assert result.status is RuleEvaluationStatus.MATCH
        assert len(result.evidence_requests) == 1
        engine_ref = result.evidence_requests[0]

        builder_ref = _BUILDER.build(
            result.event,
            params=_params(),
        )
        assert builder_ref is not None
        assert builder_ref.ref_id == engine_ref.ref_id
        assert builder_ref.event_id == engine_ref.event_id
        assert builder_ref.video_session_id == engine_ref.video_session_id


# =============================================================================
# Scope security (Task 5 boundary)
# =============================================================================


class TestScopeSecurity:
    def test_wrong_tenant_rejected(self) -> None:
        with pytest.raises(InvalidEvidenceRequestError, match="tenant scope"):
            _build(_dwell_envelope(), params=_params(tenant_id=_TENANT_B))

    def test_wrong_venue_rejected(self) -> None:
        with pytest.raises(InvalidEvidenceRequestError, match="venue scope"):
            _build(_dwell_envelope(), params=_params(venue_id=_VENUE_B))

    def test_wrong_session_rejected(self) -> None:
        with pytest.raises(InvalidEvidenceRequestError, match="session mismatch"):
            _build(_dwell_envelope(), params=_params(video_session_id=_SESSION_B))

    def test_camera_mismatch_rejected(self) -> None:
        with pytest.raises(InvalidEvidenceRequestError, match="camera mismatch"):
            _build(_dwell_envelope(), params=_params(camera_id=_CAMERA_B))

    def test_missing_source_rejected(self) -> None:
        """A data_quality event without camera needs a source from params."""
        envelope = _envelope(
            RuleEventType.DATA_QUALITY.value,
            _quality_payload(camera_id=None),
        )
        with pytest.raises(InvalidEvidenceRequestError, match="source provenance"):
            _build(envelope, params=_params(camera_id=None))

    def test_params_camera_used_when_payload_has_none(self) -> None:
        envelope = _envelope(
            RuleEventType.DATA_QUALITY.value,
            _quality_payload(camera_id=None),
        )
        ref = _build(envelope, params=_params(camera_id=_CAMERA))
        assert ref is not None
        assert ref.camera_id == _CAMERA

    def test_video_asset_satisfies_source(self) -> None:
        envelope = _envelope(
            RuleEventType.DATA_QUALITY.value,
            _quality_payload(camera_id=None),
        )
        ref = _build(envelope, params=_params(camera_id=None, video_asset_id=_ASSET))
        assert ref is not None
        assert ref.camera_id is None
        assert ref.video_asset_id == _ASSET


# =============================================================================
# Provenance preservation
# =============================================================================


class TestProvenancePreservation:
    def test_configuration_version_preserved(self) -> None:
        payload = _dwell_payload(config_version=_CONFIG_V2)
        ref = _build(_envelope(RuleEventType.DWELL_THRESHOLD.value, payload))
        assert ref is not None
        assert ref.configuration_version_id == _CONFIG_V2

    def test_rule_version_preserved(self) -> None:
        ref = _build(_dwell_envelope())
        assert ref is not None
        assert ref.rule_version == _RULE_VERSION

    def test_rule_id_preserved(self) -> None:
        ref = _build(_dwell_envelope())
        assert ref is not None
        assert ref.rule_id == _RULE_ID

    def test_interval_override_via_params(self) -> None:
        window_start = datetime(2026, 8, 1, 9, 55, 0, tzinfo=UTC)
        window_end = datetime(2026, 8, 1, 10, 4, 0, tzinfo=UTC)
        ref = _build(
            _dwell_envelope(),
            params=_params(start_time=window_start, end_time=window_end),
        )
        assert ref is not None
        assert ref.start_time == window_start
        assert ref.end_time == window_end

    def test_frame_range_and_versions_preserved(self) -> None:
        ref = _build(
            _dwell_envelope(),
            params=_params(
                video_asset_id=_ASSET,
                start_frame=120,
                end_frame=8999,
                detector_version="8.1.0",
                tracker_version="1.0.0",
            ),
        )
        assert ref is not None
        assert ref.start_frame == 120
        assert ref.end_frame == 8999
        assert ref.detector_version == "8.1.0"
        assert ref.tracker_version == "1.0.0"
        assert ref.video_asset_id == _ASSET

    def test_impossible_interval_rejected(self) -> None:
        future_start = datetime(2026, 8, 1, 11, 0, 0, tzinfo=UTC)
        with pytest.raises(InvalidEvidenceRequestError, match="precedes"):
            _build(_dwell_envelope(), params=_params(start_time=future_start))

    def test_envelope_never_modified(self) -> None:
        envelope = _dwell_envelope()
        snapshot = envelope.model_dump(mode="json")
        _build(envelope)
        assert envelope.model_dump(mode="json") == snapshot

    def test_request_round_trips(self) -> None:
        ref = _build(_dwell_envelope())
        assert ref is not None
        restored = EvidenceRef.model_validate(ref.model_dump(mode="json"))
        assert restored == ref
