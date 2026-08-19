"""Unit tests for Task 16.3 — the deterministic rule evaluation engine.

Covers the Task 16.3 test list (Parts 17-28):

- basic evaluation (threshold evaluator): MATCH / NO_MATCH
- explicit rule-version resolution (v1 never silently executes v2)
- configuration-version selection + preservation
- determinism (repeated evaluation → identical logical result)
- NO_MATCH: no EventEnvelope, no evidence request
- MATCH: canonical event type/time/tenant/venue/session/identity
- invalid input → controlled typed failures (never silent MATCH)
- cross-tenant rejection
- replay (evaluate → Result A; register v2; re-evaluate v1 → same A)
- EventEnvelope / EvidenceRef contract compatibility
- evidence policy (REQUIRED auto-constructs a request; NONE rejects one)
- side-effect freedom (engine is pure)
- exception safety (evaluator failure → typed error, no partial event)
- evaluator registry governance (duplicates rejected, missing rejected)

The ``ThresholdRuleEvaluator`` below is the TEST-ONLY deterministic
evaluator required by Part 17 — it compares a fact-derived value against
a configured threshold. It is NOT a HotelOps business rule.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.intelligence.rules import (
    DuplicateEvaluatorError,
    InvalidRuleEvaluationError,
    InvalidRuleInputError,
    MissingRuleConfigurationError,
    MixedScopeRuleInputError,
    RuleConfigurationMismatchError,
    RuleEvaluationEngine,
    RuleEvaluationExecutionError,
    RuleEvaluatorRegistry,
    RuleRegistry,
    UnsupportedEvaluatorError,
    UnsupportedFactTypeError,
    UnsupportedRuleVersionError,
    deterministic_event_id,
    deterministic_evidence_ref,
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
    new_uuid,
)
from contracts.events import EventEnvelope, EvidenceRef, EvidenceType
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
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TemporalReason,
    TemporalStateKey,
    WaitingInterval,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT_A = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_TENANT_B = TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_V2 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_TRACK = TrackId(uuid.UUID("61000000-0000-0000-0000-000000000001"))

_EVENT_TIME = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_PROCESSED = datetime(2026, 8, 1, 10, 0, 5, tzinfo=UTC)

EVALUATOR = "test_threshold_evaluator.v1"
EVALUATOR_V2 = "test_threshold_evaluator.v2"


# =============================================================================
# TEST-ONLY deterministic evaluator (Task 16.3 Part 17)
# =============================================================================


class ThresholdRuleEvaluator:
    """TEST-ONLY evaluator: value >= configured threshold → MATCH.

    Reads the numeric ``value`` from the first fact (a WaitingInterval's
    duration) and compares it against the ``threshold`` configuration key.
    Deterministic, side-effect free, no I/O, no wall clock.
    """

    evaluator_id = EVALUATOR

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        threshold = float(inp.configuration["threshold"])
        value = float(inp.facts[0].duration_seconds or 0.0)  # type: ignore[union-attr]
        key = inp.facts[0].key
        if value >= threshold:
            event_id = deterministic_event_id(
                rule,
                inp,
                event_time=inp.event_time,
                event_type=rule.output_event_type.value,
            )
            event = EventEnvelope(
                event_id=event_id,
                event_type=rule.output_event_type.value,
                event_time=inp.event_time,
                produced_at=inp.processing_time or inp.event_time,
                source=f"rule:{rule.canonical_identity}",
                payload={
                    "value": value,
                    "threshold": threshold,
                    "rule_version": rule.rule_version,
                },
            )
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.MATCH,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                event=event,
                tenant_id=key.tenant_id,
                venue_id=key.venue_id,
                session_id=key.session_id,
            )
        return RuleEvaluationResult(
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            status=RuleEvaluationStatus.NO_MATCH,
            event_time=inp.event_time,
            configuration_version_id=inp.configuration_version_id,
            tenant_id=key.tenant_id,
            venue_id=key.venue_id,
            session_id=key.session_id,
        )


class ThresholdRuleEvaluatorV2(ThresholdRuleEvaluator):
    """v2 of the test evaluator: threshold is strictly-greater (same rule)."""

    evaluator_id = EVALUATOR_V2

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        result = super().evaluate(rule, inp)
        if result.status is RuleEvaluationStatus.MATCH:
            # v2 semantics: require value STRICTLY greater than threshold.
            threshold = float(inp.configuration["threshold"])
            value = float(inp.facts[0].duration_seconds or 0.0)  # type: ignore[union-attr]
            if value == threshold:
                return RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    status=RuleEvaluationStatus.NO_MATCH,
                    event_time=inp.event_time,
                    configuration_version_id=inp.configuration_version_id,
                    tenant_id=inp.facts[0].key.tenant_id,
                    venue_id=inp.facts[0].key.venue_id,
                    session_id=inp.facts[0].key.session_id,
                )
        return result


# =============================================================================
# Helpers
# =============================================================================


def _definition(
    *,
    rule_id: str = RuleIdentifier.QUEUE_CANDIDATE.value,
    version: str = "v1",
    evaluator: str = EVALUATOR,
    evidence: EvidenceRequirement = EvidenceRequirement.NONE,
    **overrides,
) -> RuleDefinition:
    values: dict = {
        "rule_id": RuleId(rule_id),
        "rule_version": RuleVersion(version),
        "rule_name": "Test threshold rule",
        "description": "Test-only threshold rule (Task 16.3).",
        "enabled": True,
        "input_fact_types": frozenset({FactType.WAITING_INTERVAL}),
        "configuration_requirements": frozenset({"threshold"}),
        "evaluator_id": evaluator,
        "output_event_type": RuleEventType.QUEUE_CANDIDATE,
        "evidence_requirement": evidence,
        "cooldown_policy": CooldownPolicy(enabled=False, duration_seconds=0.0),
        "deterministic_version": TEMPORAL_ENGINE_VERSION,
    }
    values.update(overrides)
    return RuleDefinition(**values)


def _registry(*, evaluators: tuple[str, ...] = (EVALUATOR, EVALUATOR_V2)) -> RuleRegistry:
    return RuleRegistry(supported_evaluators=frozenset(evaluators))


def _evaluator_registry() -> RuleEvaluatorRegistry:
    registry = RuleEvaluatorRegistry()
    registry.register(ThresholdRuleEvaluator())
    registry.register(ThresholdRuleEvaluatorV2())
    return registry


def _engine() -> RuleEvaluationEngine:
    """An engine with queue_candidate:v1 registered and evaluators bound."""
    registry = _registry()
    registry.register(_definition(version="v1"))
    return RuleEvaluationEngine(registry, _evaluator_registry())


def _fact(
    *,
    duration: float = 5.0,
    tenant: TenantId = _TENANT_A,
    config: ConfigurationVersionId = _CONFIG_V1,
) -> WaitingInterval:
    key = TemporalStateKey(
        fsm_kind="waiting",
        tenant_id=tenant,
        venue_id=_VENUE,
        session_id=_SESSION,
        camera_id=_CAMERA,
        configuration_version_id=config,
        track_id=_TRACK,
        semantic_context="zone-queue-a",
    )
    return WaitingInterval(
        interval_id=EventId(new_uuid()),
        fsm_kind="waiting",
        key=key,
        waiting_start=_EVENT_TIME,
        waiting_end=_EVENT_TIME,
        last_seen=_EVENT_TIME,
        duration_seconds=duration,
        qualified=True,
        minimum_waiting_seconds=0.0,
        reason=TemporalReason.EXIT_CONFIRMED,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(
    *,
    rule_version: str = "v1",
    config_version: ConfigurationVersionId = _CONFIG_V1,
    configuration: dict | None = None,
    facts: tuple | None = None,
    processing_time: datetime | None = _PROCESSED,
) -> RuleEvaluationInput:
    config = {"threshold": 3.0} if configuration is None else configuration
    return RuleEvaluationInput(
        facts=facts if facts is not None else (_fact(),),
        configuration=config,
        configuration_version_id=config_version,
        rule_version=RuleVersion(rule_version),
        event_time=_EVENT_TIME,
        processing_time=processing_time,
    )


# =============================================================================
# 17/21/22. Basic evaluation — MATCH / NO_MATCH
# =============================================================================


class TestBasicEvaluation:
    def test_match_when_value_above_threshold(self) -> None:
        engine = _engine()
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(duration=5.0),)),
        )
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.event_type == RuleEventType.QUEUE_CANDIDATE.value
        assert result.event.event_time == _EVENT_TIME
        assert result.tenant_id == _TENANT_A
        assert result.venue_id == _VENUE
        assert result.session_id == _SESSION
        assert result.configuration_version_id == _CONFIG_V1
        assert result.rule_version == RuleVersion("v1")

    def test_no_match_when_value_below_threshold(self) -> None:
        engine = _engine()
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(duration=1.0),)),
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_value_equals_threshold_matches_v1(self) -> None:
        engine = _engine()
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(duration=3.0),)),
        )
        assert result.status is RuleEvaluationStatus.MATCH  # >= threshold

    def test_suppressed_contract_supported(self) -> None:
        # SUPPRESSED is a valid evaluator outcome the engine passes through
        # (cooldown EXECUTION is a later Task 16 step — not implemented).
        class SuppressingEvaluator:
            evaluator_id = "test_suppressing_evaluator.v1"

            def evaluate(self, rule, inp):
                return RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    status=RuleEvaluationStatus.SUPPRESSED,
                    event_time=inp.event_time,
                    configuration_version_id=inp.configuration_version_id,
                    reason="suppressed by test policy",
                )

        evaluators = RuleEvaluatorRegistry()
        evaluators.register(SuppressingEvaluator())
        registry = _registry(evaluators=("test_suppressing_evaluator.v1",))
        registry.register(_definition(evaluator="test_suppressing_evaluator.v1"))
        engine = RuleEvaluationEngine(registry, evaluators)
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(duration=9.0),)),
        )
        assert result.status is RuleEvaluationStatus.SUPPRESSED
        assert result.event is None  # suppressed → no event emitted


# =============================================================================
# 18. Rule version resolution — v1 never silently executes v2
# =============================================================================


class TestRuleVersionResolution:
    def test_exact_version_resolved(self) -> None:
        engine = _engine()  # queue_candidate:v1 already registered
        engine._registry.register(_definition(version="v2", evaluator=EVALUATOR_V2))
        # value == threshold: v1 (>=) matches, v2 (strictly >) does not.
        inp_v1 = _input(rule_version="v1", facts=(_fact(duration=3.0),))
        inp_v2 = _input(rule_version="v2", facts=(_fact(duration=3.0),))
        v1 = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp_v1
        )
        v2 = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v2"), inp_v2
        )
        assert v1.status is RuleEvaluationStatus.MATCH
        assert v1.rule_version == RuleVersion("v1")
        assert v2.status is RuleEvaluationStatus.NO_MATCH
        assert v2.rule_version == RuleVersion("v2")
        # The engine resolved the exact versions — no silent cross-execution.

    def test_unsupported_version_rejected(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v9"),
                _input(),
            )

    def test_rule_version_mismatch_rejected(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        with pytest.raises(RuleConfigurationMismatchError):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(rule_version="v2"),
            )

    def test_missing_evaluator_rejected(self) -> None:
        # The rule registry may reference an evaluator id that has no
        # registered IMPLEMENTATION — evaluation then fails explicitly.
        registry = _registry(evaluators=("missing_evaluator.v1",))
        registry.register(_definition(version="v1", evaluator="missing_evaluator.v1"))
        engine = RuleEvaluationEngine(registry, RuleEvaluatorRegistry())
        with pytest.raises(UnsupportedEvaluatorError, match="not registered"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(),
            )


# =============================================================================
# 19. Configuration version selection + preservation
# =============================================================================


class TestConfigurationVersion:
    def test_configuration_explicitly_selected(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        # Same rule + facts, two different pinned configuration versions.
        r1 = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(config_version=_CONFIG_V1),
        )
        r2 = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(config_version=_CONFIG_V2),
        )
        assert r1.configuration_version_id == _CONFIG_V1
        assert r2.configuration_version_id == _CONFIG_V2
        # Both are reproducible (deterministic event identities per config).
        assert r1.event is not None and r2.event is not None
        assert r1.event.event_id != r2.event.event_id

    def test_missing_configuration_rejected(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        with pytest.raises(MissingRuleConfigurationError, match="threshold"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(configuration={}),
            )


# =============================================================================
# 20/25. Determinism + replay
# =============================================================================


class TestDeterminismAndReplay:
    def test_repeated_evaluation_is_identical(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        inp = _input(facts=(_fact(duration=5.0),))
        r1 = engine.evaluate(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp)
        r2 = engine.evaluate(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp)
        assert r1 == r2
        assert r1.model_dump_json() == r2.model_dump_json()
        assert r1.event is not None and r2.event is not None
        assert r1.event.event_id == r2.event.event_id  # same logical event

    def test_replay_unaffected_by_new_rule_version(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered (EVALUATOR)
        inp = _input(facts=(_fact(duration=5.0),))
        result_a = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp
        )
        serialized_a = result_a.model_dump_json()

        # Publish v2 (with a different evaluator) — v1 evaluation must not
        # change.
        engine._registry.register(_definition(version="v2", evaluator=EVALUATOR_V2))
        result_a_replay = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp
        )
        assert result_a_replay == result_a
        assert result_a_replay.model_dump_json() == serialized_a


# =============================================================================
# 23. Invalid input — controlled typed failures, never silent MATCH
# =============================================================================


class TestInvalidInput:
    def test_non_input_rejected(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        with pytest.raises(InvalidRuleInputError, match="RuleEvaluationInput"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                object(),  # type: ignore[arg-type]
            )

    def test_empty_facts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one canonical fact"):
            _input(facts=())

    def test_naive_event_time_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-naive"):
            RuleEvaluationInput(
                facts=(_fact(),),
                configuration={"threshold": 3.0},
                configuration_version_id=_CONFIG_V1,
                rule_version=RuleVersion("v1"),
                event_time=datetime(2026, 8, 1, 10, 0, 0),
            )

    def test_missing_rule_version_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rule version"):
            RuleEvaluationInput(
                facts=(_fact(),),
                configuration={"threshold": 3.0},
                configuration_version_id=_CONFIG_V1,
                rule_version=RuleVersion("latest"),
                event_time=_EVENT_TIME,
            )

    def test_wrong_fact_type_rejected(self) -> None:
        from contracts.temporal import DwellInterval

        dwell_key = TemporalStateKey(
            fsm_kind="dwell",
            tenant_id=_TENANT_A,
            venue_id=_VENUE,
            session_id=_SESSION,
            camera_id=_CAMERA,
            configuration_version_id=_CONFIG_V1,
            track_id=_TRACK,
            semantic_context="zone-queue-a",
        )
        dwell = DwellInterval(
            interval_id=EventId(new_uuid()),
            fsm_kind="dwell",
            key=dwell_key,
            dwell_start=_EVENT_TIME,
            dwell_end=_EVENT_TIME,
            last_seen=_EVENT_TIME,
            duration_seconds=5.0,
            qualified=True,
            minimum_dwell_seconds=0.0,
            reason=TemporalReason.EXIT_CONFIRMED,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        engine = _engine()  # queue_candidate:v1 registered
        with pytest.raises(UnsupportedFactTypeError, match="does not declare fact type"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=(dwell,)),
            )


# =============================================================================
# 24. Cross-tenant rejection
# =============================================================================


class TestCrossTenant:
    def test_cross_tenant_facts_rejected(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        mixed = (_fact(tenant=_TENANT_A), _fact(tenant=_TENANT_B))
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=mixed),
            )

    def test_result_provenance_is_scope_bound(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(tenant=_TENANT_A),)),
        )
        assert result.tenant_id == _TENANT_A  # never another tenant


# =============================================================================
# 26/27. EventEnvelope + EvidenceRef contract compatibility
# =============================================================================


class TestEventAndEvidenceContracts:
    def test_match_event_envelope_serializes(self) -> None:
        # queue_candidate:v1 with REQUIRED evidence (the default engine's v1
        # has evidence_requirement=none, so use a dedicated registry).
        registry = _registry()
        registry.register(_definition(version="v1", evidence=EvidenceRequirement.REQUIRED))
        engine = RuleEvaluationEngine(registry, _evaluator_registry())
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(duration=5.0),)),
        )
        assert result.status is RuleEvaluationStatus.MATCH
        envelope = result.event
        assert envelope is not None
        # Contract compatibility: envelope survives serialize → deserialize.
        restored = EventEnvelope.model_validate(envelope.model_dump(mode="json"))
        assert restored == envelope
        assert envelope.schema_version == "1.0"

    def test_required_evidence_auto_constructed(self) -> None:
        registry = _registry()
        registry.register(_definition(version="v1", evidence=EvidenceRequirement.REQUIRED))
        engine = RuleEvaluationEngine(registry, _evaluator_registry())
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            RuleVersion("v1"),
            _input(facts=(_fact(duration=5.0),)),
        )
        assert result.status is RuleEvaluationStatus.MATCH
        assert len(result.evidence_requests) == 1
        ref = result.evidence_requests[0]
        assert isinstance(ref, EvidenceRef)
        assert ref.ref_type is EvidenceType.VIDEO_CLIP
        # The request preserves provenance.
        assert ref.metadata is not None
        assert ref.metadata["session_id"] == str(_SESSION)
        assert ref.metadata["configuration_version_id"] == str(_CONFIG_V1)
        assert ref.metadata["rule_id"] == RuleIdentifier.QUEUE_CANDIDATE.value
        # Evidence request survives serialize → deserialize.
        assert EvidenceRef.model_validate(ref.model_dump(mode="json")) == ref

    def test_none_evidence_policy_rejects_requests(self) -> None:
        class BadEvaluator:
            evaluator_id = "test_bad_evidence_evaluator.v1"

            def evaluate(self, rule, inp):
                event_id = deterministic_event_id(
                    rule, inp, event_time=inp.event_time, event_type=rule.output_event_type.value
                )
                event = EventEnvelope(
                    event_id=event_id,
                    event_type=rule.output_event_type.value,
                    event_time=inp.event_time,
                    produced_at=inp.processing_time or inp.event_time,
                    source=f"rule:{rule.canonical_identity}",
                    payload={"value": 5.0},
                )
                return RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    status=RuleEvaluationStatus.MATCH,
                    event_time=inp.event_time,
                    configuration_version_id=inp.configuration_version_id,
                    event=event,
                    evidence_requests=(deterministic_evidence_ref(rule, inp, event_id=event_id),),
                    tenant_id=inp.facts[0].key.tenant_id,
                    venue_id=inp.facts[0].key.venue_id,
                    session_id=inp.facts[0].key.session_id,
                )

        evaluators = RuleEvaluatorRegistry()
        evaluators.register(BadEvaluator())
        engine = RuleEvaluationEngine(
            _registry(evaluators=("test_bad_evidence_evaluator.v1",)),
            evaluators,
        )
        engine._registry.register(
            _definition(
                version="v1",
                evaluator="test_bad_evidence_evaluator.v1",
                evidence=EvidenceRequirement.NONE,
            )
        )
        with pytest.raises(InvalidRuleEvaluationError, match="evidence_requirement=none"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=(_fact(duration=5.0),)),
            )

    def test_deterministic_event_id_is_stable(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        inp = _input(facts=(_fact(duration=5.0),))
        rule = engine._registry.resolve(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1")
        )
        expected = deterministic_event_id(
            rule, inp, event_time=inp.event_time, event_type=rule.output_event_type.value
        )
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp
        )
        assert result.event is not None
        assert result.event.event_id == expected


# =============================================================================
# 15. Exception safety — evaluator failure → typed error, no partial event
# =============================================================================


class TestExceptionSafety:
    def test_evaluator_exception_becomes_typed_error(self) -> None:
        class ExplodingEvaluator:
            evaluator_id = "test_exploding_evaluator.v1"

            def evaluate(self, rule, inp):
                raise RuntimeError("boom")

        evaluators = RuleEvaluatorRegistry()
        evaluators.register(ExplodingEvaluator())
        engine = RuleEvaluationEngine(
            _registry(evaluators=("test_exploding_evaluator.v1",)),
            evaluators,
        )
        engine._registry.register(
            _definition(version="v1", evaluator="test_exploding_evaluator.v1")
        )
        with pytest.raises(RuleEvaluationExecutionError, match="unexpected"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=(_fact(duration=5.0),)),
            )

    def test_non_deterministic_event_id_rejected(self) -> None:
        class RandomIdEvaluator:
            evaluator_id = "test_random_id_evaluator.v1"

            def evaluate(self, rule, inp):
                event = EventEnvelope(
                    event_id=EventId(new_uuid()),  # NON-deterministic!
                    event_type=rule.output_event_type.value,
                    event_time=inp.event_time,
                    produced_at=inp.processing_time or inp.event_time,
                    source=f"rule:{rule.canonical_identity}",
                    payload={"value": 5.0},
                )
                return RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    status=RuleEvaluationStatus.MATCH,
                    event_time=inp.event_time,
                    configuration_version_id=inp.configuration_version_id,
                    event=event,
                    tenant_id=inp.facts[0].key.tenant_id,
                    venue_id=inp.facts[0].key.venue_id,
                    session_id=inp.facts[0].key.session_id,
                )

        evaluators = RuleEvaluatorRegistry()
        evaluators.register(RandomIdEvaluator())
        engine = RuleEvaluationEngine(
            _registry(evaluators=("test_random_id_evaluator.v1",)),
            evaluators,
        )
        engine._registry.register(
            _definition(version="v1", evaluator="test_random_id_evaluator.v1")
        )
        with pytest.raises(InvalidRuleEvaluationError, match="non-deterministic"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=(_fact(duration=5.0),)),
            )

    def test_wrong_event_type_rejected(self) -> None:
        class WrongTypeEvaluator:
            evaluator_id = "test_wrong_type_evaluator.v1"

            def evaluate(self, rule, inp):
                event_id = deterministic_event_id(
                    rule, inp, event_time=inp.event_time, event_type=rule.output_event_type.value
                )
                event = EventEnvelope(
                    event_id=event_id,
                    event_type="some_other_event",  # wrong type
                    event_time=inp.event_time,
                    produced_at=inp.processing_time or inp.event_time,
                    source=f"rule:{rule.canonical_identity}",
                    payload={"value": 5.0},
                )
                return RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    status=RuleEvaluationStatus.MATCH,
                    event_time=inp.event_time,
                    configuration_version_id=inp.configuration_version_id,
                    event=event,
                    tenant_id=inp.facts[0].key.tenant_id,
                    venue_id=inp.facts[0].key.venue_id,
                    session_id=inp.facts[0].key.session_id,
                )

        evaluators = RuleEvaluatorRegistry()
        evaluators.register(WrongTypeEvaluator())
        engine = RuleEvaluationEngine(
            _registry(evaluators=("test_wrong_type_evaluator.v1",)),
            evaluators,
        )
        engine._registry.register(
            _definition(version="v1", evaluator="test_wrong_type_evaluator.v1")
        )
        with pytest.raises(InvalidRuleEvaluationError, match="event type"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=(_fact(duration=5.0),)),
            )

    def test_wrong_rule_identity_rejected(self) -> None:
        class WrongIdentityEvaluator:
            evaluator_id = "test_wrong_identity_evaluator.v1"

            def evaluate(self, rule, inp):
                # Reports a DIFFERENT rule's identity.
                return RuleEvaluationResult(
                    rule_id=RuleId("other_rule"),
                    rule_version=rule.rule_version,
                    status=RuleEvaluationStatus.NO_MATCH,
                    event_time=inp.event_time,
                    configuration_version_id=inp.configuration_version_id,
                )

        evaluators = RuleEvaluatorRegistry()
        evaluators.register(WrongIdentityEvaluator())
        engine = RuleEvaluationEngine(
            _registry(evaluators=("test_wrong_identity_evaluator.v1",)),
            evaluators,
        )
        engine._registry.register(
            _definition(version="v1", evaluator="test_wrong_identity_evaluator.v1")
        )
        with pytest.raises(InvalidRuleEvaluationError, match="another rule"):
            engine.evaluate(
                RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
                RuleVersion("v1"),
                _input(facts=(_fact(duration=5.0),)),
            )


# =============================================================================
# 2. Evaluator registry governance
# =============================================================================


class TestEvaluatorRegistry:
    def test_duplicate_evaluator_rejected(self) -> None:
        registry = RuleEvaluatorRegistry()
        registry.register(ThresholdRuleEvaluator())
        with pytest.raises(DuplicateEvaluatorError, match="already registered"):
            registry.register(ThresholdRuleEvaluator())

    def test_resolve_missing_evaluator(self) -> None:
        registry = RuleEvaluatorRegistry()
        with pytest.raises(UnsupportedEvaluatorError, match="not registered"):
            registry.resolve("missing")

    def test_list_is_deterministic(self) -> None:
        registry = RuleEvaluatorRegistry()
        registry.register(ThresholdRuleEvaluatorV2())
        registry.register(ThresholdRuleEvaluator())
        ids = [e.evaluator_id for e in registry.list()]
        assert ids == sorted(ids)


# =============================================================================
# 28. Side-effect freedom
# =============================================================================


class TestSideEffectFreedom:
    def test_evaluation_is_pure(self) -> None:
        engine = _engine()  # queue_candidate:v1 registered
        inp = _input(facts=(_fact(duration=5.0),))
        before = inp.model_dump_json()
        result = engine.evaluate(
            RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"), inp
        )
        # The input is unchanged; the result is a fresh object; no global
        # state was touched (registry contents unchanged).
        assert inp.model_dump_json() == before
        assert result.event is not None
        assert engine._registry.has(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"))
        assert len(engine._evaluators.list()) == 2  # unchanged
