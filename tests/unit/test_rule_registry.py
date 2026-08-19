"""Unit tests for the Task 16.1 deterministic rule registry.

Covers the Task 16.1 Part 15 list:

1. register valid rule
2. retrieve rule
3. list rules
4. duplicate registration rejected
5. unknown rule rejected
6. unknown version rejected
7. invalid definition rejected
8. immutable metadata
9. deterministic rule identity
10. invalid configuration rejected
11. invalid input rejected
12. canonical EventEnvelope compatibility (see test_rule_contracts.py)
13. canonical EvidenceRef compatibility (see test_rule_contracts.py)

plus: the unsupported-evaluator rejection, whole-registry ``validate()``,
and the deterministic input boundary (fact types / configuration
requirements). No business rules are implemented or tested here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.intelligence.rules import (
    DuplicateRuleError,
    InvalidRuleDefinitionError,
    InvalidRuleInputError,
    MissingRuleConfigurationError,
    RuleConfigurationMismatchError,
    RuleRegistry,
    UnknownRuleError,
    UnsupportedEvaluatorError,
    UnsupportedFactTypeError,
    UnsupportedRuleVersionError,
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
from contracts.rules import (
    FactType,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TemporalReason,
    TemporalStateKey,
    WaitingInterval,
)

_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_TRACK = TrackId(uuid.UUID("60000000-0000-0000-0000-000000000001"))

_EVENT_TIME = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)

EVALUATOR = "queue_candidate_evaluator.v1"


def _definition(
    *,
    rule_id: str = RuleIdentifier.QUEUE_CANDIDATE.value,
    version: str = "v1",
    evaluator: str = EVALUATOR,
    **overrides,
) -> RuleDefinition:
    values: dict = {
        "rule_id": RuleId(rule_id),
        "rule_version": RuleVersion(version),
        "rule_name": "Queue candidate",
        "description": "Detects a queue candidate in a configured waiting context.",
        "input_fact_types": frozenset({FactType.WAITING_INTERVAL}),
        "configuration_requirements": frozenset({"waiting_qualification_seconds"}),
        "evaluator_id": evaluator,
        "output_event_type": RuleEventType.QUEUE_CANDIDATE,
        "deterministic_version": TEMPORAL_ENGINE_VERSION,
    }
    values.update(overrides)
    return RuleDefinition(**values)


def _registry() -> RuleRegistry:
    return RuleRegistry(supported_evaluators=frozenset({EVALUATOR}))


def _waiting_fact() -> WaitingInterval:
    key = TemporalStateKey(
        fsm_kind="waiting",
        tenant_id=_TENANT,
        venue_id=_VENUE,
        session_id=_SESSION,
        camera_id=_CAMERA,
        configuration_version_id=_CONFIG,
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
        duration_seconds=0.0,
        qualified=True,
        minimum_waiting_seconds=0.0,
        reason=TemporalReason.EXIT_CONFIRMED,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(
    *,
    rule_version: str = "v1",
    configuration: dict | None = None,
    facts: tuple | None = None,
) -> RuleEvaluationInput:
    config = {"waiting_qualification_seconds": 3.0} if configuration is None else configuration
    return RuleEvaluationInput(
        facts=facts if facts is not None else (_waiting_fact(),),
        configuration=config,
        configuration_version_id=_CONFIG,
        rule_version=RuleVersion(rule_version),
        event_time=_EVENT_TIME,
    )


# =============================================================================
# 1/2/3. Register, retrieve, list
# =============================================================================


class TestRegisterAndLookup:
    def test_register_valid_rule(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        assert registry.has(rule.rule_id, rule.rule_version)
        assert registry.get(rule.rule_id, rule.rule_version) is rule

    def test_retrieve_rule(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        assert registry.resolve(rule.rule_id, rule.rule_version) == rule
        assert registry.get(rule.rule_id, rule.rule_version) == rule

    def test_list_rules_deterministic_order(self) -> None:
        registry = _registry()
        registry.register(_definition(rule_id="b_rule", version="v1"))
        registry.register(_definition(rule_id="a_rule", version="v2"))
        registry.register(_definition(rule_id="a_rule", version="v1"))
        identities = [rule.canonical_identity for rule in registry.list()]
        assert identities == ["a_rule:v1", "a_rule:v2", "b_rule:v1"]
        # Deterministic: repeated calls yield the same order.
        assert identities == [rule.canonical_identity for rule in registry.list()]

    def test_registry_supports_many_rules(self) -> None:
        registry = _registry()
        for index in range(100):
            registry.register(_definition(rule_id=f"rule_{index:03d}", version="v1"))
        assert len(registry.list()) == 100
        # O(1) lookup remains exact.
        assert registry.has(RuleId("rule_099"), RuleVersion("v1"))


# =============================================================================
# 4. Duplicate registration rejected
# =============================================================================


class TestDuplicateRejection:
    def test_duplicate_registration_rejected(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        with pytest.raises(DuplicateRuleError, match="already registered"):
            registry.register(_definition())  # same (rule_id, version)
        # The original is preserved — nothing was overwritten.
        assert registry.resolve(rule.rule_id, rule.rule_version) is rule

    def test_same_rule_new_version_allowed(self) -> None:
        registry = _registry()
        registry.register(_definition(version="v1"))
        registry.register(_definition(version="v2"))
        assert registry.has(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"))
        assert registry.has(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v2"))


# =============================================================================
# 5/6. Unknown rule / unknown version rejected
# =============================================================================


class TestUnknownLookup:
    def test_unknown_rule_rejected(self) -> None:
        registry = _registry()
        with pytest.raises(UnknownRuleError, match="unknown rule"):
            registry.resolve(RuleId("no_such_rule"), RuleVersion("v1"))
        assert registry.get(RuleId("no_such_rule"), RuleVersion("v1")) is None
        assert not registry.has(RuleId("no_such_rule"), RuleVersion("v1"))

    def test_unknown_version_rejected(self) -> None:
        registry = _registry()
        registry.register(_definition(version="v1"))
        with pytest.raises(UnsupportedRuleVersionError, match="no registered version"):
            registry.resolve(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v9"))


# =============================================================================
# 7. Invalid definition rejected
# =============================================================================


class TestInvalidDefinition:
    def test_non_definition_rejected(self) -> None:
        registry = _registry()
        with pytest.raises(InvalidRuleDefinitionError, match="RuleDefinition"):
            registry.register("not a rule")  # type: ignore[arg-type]

    def test_unsupported_evaluator_rejected(self) -> None:
        registry = _registry()  # supports only EVALUATOR
        with pytest.raises(UnsupportedEvaluatorError, match="unsupported evaluator"):
            registry.register(_definition(evaluator="future_evaluator.v9"))

    def test_missing_fact_types_rejected(self) -> None:
        registry = _registry()
        # model_construct bypasses pydantic so the REGISTRY's own guard fires
        # (a definition with no declared fact types must never register).
        bad = RuleDefinition.model_construct(
            rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
            rule_version=RuleVersion("v1"),
            rule_name="Queue candidate",
            description="desc",
            input_fact_types=frozenset(),
            configuration_requirements=frozenset({"waiting_qualification_seconds"}),
            evaluator_id=EVALUATOR,
            output_event_type=RuleEventType.QUEUE_CANDIDATE,
            deterministic_version=TEMPORAL_ENGINE_VERSION,
        )
        with pytest.raises(InvalidRuleDefinitionError, match="at least one input fact type"):
            registry.register(bad)

    def test_empty_metadata_rejected(self) -> None:
        registry = _registry()
        with pytest.raises(InvalidRuleDefinitionError, match="rule_name"):
            registry.register(
                RuleDefinition.model_construct(
                    rule_id=RuleId("a"),
                    rule_version=RuleVersion("v1"),
                    rule_name="",
                    description="desc",
                    input_fact_types=frozenset({FactType.WAITING_INTERVAL}),
                    configuration_requirements=frozenset(),
                    evaluator_id=EVALUATOR,
                    output_event_type=RuleEventType.QUEUE_CANDIDATE,
                    deterministic_version=TEMPORAL_ENGINE_VERSION,
                )
            )


# =============================================================================
# 8. Immutable metadata
# =============================================================================


class TestImmutability:
    def test_registered_definition_is_never_mutated(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        stored = registry.resolve(rule.rule_id, rule.rule_version)
        # Frozen model: in-place writes raise (pydantic frozen validation).
        with pytest.raises(ValidationError):
            stored.rule_name = "mutated"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            stored.rule_version = RuleVersion("v2")  # type: ignore[misc]
        # And the registry has no mutation surface — re-registering is the
        # only path and it is rejected (see TestDuplicateRejection).
        assert registry.resolve(rule.rule_id, rule.rule_version) == rule


# =============================================================================
# 9. Deterministic rule identity
# =============================================================================


class TestDeterministicIdentity:
    def test_identity_is_stable_and_versioned(self) -> None:
        registry = _registry()
        v1 = _definition(version="v1")
        v2 = _definition(version="v2")
        registry.register(v1)
        registry.register(v2)
        assert registry.resolve(v1.rule_id, v1.rule_version).canonical_identity == (
            "queue_candidate:v1"
        )
        assert registry.resolve(v2.rule_id, v2.rule_version).canonical_identity == (
            "queue_candidate:v2"
        )
        # Two lookups of the same identity return the same definition.
        assert registry.resolve(v1.rule_id, v1.rule_version) == registry.get(
            v1.rule_id, v1.rule_version
        )


# =============================================================================
# 10/11. Invalid configuration / input rejected (deterministic boundary)
# =============================================================================


class TestInputValidation:
    def test_input_version_mismatch_rejected(self) -> None:
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        with pytest.raises(RuleConfigurationMismatchError, match="does not match"):
            registry.validate_input(rule, _input(rule_version="v2"))

    def test_missing_configuration_rejected(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        with pytest.raises(MissingRuleConfigurationError, match="waiting_qualification_seconds"):
            registry.validate_input(rule, _input(configuration={}))

    def test_unsupported_fact_type_rejected(self) -> None:
        registry = _registry()
        rule = _definition()  # declares only WAITING_INTERVAL
        registry.register(rule)
        from contracts.temporal import DwellInterval

        dwell_key = TemporalStateKey(
            fsm_kind="dwell",
            tenant_id=_TENANT,
            venue_id=_VENUE,
            session_id=_SESSION,
            camera_id=_CAMERA,
            configuration_version_id=_CONFIG,
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
            duration_seconds=0.0,
            qualified=True,
            minimum_dwell_seconds=0.0,
            reason=TemporalReason.EXIT_CONFIRMED,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        with pytest.raises(UnsupportedFactTypeError, match="does not declare fact type"):
            registry.validate_input(rule, _input(facts=(dwell,)))

    def test_non_input_rejected(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        with pytest.raises(InvalidRuleInputError, match="RuleEvaluationInput"):
            registry.validate_input(rule, object())  # type: ignore[arg-type]

    def test_valid_input_passes(self) -> None:
        registry = _registry()
        rule = _definition()
        registry.register(rule)
        registry.validate_input(rule, _input())  # must not raise


# =============================================================================
# validate() — whole-registry governance (Part 5)
# =============================================================================


class TestRegistryValidate:
    def test_valid_registry_returns_ok(self) -> None:
        registry = _registry()
        registry.register(_definition(rule_id="a", version="v1"))
        registry.register(_definition(rule_id="b", version="v1"))
        result = registry.validate()
        assert result.ok is True
        assert result.issues == ()

    def test_empty_registry_validates_ok(self) -> None:
        assert _registry().validate().ok is True

    def test_reports_invalid_entries_without_raising(self) -> None:
        registry = RuleRegistry(supported_evaluators=frozenset({EVALUATOR}))
        registry.register(_definition(rule_id="a", version="v1"))
        # Simulate an evaluator being de-listed after registration: the
        # registry must REPORT it, never silently drop the rule.
        registry._supported_evaluators = frozenset()  # direct state for test
        result = registry.validate()
        assert result.ok is False
        assert any("unsupported evaluator" in issue for issue in result.issues)
        # The rule is still registered (report-only governance).
        assert registry.has(RuleId("a"), RuleVersion("v1"))
