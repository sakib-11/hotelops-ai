"""Unit tests for Task 16.2 — deterministic rule definition + versioning.

Task 16.2 hardens the Task 16.1 registry into an explicitly versioned,
immutable, replayable rule catalog:

- rule identity: (rule_id, rule_version) is unique and immutable;
- version isolation: v1 and v2 coexist; neither overwrites the other;
- configuration version is SEPARATE from rule version and both are
  preserved through evaluation;
- disabled rules stay registered and resolvable (never deleted);
- historical replay resolves the exact original rule version — never a
  mutable \"latest\";
- deterministic serialization: no runtime-only values, byte-stable output;
- tenant/venue/session isolation: one evaluation never mixes scopes
  (Task 16.2 Part 18).

No business rules are implemented here — the \"evaluator\" in the replay
tests is a deterministic test-local oracle that maps (rule, input) to a
``RuleEvaluationResult`` purely to prove the replay contract.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from backend.app.intelligence.rules import (
    DuplicateRuleError,
    InvalidRuleDefinitionError,
    MissingRuleConfigurationError,
    MixedScopeRuleInputError,
    RuleRegistry,
    UnsupportedDeterministicVersionError,
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
from contracts.events import EventEnvelope
from contracts.rules import (
    FactType,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
    validate_rule_version,
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
_VENUE_A = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_VENUE_B = VenueId(uuid.UUID("80000000-0000-0000-0000-000000000001"))
_SESSION_A = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_SESSION_B = VideoSessionId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG_A = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_B = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_TRACK = TrackId(uuid.UUID("61000000-0000-0000-0000-000000000001"))

_EVENT_TIME = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_PRODUCED = datetime(2026, 8, 1, 10, 0, 5, tzinfo=UTC)

EVALUATOR_V1 = "queue_candidate_evaluator.v1"
EVALUATOR_V2 = "queue_candidate_evaluator.v2"


def _definition(
    *,
    rule_id: str = RuleIdentifier.QUEUE_CANDIDATE.value,
    version: str = "v1",
    evaluator: str = EVALUATOR_V1,
    enabled: bool = True,
    deterministic_version: str = TEMPORAL_ENGINE_VERSION,
    **overrides,
) -> RuleDefinition:
    values: dict = {
        "rule_id": RuleId(rule_id),
        "rule_version": RuleVersion(version),
        "rule_name": "Queue candidate",
        "description": "Detects a queue candidate in a configured waiting context.",
        "enabled": enabled,
        "input_fact_types": frozenset({FactType.WAITING_INTERVAL}),
        "configuration_requirements": frozenset({"waiting_qualification_seconds"}),
        "evaluator_id": evaluator,
        "output_event_type": RuleEventType.QUEUE_CANDIDATE,
        "deterministic_version": deterministic_version,
    }
    values.update(overrides)
    return RuleDefinition(**values)


def _registry(**kwargs) -> RuleRegistry:
    kwargs.setdefault("supported_evaluators", frozenset({EVALUATOR_V1, EVALUATOR_V2}))
    return RuleRegistry(**kwargs)


def _waiting_fact(
    *,
    tenant: TenantId = _TENANT_A,
    venue: VenueId = _VENUE_A,
    session: VideoSessionId = _SESSION_A,
    config: ConfigurationVersionId = _CONFIG_A,
) -> WaitingInterval:
    key = TemporalStateKey(
        fsm_kind="waiting",
        tenant_id=tenant,
        venue_id=venue,
        session_id=session,
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
    config_version: ConfigurationVersionId = _CONFIG_A,
    configuration: dict | None = None,
    facts: tuple | None = None,
) -> RuleEvaluationInput:
    config = {"waiting_qualification_seconds": 3.0} if configuration is None else configuration
    return RuleEvaluationInput(
        facts=facts if facts is not None else (_waiting_fact(),),
        configuration=config,
        configuration_version_id=config_version,
        rule_version=RuleVersion(rule_version),
        event_time=_EVENT_TIME,
    )


def _evaluate(rule: RuleDefinition, inp: RuleEvaluationInput) -> RuleEvaluationResult:
    """Deterministic test-local evaluation oracle (NOT a business rule).

    Maps (rule, input) to a RuleEvaluationResult purely to prove the
    replay contract: the same (facts, configuration, configuration
    version, rule version) MUST produce the same logical result, and
    registering rule v2 must never perturb v1's evaluation.
    """
    # A rule \"matches\" when its declared configuration is present — a
    # content-based decision derived only from the rule + input, so v2
    # (with different configuration_requirements) can differ explicitly.
    matched = all(key in inp.configuration for key in rule.configuration_requirements)
    status = RuleEvaluationStatus.MATCH if matched else RuleEvaluationStatus.NO_MATCH
    event = None
    if status is RuleEvaluationStatus.MATCH:
        # Content-derived event id: replaying the same (rule, input)
        # reproduces the same identity (the Task 7 idempotency principle).
        event_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rule:{rule.canonical_identity}@{inp.configuration_version_id}",
        )
        event = EventEnvelope(
            event_id=EventId(event_id),
            event_type=rule.output_event_type.value,
            event_time=inp.event_time,
            produced_at=_PRODUCED,
            source=f"rule:{rule.canonical_identity}",
            payload={"rule_version": rule.rule_version, "matched": True},
        )
    return RuleEvaluationResult(
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        status=status,
        event_time=inp.event_time,
        configuration_version_id=inp.configuration_version_id,
        event=event,
        tenant_id=inp.facts[0].key.tenant_id,
        venue_id=inp.facts[0].key.venue_id,
        session_id=inp.facts[0].key.session_id,
    )


# =============================================================================
# Version isolation (Parts 2 / 6 / 15) — v1 and v2 coexist, never overwrite
# =============================================================================


class TestVersionIsolation:
    def test_v1_and_v2_coexist(self) -> None:
        registry = _registry()
        v1 = _definition(version="v1", evaluator=EVALUATOR_V1)
        v2 = _definition(
            version="v2",
            evaluator=EVALUATOR_V2,
            configuration_requirements=frozenset({
                "waiting_qualification_seconds",
                "queue_max_length",
            }),
        )
        registry.register(v1)
        registry.register(v2)
        # get() returns the EXACT requested version — neither overwrites.
        assert registry.get(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1")) is v1
        assert registry.get(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v2")) is v2
        assert v1.canonical_identity == "queue_candidate:v1"
        assert v2.canonical_identity == "queue_candidate:v2"

    def test_v2_never_mutates_v1(self) -> None:
        registry = _registry()
        v1 = _definition(version="v1")
        registry.register(v1)
        before = v1.model_dump_json()
        registry.register(_definition(version="v2"))
        after = registry.resolve(v1.rule_id, v1.rule_version).model_dump_json()
        assert before == after  # byte-identical definition, unchanged

    def test_duplicate_version_rejected(self) -> None:
        registry = _registry()
        registry.register(_definition(version="v1"))
        with pytest.raises(DuplicateRuleError):
            registry.register(_definition(version="v1", evaluator=EVALUATOR_V2))

    def test_rule_version_format_is_explicit(self) -> None:
        # The single approved format: machine-comparable, serializable,
        # rejects malformed versions (Task 16.2 Part 3).
        assert validate_rule_version(RuleVersion("v1")) == RuleVersion("v1")
        for bad in ("1", "V1", "latest", "", "v1-rc", "queue_candidate"):
            with pytest.raises(ValueError):
                validate_rule_version(RuleVersion(bad))

    def test_no_latest_bypass(self) -> None:
        # Part 16 — the registry has no \"latest\" resolver: resolution is
        # always explicit-version, and \"latest\" is not even a valid version.
        registry = _registry()
        registry.register(_definition(version="v1"))
        assert not hasattr(registry, "latest")
        with pytest.raises(ValueError):
            validate_rule_version(RuleVersion("latest"))
        # resolve() without an explicit version is impossible by contract.
        assert registry.resolve(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"))


# =============================================================================
# Configuration version is SEPARATE from rule version (Part 7)
# =============================================================================


class TestConfigurationVersionSeparation:
    def test_config_version_and_rule_version_both_preserved(self) -> None:
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        # Same rule v1, two different pinned configuration versions.
        inp_a = _input(rule_version="v1", config_version=_CONFIG_A)
        inp_b = _input(rule_version="v1", config_version=_CONFIG_B)
        registry.validate_input(rule, inp_a)
        registry.validate_input(rule, inp_b)
        result_a = _evaluate(rule, inp_a)
        result_b = _evaluate(rule, inp_b)
        # rule_version is v1 in both; configuration version differs.
        assert result_a.rule_version == RuleVersion("v1")
        assert result_b.rule_version == RuleVersion("v1")
        assert result_a.configuration_version_id == _CONFIG_A
        assert result_b.configuration_version_id == _CONFIG_B
        # Both serialize independently.
        assert result_a.model_validate(result_a.model_dump(mode="json")) == result_a
        assert result_b.model_validate(result_b.model_dump(mode="json")) == result_b

    def test_never_uses_current_latest_configuration(self) -> None:
        # The input carries the explicit snapshot + pinned version; no
        # \"current\" configuration is consulted anywhere.
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        inp = _input(config_version=_CONFIG_A, configuration={"waiting_qualification_seconds": 5.0})
        registry.validate_input(rule, inp)
        assert inp.configuration_version_id == _CONFIG_A
        assert inp.configuration["waiting_qualification_seconds"] == 5


# =============================================================================
# Disabled rules (Part 10) — stay registered, stay resolvable
# =============================================================================


class TestDisabledRules:
    def test_disabled_rule_remains_registered(self) -> None:
        registry = _registry()
        disabled = _definition(version="v1", enabled=False)
        registry.register(disabled)
        # Still in the registry: listable, resolvable, validated.
        assert any(r.canonical_identity == "queue_candidate:v1" for r in registry.list())
        resolved = registry.resolve(disabled.rule_id, disabled.rule_version)
        assert resolved.enabled is False
        assert registry.validate().ok is True

    def test_disabled_rule_never_deleted_by_reenabling(self) -> None:
        registry = _registry()
        registry.register(_definition(version="v1", enabled=False))
        # \"Re-enabling\" is a NEW version — the disabled v1 remains.
        registry.register(_definition(version="v2", enabled=True))
        v1 = registry.resolve(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v1"))
        v2 = registry.resolve(RuleId(RuleIdentifier.QUEUE_CANDIDATE.value), RuleVersion("v2"))
        assert v1.enabled is False
        assert v2.enabled is True


# =============================================================================
# Historical replay (Part 14) — facts + rule:v1 + config:v1 → stable result
# =============================================================================


class TestHistoricalReplay:
    def test_replay_with_v1_is_unaffected_by_v2(self) -> None:
        registry = _registry()
        v1 = _definition(version="v1", evaluator=EVALUATOR_V1)
        registry.register(v1)

        inp = _input(rule_version="v1", config_version=_CONFIG_A)
        registry.validate_input(v1, inp)
        result_a = _evaluate(v1, inp)
        serialized_a = result_a.model_dump_json()

        # Publish v2 (different requirements — would change the outcome).
        v2 = _definition(
            version="v2",
            evaluator=EVALUATOR_V2,
            configuration_requirements=frozenset({
                "waiting_qualification_seconds",
                "queue_max_length",
            }),
        )
        registry.register(v2)

        # Re-evaluate the SAME facts with rule:v1 → result A unchanged.
        v1_again = registry.resolve(v1.rule_id, v1.rule_version)
        assert v1_again is v1  # the exact same immutable definition
        registry.validate_input(v1_again, inp)
        result_a_replay = _evaluate(v1_again, inp)
        assert result_a_replay == result_a
        assert result_a_replay.model_dump_json() == serialized_a

        # Evaluating with v2 differs — and the difference is attributable
        # to v2's declared requirements (queue_max_length is missing).
        inp_v2 = _input(rule_version="v2", config_version=_CONFIG_A)
        with pytest.raises(MissingRuleConfigurationError, match="queue_max_length"):
            registry.validate_input(v2, inp_v2)

    def test_replay_uses_pinned_configuration_version(self) -> None:
        registry = _registry()
        v1 = _definition(version="v1")
        registry.register(v1)
        inp = _input(rule_version="v1", config_version=_CONFIG_B)
        registry.validate_input(v1, inp)
        result = _evaluate(v1, inp)
        # The result preserves the pinned configuration version (B), never
        # \"the latest\" (A).
        assert result.configuration_version_id == _CONFIG_B


# =============================================================================
# Deterministic serialization (Part 12) — byte-stable, no runtime values
# =============================================================================


class TestDeterministicSerialization:
    def test_definition_serialization_is_byte_stable(self) -> None:
        rule = _definition(version="v1")
        first = rule.model_dump_json()
        second = rule.model_dump_json()
        assert first == second
        # No runtime-only fields (no timestamps, no counters) are emitted.
        data = json.loads(first)
        assert "schema_version" in data
        assert "rule_version" in data

    def test_deserialization_round_trip_equivalent(self) -> None:
        rule = _definition(version="v1")
        restored = RuleDefinition.model_validate(rule.model_dump(mode="json"))
        assert restored == rule
        assert restored.canonical_identity == rule.canonical_identity


# =============================================================================
# Registry validation hardening (Part 11) — unsupported contract versions
# =============================================================================


class TestUnsupportedDeterministicVersion:
    def test_unsupported_contract_version_rejected(self) -> None:
        # A registry pinned to an older engine contract rejects a rule
        # authored against a newer one — no silent reinterpretation.
        registry = RuleRegistry(
            supported_evaluators=frozenset({EVALUATOR_V1}),
            supported_deterministic_versions=frozenset({"0.1.0"}),
        )
        with pytest.raises(UnsupportedDeterministicVersionError, match="deterministic"):
            registry.register(_definition(deterministic_version="9.9.9"))

    def test_default_registry_supports_current_engine_version(self) -> None:
        registry = _registry()
        registry.register(_definition())  # deterministic_version == TEMPORAL_ENGINE_VERSION
        assert registry.validate().ok is True

    def test_invalid_definition_still_rejected(self) -> None:
        registry = _registry()
        with pytest.raises(InvalidRuleDefinitionError):
            registry.register(
                RuleDefinition.model_construct(
                    rule_id=RuleId("a"),
                    rule_version=RuleVersion("v1"),
                    rule_name="",
                    description="desc",
                    input_fact_types=frozenset({FactType.WAITING_INTERVAL}),
                    configuration_requirements=frozenset(),
                    evaluator_id=EVALUATOR_V1,
                    output_event_type=RuleEventType.QUEUE_CANDIDATE,
                    deterministic_version=TEMPORAL_ENGINE_VERSION,
                )
            )


# =============================================================================
# Tenant / venue / session isolation (Part 18)
# =============================================================================


class TestScopeIsolation:
    def test_cross_tenant_input_rejected(self) -> None:
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        mixed = (
            _waiting_fact(tenant=_TENANT_A),
            _waiting_fact(tenant=_TENANT_B),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            registry.validate_input(rule, _input(facts=mixed))

    def test_cross_venue_input_rejected(self) -> None:
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        mixed = (
            _waiting_fact(venue=_VENUE_A),
            _waiting_fact(venue=_VENUE_B),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            registry.validate_input(rule, _input(facts=mixed))

    def test_cross_session_input_rejected(self) -> None:
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        mixed = (
            _waiting_fact(session=_SESSION_A),
            _waiting_fact(session=_SESSION_B),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            registry.validate_input(rule, _input(facts=mixed))

    def test_single_scope_input_passes(self) -> None:
        registry = _registry()
        rule = _definition(version="v1")
        registry.register(rule)
        registry.validate_input(rule, _input(facts=(_waiting_fact(), _waiting_fact())))
