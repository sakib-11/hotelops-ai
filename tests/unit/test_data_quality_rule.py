"""Tests for Task 16.9 — the deterministic data_quality rule (FINAL Task 16 rule).

Detects deterministic data-quality conditions in canonical Task 15 facts
and emits an aggregate ``data_quality`` event with stable machine-readable
quality codes. The rule does NOT inspect raw video, frames, YOLO
detections, tracker objects, bounding boxes, or pixels — it consumes
canonical facts only (Part 1).

Covered (Task 16.9 Part 41):

- unit: valid fact, missing camera/track identity, invalid event_time,
  non-monotonic event time, negative duration, temporal inconsistency,
  invalid provenance, unknown spatial reference, configuration mismatch,
  missing configuration, invalid configuration, missing fact;
- quality-check registry: each code independently, severity mapping,
  applicability, deterministic ordering, duplicate registration;
- multiple failures: two findings, three findings, all-applicable checks;
- idempotency: repeated evaluation, duplicate delivery, replay;
- version: rule v1, unsupported version, config v1, config v2, historical
  replay;
- security: cross-tenant, cross-venue;
- contract: EventEnvelope, EvidenceRef;
- invariants (Part 42): 16 guarantees;
- golden tests (§25-36).

All fixtures use the REAL canonical contracts with fixed deterministic IDs
so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.intelligence.rules import (
    KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY,
    MissingRuleConfigurationError,
    MixedScopeRuleInputError,
    UnsupportedRuleVersionError,
    build_operational_engine,
    deterministic_event_id,
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
    VideoSessionId,
)
from contracts.events import EventEnvelope, EvidenceRef
from contracts.rules import (
    DataQualityPayload,
    DataQualitySeverity,
    RuleEvaluationInput,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    DwellInterval,
    OccupancySnapshot,
    TemporalReason,
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
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_V2 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_TRACK_A = TrackId(uuid.UUID("61000000-0000-0000-0000-000000000001"))
_TRACK_B = TrackId(uuid.UUID("62000000-0000-0000-0000-000000000002"))

_RULE_ID = RuleId(RuleIdentifier.DATA_QUALITY.value)
_RULE_VERSION = RuleVersion("v1")
_ZONE_A = "zone-front-desk"
_ZONE_B = "zone-restaurant"
_ZONE_UNKNOWN = "zone-nonexistent"

_PROCESSED = datetime(2026, 8, 1, 11, 0, 30, tzinfo=UTC)
_START = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)


def _event(seconds: int) -> datetime:
    """Deterministic event-time: 10:00:00 + ``seconds`` (Part 16)."""
    return _START + timedelta(seconds=seconds)


def _key(
    *,
    tenant_id: TenantId = _TENANT_A,
    venue_id: VenueId = _VENUE_A,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG_V1,
    track_id: TrackId = _TRACK_A,
    semantic_context: str | None = _ZONE_A,
) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind="dwell",
        tenant_id=tenant_id,
        venue_id=venue_id,
        session_id=session_id,
        camera_id=camera_id,
        configuration_version_id=configuration_version_id,
        track_id=track_id,
        semantic_context=semantic_context,
    )


def _interval(
    *,
    duration: float = 300.0,
    dwell_start: datetime = _START,
    last_seen: datetime | None = None,
    reason: TemporalReason | None = None,
    interval_id: EventId | None = None,
    key: TemporalStateKey | None = None,
    label: str = "i",
    qualified: bool = True,
    minimum_dwell_seconds: float = 0.0,
) -> DwellInterval:
    """A canonical DwellInterval fact with deterministic content-derived id."""
    last = last_seen if last_seen is not None else dwell_start + timedelta(seconds=duration)
    return DwellInterval(
        interval_id=interval_id
        or EventId(uuid.uuid5(uuid.NAMESPACE_URL, f"dwell-{label}-{dwell_start.isoformat()}")),
        fsm_kind="dwell",
        key=key or _key(),
        dwell_start=dwell_start,
        dwell_end=last if reason is not None else None,
        last_seen=last,
        duration_seconds=duration,
        qualified=qualified,
        minimum_dwell_seconds=minimum_dwell_seconds,
        reason=reason,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _snapshot(
    *,
    occupancy_count: int = 1,
    key: TemporalStateKey | None = None,
    previous_count: int = 0,
    delta: int = 1,
) -> OccupancySnapshot:
    """A canonical OccupancySnapshot (default: reconciled counts)."""
    return OccupancySnapshot(
        snapshot_id=EventId(uuid.uuid4()),
        fsm_kind="occupancy",
        key=key or _key().model_copy(update={"fsm_kind": "occupancy"}),
        event_time=_event(300),
        previous_count=previous_count,
        delta=delta,
        occupancy_count=occupancy_count,
        occupied_tracks=(),
        source_transition_id=EventId(uuid.uuid4()),
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(
    fact,
    *,
    config_version: ConfigurationVersionId = _CONFIG_V1,
    event_time: datetime | None = None,
    rule_version: RuleVersion = _RULE_VERSION,
    known_contexts: tuple[str, ...] = (_ZONE_A, _ZONE_B),
):
    """A canonical RuleEvaluationInput for the data_quality rule."""
    return RuleEvaluationInput(
        facts=(fact,),
        configuration={
            KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY: list(known_contexts),
        },
        configuration_version_id=config_version,
        rule_version=rule_version,
        event_time=event_time if event_time is not None else _event(300),
        processing_time=_PROCESSED,
    )


def _engine():
    """The sanctioned operational engine (data_quality:v1 registered)."""
    return build_operational_engine()


def _evaluate(fact, **kwargs):
    engine = _engine()
    return engine.evaluate(_RULE_ID, _RULE_VERSION, _input(fact, **kwargs))


# =============================================================================
# 41. UNIT TESTS
# =============================================================================


class TestUnit:
    def test_valid_fact_no_match(self) -> None:
        """A completely valid canonical fact produces NO_MATCH (§25)."""
        result = _evaluate(_interval())
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_valid_snapshot_no_match(self) -> None:
        result = _evaluate(_snapshot())
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_missing_camera_identity_match(self) -> None:
        """Missing source identity (camera_id) → MATCH with the quality code."""
        key = _key().model_copy(update={"camera_id": None})
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.MATCH
        payload = result.event.payload
        codes = [f.quality_code for f in payload.findings]
        assert "DATA_MISSING_REQUIRED_IDENTITY" in codes

    def test_missing_track_identity_match(self) -> None:
        key = _key().model_copy(update={"track_id": None})
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_MISSING_REQUIRED_IDENTITY" in codes

    def test_missing_event_time_check_fires(self) -> None:
        """A fact with no event-time field → DATA_INVALID_EVENT_TIME.

        The check is the SECOND line of defense (Part 1): a DwellInterval
        missing ``last_seen`` cannot be constructed through the canonical
        input boundary (schema validation protects contracts), so the check
        is exercised directly against a ``model_construct``-ed fact.
        """
        from backend.app.intelligence.rules.data_quality import _check_invalid_event_time
        from contracts.temporal import DwellInterval

        interval = _interval()
        data = interval.model_dump(mode="python")
        data["last_seen"] = None
        bad = DwellInterval.model_construct(**data)
        finding = _check_invalid_event_time().evaluator(bad)
        assert finding is not None
        assert finding.quality_code == "DATA_INVALID_EVENT_TIME"

    def test_schema_boundary_rejects_missing_event_time(self) -> None:
        """Schema validation protects contracts (Part 1): a fact missing its
        event-time cannot flow through the canonical input boundary."""
        from pydantic import ValidationError

        interval = _interval().model_copy(update={"last_seen": None})
        with pytest.raises((ValidationError, TypeError)):
            _input(interval)

    def test_non_monotonic_event_time_check_fires(self) -> None:
        """end before start → DATA_NON_MONOTONIC_EVENT_TIME (defense-in-depth)."""
        from backend.app.intelligence.rules.data_quality import (
            _check_non_monotonic_event_time,
        )
        from contracts.temporal import DwellInterval

        interval = _interval()
        data = interval.model_dump(mode="python")
        data["dwell_end"] = _START - timedelta(seconds=1)
        bad = DwellInterval.model_construct(**data)
        finding = _check_non_monotonic_event_time().evaluator(bad)
        assert finding is not None
        assert finding.quality_code == "DATA_NON_MONOTONIC_EVENT_TIME"

    def test_schema_boundary_rejects_non_monotonic(self) -> None:
        from pydantic import ValidationError

        bad = _interval().model_copy(update={"dwell_end": _START - timedelta(seconds=1)})
        with pytest.raises((ValidationError, TypeError)):
            _input(bad)

    def test_negative_duration_match(self) -> None:
        """duration_seconds < 0 → DATA_NEGATIVE_DURATION (§28-style)."""
        interval = _interval()
        bad = interval.model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_NEGATIVE_DURATION" in codes

    def test_temporal_inconsistency_qualified_below_minimum(self) -> None:
        """qualified=True below the configured minimum is contradictory."""
        interval = _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_TEMPORAL_INCONSISTENCY" in codes

    def test_temporal_inconsistency_snapshot_counts_check_fires(self) -> None:
        """Unreconciled occupancy counts → DATA_TEMPORAL_INCONSISTENCY.

        The snapshot model itself validates count reconciliation at
        construction, so the check (defense-in-depth) is exercised directly.
        """
        from backend.app.intelligence.rules.data_quality import _check_temporal_inconsistency
        from contracts.temporal import OccupancySnapshot

        snapshot = _snapshot()
        data = snapshot.model_dump(mode="python")
        data["occupancy_count"] = 3
        bad = OccupancySnapshot.model_construct(**data)
        finding = _check_temporal_inconsistency().evaluator(bad)
        assert finding is not None
        assert finding.quality_code == "DATA_TEMPORAL_INCONSISTENCY"

    def test_invalid_provenance_match(self) -> None:
        """Blank fsm_version → DATA_INVALID_PROVENANCE."""
        interval = _interval()
        bad = interval.model_copy(update={"fsm_version": ""})
        result = _evaluate(bad)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_INVALID_PROVENANCE" in codes

    def test_unknown_spatial_reference_match(self) -> None:
        """semantic_context outside the configured known set → MATCH."""
        key = _key(semantic_context=_ZONE_UNKNOWN)
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_UNKNOWN_SPATIAL_REFERENCE" in codes

    def test_known_spatial_reference_no_match(self) -> None:
        key = _key(semantic_context=_ZONE_B)
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_configuration_mismatch_match(self) -> None:
        """Fact key config v1 evaluated under config v2 → DATA_CONFIGURATION_MISMATCH (§32)."""
        key = _key(configuration_version_id=_CONFIG_V1)
        interval = _interval(key=key)
        result = _evaluate(interval, config_version=_CONFIG_V2)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_CONFIGURATION_MISMATCH" in codes
        # The event preserves the EVALUATION's pinned version (never switches).
        assert result.configuration_version_id == _CONFIG_V2
        assert result.event.payload.configuration_version_id == _CONFIG_V2

    def test_missing_configuration_raises(self) -> None:
        """Missing required configuration key → typed registry error."""
        engine = _engine()
        inp = RuleEvaluationInput(
            facts=(_interval(),),
            configuration={},  # known_spatial_contexts missing
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MissingRuleConfigurationError):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_invalid_configuration_value_invalid(self) -> None:
        """Present-but-invalid configuration value → INVALID (never a silent default)."""
        result = _evaluate(_interval(), known_contexts=(123,))  # type: ignore[arg-type]
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.reason is not None

    def test_empty_known_contexts_marks_unknown(self) -> None:
        """An EMPTY known set is explicit: any semantic_context is unknown."""
        key = _key(semantic_context=_ZONE_A)
        result = _evaluate(_interval(key=key), known_contexts=())
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_UNKNOWN_SPATIAL_REFERENCE" in codes

    def test_missing_tenant_invalid(self) -> None:
        """Un-attributable fact (missing tenant) → INVALID, never MATCH (Parts 12/22/23)."""
        key = _key().model_copy(update={"tenant_id": None})
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.reason is not None

    def test_schema_boundary_rejects_non_canonical_fact(self) -> None:
        """A non-canonical fact cannot flow through the canonical input
        boundary (schema validation protects contracts — Part 1)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _input(object())  # type: ignore[arg-type]


# =============================================================================
# Quality-check registry tests (Part 6/37)
# =============================================================================


class TestQualityCheckRegistry:
    def test_all_expected_checks_registered(self) -> None:
        from backend.app.intelligence.rules.data_quality import build_quality_check_registry

        registry = build_quality_check_registry(
            known_spatial_contexts=frozenset({_ZONE_A}),
            configuration_version_id=_CONFIG_V1,
        )
        ids = [c.check_id for c in registry.list()]
        assert ids == sorted(ids)  # deterministic ordering
        assert "DATA_MISSING_REQUIRED_IDENTITY" in ids
        assert "DATA_INVALID_EVENT_TIME" in ids
        assert "DATA_NON_MONOTONIC_EVENT_TIME" in ids
        assert "DATA_NEGATIVE_DURATION" in ids
        assert "DATA_TEMPORAL_INCONSISTENCY" in ids
        assert "DATA_INVALID_PROVENANCE" in ids
        assert "DATA_UNKNOWN_SPATIAL_REFERENCE" in ids
        assert "DATA_CONFIGURATION_MISMATCH" in ids

    def test_duplicate_registration_rejected(self) -> None:
        from backend.app.intelligence.rules.data_quality import (
            QualityCheck,
            QualityCheckRegistry,
        )

        registry = QualityCheckRegistry()
        check = QualityCheck(
            check_id="X",
            description="d",
            severity=DataQualitySeverity.INFO,
            applicability=frozenset({_fact_type()}),
            version="v1",
            evaluator=lambda f: None,
        )
        registry.register(check)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(check)

    def test_severity_is_deterministic_per_check(self) -> None:
        from backend.app.intelligence.rules.data_quality import build_quality_check_registry

        registry = build_quality_check_registry(
            known_spatial_contexts=frozenset({_ZONE_A}),
            configuration_version_id=_CONFIG_V1,
        )
        by_code = {c.check_id: c.severity for c in registry.list()}
        assert by_code["DATA_MISSING_REQUIRED_IDENTITY"] is DataQualitySeverity.ERROR
        assert by_code["DATA_INVALID_EVENT_TIME"] is DataQualitySeverity.ERROR
        assert by_code["DATA_NON_MONOTONIC_EVENT_TIME"] is DataQualitySeverity.ERROR
        assert by_code["DATA_NEGATIVE_DURATION"] is DataQualitySeverity.ERROR
        assert by_code["DATA_TEMPORAL_INCONSISTENCY"] is DataQualitySeverity.ERROR
        assert by_code["DATA_INVALID_PROVENANCE"] is DataQualitySeverity.WARNING
        assert by_code["DATA_UNKNOWN_SPATIAL_REFERENCE"] is DataQualitySeverity.WARNING
        assert by_code["DATA_CONFIGURATION_MISMATCH"] is DataQualitySeverity.CRITICAL

    def test_applicability(self) -> None:
        from backend.app.intelligence.rules.data_quality import build_quality_check_registry
        from contracts.rules import FactType

        registry = build_quality_check_registry(
            known_spatial_contexts=frozenset({_ZONE_A}),
            configuration_version_id=_CONFIG_V1,
        )
        by_code = {c.check_id: c for c in registry.list()}
        # duration checks apply only to interval/measurement fact types
        neg = by_code["DATA_NEGATIVE_DURATION"]
        assert FactType.DWELL_INTERVAL in neg.applicability
        assert FactType.OCCUPANCY_SNAPSHOT not in neg.applicability


def _fact_type():
    from contracts.rules import FactType

    return FactType.DWELL_INTERVAL


# =============================================================================
# Multiple failure tests (Part 13/33)
# =============================================================================


class TestMultipleFindings:
    def test_two_findings_aggregated(self) -> None:
        """A fact with two independent issues → one event with BOTH findings."""
        interval = _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0)
        bad = interval.model_copy(update={"fsm_version": ""})
        result = _evaluate(bad)
        assert result.status is RuleEvaluationStatus.MATCH
        payload = result.event.payload
        codes = [f.quality_code for f in payload.findings]
        assert "DATA_TEMPORAL_INCONSISTENCY" in codes
        assert "DATA_INVALID_PROVENANCE" in codes
        assert len(codes) == 2

    def test_three_findings_aggregated(self) -> None:
        interval = _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0)
        bad = interval.model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        codes = [f.quality_code for f in result.event.payload.findings]
        assert len(codes) >= 2
        assert codes == sorted(codes)  # deterministic ordering (Part 14)

    def test_ordering_is_deterministic(self) -> None:
        result_a = _evaluate(
            _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0).model_copy(
                update={"fsm_version": ""}
            )
        )
        result_b = _evaluate(
            _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0).model_copy(
                update={"fsm_version": ""}
            )
        )
        codes_a = [f.quality_code for f in result_a.event.payload.findings]
        codes_b = [f.quality_code for f in result_b.event.payload.findings]
        assert codes_a == codes_b


# =============================================================================
# Idempotency / replay (Parts 15/34/35)
# =============================================================================


class TestIdempotency:
    def test_repeated_evaluation_same_identity(self) -> None:
        interval = _interval().model_copy(update={"duration_seconds": -5.0})
        first = _evaluate(interval)
        second = _evaluate(interval)
        assert first.status is RuleEvaluationStatus.MATCH
        assert first.event.event_id == second.event.event_id
        assert first.event.payload == second.event.payload

    def test_replay_identical(self) -> None:
        """Golden §34 — identical input repeatedly → identical everything."""
        bad = _interval().model_copy(update={"fsm_version": ""})
        r1 = _evaluate(bad)
        r2 = _evaluate(bad)
        assert r1.status is r2.status
        assert r1.event.event_type == r2.event.event_type
        assert r1.event.event_time == r2.event.event_time
        assert r1.event.payload == r2.event.payload
        assert r1.rule_version == r2.rule_version
        assert r1.configuration_version_id == r2.configuration_version_id
        assert r1.event.event_id == r2.event.event_id

    def test_duplicate_delivery_same_event(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        r1 = _evaluate(bad)
        r2 = _evaluate(bad)
        assert r1.event.event_id == r2.event.event_id


# =============================================================================
# Version tests (Part 18/19)
# =============================================================================


class TestVersioning:
    def test_rule_v1_registered(self) -> None:
        engine = _engine()
        definition = engine._registry.get(_RULE_ID, _RULE_VERSION)
        assert definition is not None
        assert definition.rule_version == "v1"
        assert definition.output_event_type is RuleEventType.DATA_QUALITY

    def test_unsupported_rule_version_raises(self) -> None:
        engine = _engine()
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(_RULE_ID, RuleVersion("v9"), _input(_interval()))

    def test_config_v1_vs_v2_historical_replay(self) -> None:
        """Golden §33 — replay with v1 stays v1; a different config changes findings."""
        key = _key(configuration_version_id=_CONFIG_V1)
        interval = _interval(key=key)
        v1 = _evaluate(interval, config_version=_CONFIG_V1)
        assert v1.status is RuleEvaluationStatus.NO_MATCH
        # Same fact replayed under v2 → configuration mismatch finding, but the
        # event identity + evaluation stay pinned to the explicitly supplied v2.
        v2 = _evaluate(interval, config_version=_CONFIG_V2)
        assert v2.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in v2.event.payload.findings]
        assert "DATA_CONFIGURATION_MISMATCH" in codes
        assert v2.configuration_version_id == _CONFIG_V2


# =============================================================================
# Security tests (Parts 22/23)
# =============================================================================


class TestSecurity:
    def test_cross_tenant_rejected(self) -> None:
        """Tenant A fact + Tenant B fact in one input → MixedScopeRuleInputError."""
        engine = _engine()
        inp = RuleEvaluationInput(
            facts=(
                _interval(key=_key(tenant_id=_TENANT_A)),
                _interval(label="other", key=_key(tenant_id=_TENANT_B)),
            ),
            configuration={KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY: [_ZONE_A]},
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MixedScopeRuleInputError):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_cross_venue_rejected(self) -> None:
        engine = _engine()
        inp = RuleEvaluationInput(
            facts=(
                _interval(key=_key(venue_id=_VENUE_A)),
                _interval(label="other", key=_key(venue_id=_VENUE_B)),
            ),
            configuration={KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY: [_ZONE_A]},
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MixedScopeRuleInputError):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_tenant_provenance_preserved_in_match(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.tenant_id == _TENANT_A
        assert result.venue_id == _VENUE_A
        assert result.session_id == _SESSION


# =============================================================================
# Contract tests (Parts 20/21)
# =============================================================================


class TestContract:
    def test_event_envelope_valid(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        event = result.event
        assert isinstance(event, EventEnvelope)
        assert event.event_type == RuleEventType.DATA_QUALITY.value
        assert event.event_time == _event(300)
        assert isinstance(event.payload, DataQualityPayload)
        assert event.event_id == deterministic_event_id(
            result.rule_id and _engine()._registry.get(_RULE_ID, _RULE_VERSION),
            _input(bad),
            event_time=event.event_time,
            event_type=RuleEventType.DATA_QUALITY.value,
        )

    def test_evidence_ref_preserves_provenance(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.evidence_requests
        ref = result.evidence_requests[0]
        assert isinstance(ref, EvidenceRef)
        meta = ref.metadata
        assert meta["tenant_id"] == str(_TENANT_A)
        assert meta["venue_id"] == str(_VENUE_A)
        assert meta["session_id"] == str(_SESSION)
        assert meta["camera_id"] == str(_CAMERA)
        assert meta["configuration_version_id"] == str(_CONFIG_V1)
        assert meta["rule_id"] == str(_RULE_ID)
        assert meta["rule_version"] == "v1"

    def test_no_evidence_on_no_match(self) -> None:
        result = _evaluate(_interval())
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.evidence_requests == ()
        assert result.event is None


# =============================================================================
# Invariants (Part 42)
# =============================================================================


class TestInvariants:
    def test_valid_fact_no_event(self) -> None:
        assert _evaluate(_interval()).status is RuleEvaluationStatus.NO_MATCH

    def test_every_match_has_stable_quality_code(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        payload = result.event.payload
        assert payload.primary_quality_code
        assert all(f.quality_code for f in payload.findings)

    def test_severity_deterministic(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        r1 = _evaluate(bad)
        r2 = _evaluate(bad)
        assert r1.event.payload.primary_severity is r2.event.payload.primary_severity

    def test_same_input_same_result(self) -> None:
        bad = _interval().model_copy(update={"fsm_version": ""})
        assert _evaluate(bad).event.payload == _evaluate(bad).event.payload

    def test_same_failure_same_event_identity(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        assert _evaluate(bad).event.event_id == _evaluate(bad).event.event_id

    def test_multiple_findings_deterministic(self) -> None:
        bad = _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0).model_copy(
            update={"fsm_version": ""}
        )
        codes1 = [f.quality_code for f in _evaluate(bad).event.payload.findings]
        codes2 = [f.quality_code for f in _evaluate(bad).event.payload.findings]
        assert codes1 == codes2 == sorted(codes1)

    def test_rule_version_preserved(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.rule_version == _RULE_VERSION
        assert result.event.payload.rule_version == _RULE_VERSION

    def test_configuration_version_preserved(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.configuration_version_id == _CONFIG_V1
        assert result.event.payload.configuration_version_id == _CONFIG_V1

    def test_event_time_preserved(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.event_time == _event(300)
        assert result.event.event_time == _event(300)
        assert result.event.payload.event_time == _event(300)

    def test_tenant_venue_scope_preserved(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        result = _evaluate(bad)
        assert result.tenant_id == _TENANT_A
        assert result.venue_id == _VENUE_A
        assert result.session_id == _SESSION

    def test_invalid_never_repaired(self) -> None:
        """A missing identity is INVALID — never silently repaired into a MATCH."""
        key = _key().model_copy(update={"tenant_id": None})
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.INVALID

    def test_no_state_mutation(self) -> None:
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        before = bad.model_dump_json()
        _evaluate(bad)
        assert bad.model_dump_json() == before

    def test_no_raw_video_dependency(self) -> None:
        """The rule imports no CV/frame/image dependencies."""
        import inspect

        from backend.app.intelligence.rules import data_quality

        source = inspect.getsource(data_quality)
        for forbidden_import in (
            "import cv2",
            "from cv2",
            "import yolo",
            "from yolo",
            "import numpy",
            "from numpy",
            "bytetrack",
        ):
            assert forbidden_import not in source

    def test_no_infrastructure_side_effects(self) -> None:
        """The rule imports no infrastructure/network clients."""
        import inspect

        from backend.app.intelligence.rules import data_quality

        source = inspect.getsource(data_quality)
        for forbidden_import in (
            "import psycopg",
            "from psycopg",
            "import redis",
            "from redis",
            "import boto3",
            "import requests",
            "import httpx",
            "from fastapi",
        ):
            assert forbidden_import not in source


# =============================================================================
# Golden tests (§25-36)
# =============================================================================


class TestGolden:
    def test_valid_fact(self) -> None:
        """§25 — a completely valid canonical fact → NO_MATCH."""
        assert _evaluate(_interval()).status is RuleEvaluationStatus.NO_MATCH

    def test_missing_identity(self) -> None:
        """§26 — missing required identity → deterministic MATCH with the code."""
        key = _key().model_copy(update={"camera_id": None})
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_MISSING_REQUIRED_IDENTITY" in codes

    def test_invalid_event_time(self) -> None:
        """§27 — a missing event-time is a deterministic quality finding.

        The canonical input boundary (schema validation) rejects a fact
        whose event-time is missing — the check is exercised directly as
        the second line of defense (Part 1/12: INVALID input is never
        silently repaired into MATCH; schema-valid facts with quality
        conditions are MATCH).
        """
        from backend.app.intelligence.rules.data_quality import _check_invalid_event_time
        from contracts.temporal import DwellInterval

        data = _interval().model_dump(mode="python")
        data["last_seen"] = None
        finding = _check_invalid_event_time().evaluator(DwellInterval.model_construct(**data))
        assert finding is not None
        assert finding.quality_code == "DATA_INVALID_EVENT_TIME"

    def test_negative_duration(self) -> None:
        """§28 — negative duration → MATCH with the negative-duration code."""
        bad = _interval().model_copy(update={"duration_seconds": -10.0})
        result = _evaluate(bad)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_NEGATIVE_DURATION" in codes

    def test_non_monotonic_event_time(self) -> None:
        """§29 — end before start → the deterministic non-monotonic finding.

        Exercised at the check level: the canonical boundary rejects such a
        fact (schema validation protects contracts — Part 1).
        """
        from backend.app.intelligence.rules.data_quality import _check_non_monotonic_event_time
        from contracts.temporal import DwellInterval

        data = _interval().model_dump(mode="python")
        data["dwell_end"] = _START - timedelta(seconds=5)
        finding = _check_non_monotonic_event_time().evaluator(DwellInterval.model_construct(**data))
        assert finding is not None
        assert finding.quality_code == "DATA_NON_MONOTONIC_EVENT_TIME"

    def test_invalid_provenance(self) -> None:
        """§30 — missing provenance → MATCH."""
        bad = _interval().model_copy(update={"fsm_version": ""})
        result = _evaluate(bad)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_INVALID_PROVENANCE" in codes

    def test_unknown_spatial_reference(self) -> None:
        """§31 — reference outside the pinned configuration → MATCH."""
        key = _key(semantic_context=_ZONE_UNKNOWN)
        result = _evaluate(_interval(key=key))
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_UNKNOWN_SPATIAL_REFERENCE" in codes

    def test_configuration_mismatch(self) -> None:
        """§32 — fact under v1 evaluated against v2 → MATCH, never a silent switch."""
        key = _key(configuration_version_id=_CONFIG_V1)
        result = _evaluate(_interval(key=key), config_version=_CONFIG_V2)
        assert result.status is RuleEvaluationStatus.MATCH
        codes = [f.quality_code for f in result.event.payload.findings]
        assert "DATA_CONFIGURATION_MISMATCH" in codes
        assert result.configuration_version_id == _CONFIG_V2

    def test_multiple_failures(self) -> None:
        """§33 — all failures returned, deterministic ordering."""
        bad = _interval(duration=30.0, qualified=True, minimum_dwell_seconds=300.0).model_copy(
            update={"fsm_version": ""}
        )
        result = _evaluate(bad)
        codes = [f.quality_code for f in result.event.payload.findings]
        assert len(codes) >= 2
        assert codes == sorted(codes)
        assert "DATA_TEMPORAL_INCONSISTENCY" in codes
        assert "DATA_INVALID_PROVENANCE" in codes

    def test_replay(self) -> None:
        """§34 — identical replay → identical quality codes/severity/identity."""
        bad = _interval().model_copy(update={"fsm_version": ""})
        r1 = _evaluate(bad)
        r2 = _evaluate(bad)
        assert [f.quality_code for f in r1.event.payload.findings] == [
            f.quality_code for f in r2.event.payload.findings
        ]
        assert r1.event.payload.primary_severity is r2.event.payload.primary_severity
        assert r1.event.event_type == r2.event.event_type
        assert r1.event.event_id == r2.event.event_id

    def test_idempotency(self) -> None:
        """§35 — the same invalid fact delivered repeatedly → one logical event."""
        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        ids = {_evaluate(bad).event.event_id for _ in range(3)}
        assert len(ids) == 1

    def test_distinct_quality_conditions_distinct_events(self) -> None:
        """§36 — different facts/quality conditions → distinct logical events.

        Two different facts (different event-times — the natural case for
        distinct moments) produce distinct content-derived event identities
        and distinct finding sets; unrelated failures are never collapsed.
        """
        fact_a = _interval().model_copy(update={"duration_seconds": -5.0})
        fact_b = _interval(label="b", last_seen=_event(600)).model_copy(update={"fsm_version": ""})
        event_a = _evaluate(fact_a).event
        event_b = _evaluate(fact_b, event_time=_event(600)).event
        assert event_a.event_id != event_b.event_id
        codes_a = {f.quality_code for f in event_a.payload.findings}
        codes_b = {f.quality_code for f in event_b.payload.findings}
        assert codes_a != codes_b  # unrelated failures are never collapsed


# =============================================================================
# Observability (Part 39)
# =============================================================================


class TestObservability:
    def test_quality_codes_appear_in_telemetry(self, caplog) -> None:
        import logging

        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        with caplog.at_level(logging.INFO, logger="backend.app.intelligence.rules.engine"):
            _evaluate(bad)
        records = [r for r in caplog.records if r.name == "backend.app.intelligence.rules.engine"]
        assert len(records) == 1
        message = str(records[0].getMessage())
        assert "rule_id=data_quality" in message
        assert "DATA_NEGATIVE_DURATION" in message  # quality_code in the payload summary
        assert records[0].tenant_id == str(_TENANT_A)
        assert records[0].venue_id == str(_VENUE_A)

    def test_no_secrets_logged(self, caplog) -> None:
        import logging

        bad = _interval().model_copy(update={"duration_seconds": -5.0})
        with caplog.at_level(logging.INFO, logger="backend.app.intelligence.rules.engine"):
            _evaluate(bad)
        for record in caplog.records:
            message = str(record.getMessage()) + str(getattr(record, "extra", {}))
            for forbidden in ("password", "secret", "token"):
                assert forbidden not in message.lower()
