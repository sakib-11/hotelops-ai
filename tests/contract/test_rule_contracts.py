"""Contract tests for the Task 16.1 deterministic rule registry contracts.

Covers (Task 16.1 Part 16 / Part 17):

- serialization round-trips: valid object → serialize → deserialize →
  compare (byte-identical logical result);
- invalid payloads are rejected (missing metadata, invalid versions,
  empty fact types, backwards/naive timestamps, invalid results);
- Task 4 contract compatibility: ``RuleEvaluationResult`` carries a real
  canonical ``EventEnvelope`` and real canonical ``EvidenceRef`` requests
  — nothing is duplicated, and the Task 4 contracts serialize unchanged;
- controlled vocabularies (rule identity, event types, fact types) are
  stable and never free-form.

All fixtures use the REAL canonical contracts with fixed deterministic
IDs so round-trip comparisons are byte-exact.
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
    RuleVersion,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
    new_uuid,
)
from contracts.events import EventEnvelope, EvidenceRef, EvidenceType
from contracts.geometry import CoordinateSpace
from contracts.rules import (
    CooldownPolicy,
    EvidenceRequirement,
    FactType,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
    TemporalFact,
    validate_rule_version,
)
from contracts.spatial import SpatialPointModel, SpatialPointPolicy
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    DwellInterval,
    MovementMeasurement,
    OccupancySnapshot,
    TemporalReason,
    TemporalStateKey,
    TemporalTransition,
    WaitingInterval,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_TRACK = TrackId(uuid.UUID("60000000-0000-0000-0000-000000000001"))

_EVENT_TIME = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_PRODUCED = datetime(2026, 8, 1, 10, 0, 5, tzinfo=UTC)

EVALUATOR = "queue_candidate_evaluator.v1"


def _key(fsm_kind: str) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind=fsm_kind,
        tenant_id=_TENANT,
        venue_id=_VENUE,
        session_id=_SESSION,
        camera_id=_CAMERA,
        configuration_version_id=_CONFIG,
        track_id=_TRACK,
        semantic_context="zone-queue-a",
    )


def _definition(**overrides) -> RuleDefinition:
    values: dict = {
        "rule_id": RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
        "rule_version": RuleVersion("v1"),
        "rule_name": "Queue candidate",
        "description": "Detects a queue candidate in a configured waiting context.",
        "input_fact_types": frozenset({FactType.WAITING_INTERVAL}),
        "configuration_requirements": frozenset({"waiting_qualification_seconds"}),
        "evaluator_id": EVALUATOR,
        "output_event_type": RuleEventType.QUEUE_CANDIDATE,
        "evidence_requirement": EvidenceRequirement.REQUIRED,
        "deterministic_version": TEMPORAL_ENGINE_VERSION,
    }
    values.update(overrides)
    return RuleDefinition(**values)


def _waiting_interval() -> WaitingInterval:
    return WaitingInterval(
        interval_id=EventId(new_uuid()),
        fsm_kind="waiting",
        key=_key("waiting"),
        waiting_start=_EVENT_TIME,
        waiting_end=_EVENT_TIME,
        last_seen=_EVENT_TIME,
        duration_seconds=0.0,
        qualified=True,
        minimum_waiting_seconds=0.0,
        reason=TemporalReason.EXIT_CONFIRMED,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _event_envelope() -> EventEnvelope[dict]:
    return EventEnvelope(
        event_id=EventId(new_uuid()),
        event_type=RuleEventType.QUEUE_CANDIDATE.value,
        event_time=_EVENT_TIME,
        produced_at=_PRODUCED,
        source="rule:queue_candidate:v1",
        payload={"reason": "waiting qualified"},
    )


def _evidence_ref() -> EvidenceRef:
    return EvidenceRef(
        ref_id=EvidenceId(new_uuid()),
        ref_type=EvidenceType.VIDEO_CLIP,
        ref_uri="s3://evidence/queue_candidate/clip.mp4",
        event_id=EventId(new_uuid()),
        event_time=_EVENT_TIME,
        video_session_id=_SESSION,
        metadata={"session_id": str(_SESSION)},
    )


# =============================================================================
# Controlled vocabularies (Parts 4 / 9)
# =============================================================================


class TestControlledVocabularies:
    def test_rule_identifiers_are_stable(self) -> None:
        assert RuleIdentifier.QUEUE_CANDIDATE.value == "queue_candidate"
        assert RuleIdentifier.OCCUPANCY_SESSION.value == "occupancy_session"
        # Rule ids are centralized — never free-form strings in the app.
        assert len(RuleIdentifier) == 6

    def test_event_types_are_controlled(self) -> None:
        # Every rule output event type is a controlled enum member and is
        # consistent with the Task 4 EventEnvelope event_type convention.
        for event_type in RuleEventType:
            assert len(event_type.value) >= 1
            assert isinstance(event_type.value, str)

    def test_fact_types_mirror_task_15_facts(self) -> None:
        assert FactType.TEMPORAL_TRANSITION.value == "temporal_transition"
        assert FactType.DWELL_INTERVAL.value == "dwell_interval"
        assert FactType.OCCUPANCY_SNAPSHOT.value == "occupancy_snapshot"
        assert FactType.MOVEMENT_MEASUREMENT.value == "movement_measurement"
        assert FactType.MOVEMENT_CLASSIFICATION_TRANSITION.value == (
            "movement_classification_transition"
        )
        assert FactType.WAITING_INTERVAL.value == "waiting_interval"

    def test_rule_version_validation(self) -> None:
        # Explicit dotted numeric versions are valid (v1, v2, v1.2, ...).
        for good in ("v1", "v2", "v1.2", "v10", "v1.2.3"):
            assert validate_rule_version(RuleVersion(good)) == RuleVersion(good)
        # Anything non-explicit is rejected: missing v-prefix, non-numeric
        # segments, free-form labels, empty strings.
        for bad in ("1", "V1", "", "v", "v1-rc", "v1.2-rc1", "queue_candidate", "latest"):
            with pytest.raises(ValueError, match="rule version"):
                validate_rule_version(RuleVersion(bad))


# =============================================================================
# RuleDefinition — construction + serialization (Parts 3 / 16)
# =============================================================================


class TestRuleDefinitionSerialization:
    def test_round_trip_is_byte_identical(self) -> None:
        rule = _definition()
        data = rule.model_dump(mode="json")
        restored = RuleDefinition.model_validate(data)
        assert restored == rule
        assert restored.canonical_identity == "queue_candidate:v1"

    def test_canonical_identity_distinguishes_versions(self) -> None:
        v1 = _definition()
        v2 = _definition(rule_version=RuleVersion("v2"))
        assert v1.canonical_identity == "queue_candidate:v1"
        assert v2.canonical_identity == "queue_candidate:v2"
        assert v1.canonical_identity != v2.canonical_identity
        assert v1.rule_id == v2.rule_id  # same rule, explicit version change

    def test_frozen_definition_is_immutable(self) -> None:
        rule = _definition()
        # In-place mutation is impossible on a frozen pydantic model.
        with pytest.raises(ValidationError):
            rule.rule_name = "mutated"  # type: ignore[misc]  # frozen model
        with pytest.raises(ValidationError):
            rule.rule_version = RuleVersion("v2")  # type: ignore[misc]  # frozen model
        # The definition still serializes unchanged.
        assert rule.rule_name == "Queue candidate"
        assert rule.rule_version == RuleVersion("v1")

    def test_missing_metadata_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _definition(rule_name="")
        with pytest.raises(ValidationError):
            _definition(description="")
        with pytest.raises(ValidationError):
            _definition(evaluator_id="")

    def test_invalid_rule_version_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rule version"):
            _definition(rule_version=RuleVersion("V1"))

    def test_empty_fact_types_rejected(self) -> None:
        # The contract rejects an empty fact-type set at construction.
        with pytest.raises(ValidationError):
            _definition(input_fact_types=frozenset())

    def test_invalid_fact_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _definition(input_fact_types=frozenset({"not_a_fact_type"}))  # type: ignore[arg-type]

    def test_invalid_event_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _definition(output_event_type="free_form_event")  # type: ignore[arg-type]

    def test_cooldown_policy_validation(self) -> None:
        CooldownPolicy(enabled=False, duration_seconds=0.0)
        CooldownPolicy(enabled=True, duration_seconds=5.0)
        with pytest.raises(ValidationError, match="cooldown"):
            CooldownPolicy(enabled=True, duration_seconds=0.0)


# =============================================================================
# RuleEvaluationResult — EventEnvelope / EvidenceRef compatibility (Parts 8 / 17)
# =============================================================================


class TestRuleEvaluationResultCompat:
    def test_match_carries_canonical_event_envelope(self) -> None:
        envelope = _event_envelope()
        result = RuleEvaluationResult(
            rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            rule_version=RuleVersion("v1"),
            status=RuleEvaluationStatus.MATCH,
            event_time=_EVENT_TIME,
            configuration_version_id=_CONFIG,
            event=envelope,
            tenant_id=_TENANT,
            venue_id=_VENUE,
            session_id=_SESSION,
        )
        # Round-trip: the Task 4 EventEnvelope is preserved unchanged.
        data = result.model_dump(mode="json")
        restored = RuleEvaluationResult.model_validate(data)
        assert restored == result
        assert restored.event == envelope
        assert restored.event.event_type == RuleEventType.QUEUE_CANDIDATE.value

    def test_match_carries_evidence_ref_requests(self) -> None:
        ref = _evidence_ref()
        result = RuleEvaluationResult(
            rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            rule_version=RuleVersion("v1"),
            status=RuleEvaluationStatus.MATCH,
            event_time=_EVENT_TIME,
            configuration_version_id=_CONFIG,
            event=_event_envelope(),
            evidence_requests=(ref,),
        )
        restored = RuleEvaluationResult.model_validate(result.model_dump(mode="json"))
        assert restored == result
        assert restored.evidence_requests == (ref,)
        assert isinstance(restored.evidence_requests[0], EvidenceRef)

    def test_no_match_carries_no_envelope(self) -> None:
        result = RuleEvaluationResult(
            rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            rule_version=RuleVersion("v1"),
            status=RuleEvaluationStatus.NO_MATCH,
            event_time=_EVENT_TIME,
            configuration_version_id=_CONFIG,
        )
        assert result.event is None
        assert result.model_validate(result.model_dump(mode="json")) == result

    def test_match_without_envelope_rejected(self) -> None:
        with pytest.raises(ValidationError, match="MATCH"):
            RuleEvaluationResult(
                rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                rule_version=RuleVersion("v1"),
                status=RuleEvaluationStatus.MATCH,
                event_time=_EVENT_TIME,
                configuration_version_id=_CONFIG,
            )

    def test_non_match_with_envelope_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only a MATCH"):
            RuleEvaluationResult(
                rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                rule_version=RuleVersion("v1"),
                status=RuleEvaluationStatus.NO_MATCH,
                event_time=_EVENT_TIME,
                configuration_version_id=_CONFIG,
                event=_event_envelope(),
            )

    def test_invalid_requires_reason(self) -> None:
        with pytest.raises(ValidationError, match="INVALID"):
            RuleEvaluationResult(
                rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                rule_version=RuleVersion("v1"),
                status=RuleEvaluationStatus.INVALID,
                event_time=_EVENT_TIME,
                configuration_version_id=_CONFIG,
            )
        ok = RuleEvaluationResult(
            rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            rule_version=RuleVersion("v1"),
            status=RuleEvaluationStatus.INVALID,
            event_time=_EVENT_TIME,
            configuration_version_id=_CONFIG,
            reason="missing configuration",
        )
        assert ok.model_validate(ok.model_dump(mode="json")) == ok

    def test_event_envelope_contract_unchanged(self) -> None:
        # Task 4 compatibility: the envelope is a plain canonical
        # EventEnvelope — schema_version, event_time, produced_at intact.
        envelope = _event_envelope()
        assert envelope.schema_version == SCHEMA_VERSION
        assert envelope.event_time == _EVENT_TIME
        assert envelope.produced_at == _PRODUCED
        # Serializes/deserializes identically as a standalone Task 4 object.
        assert EventEnvelope.model_validate(envelope.model_dump(mode="json")) == envelope

    def test_evidence_ref_contract_unchanged(self) -> None:
        ref = _evidence_ref()
        assert ref.schema_version == SCHEMA_VERSION
        assert EvidenceRef.model_validate(ref.model_dump(mode="json")) == ref


# =============================================================================
# RuleEvaluationInput — explicit versioned inputs (Part 10)
# =============================================================================


class TestRuleEvaluationInput:
    def test_round_trip_preserves_facts(self) -> None:
        fact = _waiting_interval()
        inp = RuleEvaluationInput(
            facts=(fact,),
            configuration={"waiting_qualification_seconds": 3.0},
            configuration_version_id=_CONFIG,
            rule_version=RuleVersion("v1"),
            event_time=_EVENT_TIME,
        )
        restored = RuleEvaluationInput.model_validate(inp.model_dump(mode="json"))
        assert restored == inp
        assert restored.facts == (fact,)
        assert isinstance(restored.facts[0], WaitingInterval)

    def test_all_canonical_fact_types_round_trip(self) -> None:
        key = _key
        facts: tuple[TemporalFact, ...] = (
            TemporalTransition(
                transition_id=EventId(new_uuid()),
                fsm_kind="presence",
                key=key("presence"),
                from_state="absent",
                to_state="present",
                event_kind="present",
                reason=TemporalReason.ENTER_CONFIRMED,
                observation_frame_id=new_uuid(),
                event_time=_EVENT_TIME,
                processing_time=_PRODUCED,
                configuration_version_id=_CONFIG,
                fsm_version=TEMPORAL_ENGINE_VERSION,
            ),
            DwellInterval(
                interval_id=EventId(new_uuid()),
                fsm_kind="dwell",
                key=key("dwell"),
                dwell_start=_EVENT_TIME,
                dwell_end=_EVENT_TIME,
                last_seen=_EVENT_TIME,
                duration_seconds=0.0,
                qualified=True,
                minimum_dwell_seconds=0.0,
                reason=TemporalReason.EXIT_CONFIRMED,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            ),
            OccupancySnapshot(
                snapshot_id=EventId(new_uuid()),
                fsm_kind="occupancy",
                key=key("occupancy"),
                event_time=_EVENT_TIME,
                previous_count=0,
                delta=1,
                occupancy_count=1,
                occupied_tracks=(),
                source_transition_id=EventId(new_uuid()),
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            ),
            MovementMeasurement(
                measurement_id=EventId(new_uuid()),
                fsm_kind="movement",
                key=key("movement"),
                previous_position=SpatialPointModel(
                    x=0.1,
                    y=0.1,
                    coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
                    policy=SpatialPointPolicy.FOOTPOINT,
                ),
                current_position=SpatialPointModel(
                    x=0.2,
                    y=0.1,
                    coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
                    policy=SpatialPointPolicy.FOOTPOINT,
                ),
                previous_event_time=_EVENT_TIME,
                event_time=_PRODUCED,
                distance=0.1,
                time_delta_seconds=5.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            ),
            _waiting_interval(),
        )
        inp = RuleEvaluationInput(
            facts=facts,
            configuration={},
            configuration_version_id=_CONFIG,
            rule_version=RuleVersion("v1"),
            event_time=_EVENT_TIME,
        )
        restored = RuleEvaluationInput.model_validate(inp.model_dump(mode="json"))
        assert restored == inp
        assert len(restored.facts) == 5

    def test_empty_facts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one canonical fact"):
            RuleEvaluationInput(
                facts=(),
                configuration={},
                configuration_version_id=_CONFIG,
                rule_version=RuleVersion("v1"),
                event_time=_EVENT_TIME,
            )

    def test_naive_event_time_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-naive"):
            RuleEvaluationInput(
                facts=(_waiting_interval(),),
                configuration={},
                configuration_version_id=_CONFIG,
                rule_version=RuleVersion("v1"),
                event_time=datetime(2026, 8, 1, 10, 0, 0),
            )

    def test_invalid_rule_version_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rule version"):
            RuleEvaluationInput(
                facts=(_waiting_interval(),),
                configuration={},
                configuration_version_id=_CONFIG,
                rule_version=RuleVersion("latest"),  # explicit versions only
                event_time=_EVENT_TIME,
            )

    def test_unknown_fields_rejected(self) -> None:
        # extra="forbid" — a rule input with stray fields is rejected.
        data = RuleEvaluationInput(
            facts=(_waiting_interval(),),
            configuration={},
            configuration_version_id=_CONFIG,
            rule_version=RuleVersion("v1"),
            event_time=_EVENT_TIME,
        ).model_dump()
        data["fabricated_field"] = True
        with pytest.raises(ValidationError):
            RuleEvaluationInput.model_validate(data)
