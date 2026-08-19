"""Deterministic rule registry (Task 16.1 Part 5).

A versioned, immutable registry of ``RuleDefinition`` objects. The
registry is the single source of truth for which rules exist, at which
explicit version, and with which declared inputs/outputs. It performs NO
I/O (no database, Redis, HTTP, or LLM), reads no wall clock, and retains
no unbounded state: lookups are O(1) via a ``dict`` keyed by
``(rule_id, rule_version)`` and iteration is deterministic (sorted by
canonical identity).

Contract (Task 16.1 Part 5):

    register(rule)  — validate + store; reject duplicates, missing
                      metadata, invalid versions, unsupported evaluators,
                      and invalid output event types (typed errors).
    get(rule_id, version)        — safe lookup (None when absent).
    has(rule_id, version)        — membership check.
    list()                       — deterministic tuple (sorted by identity).
    validate()                   — re-validate every registered rule.
    resolve(rule_id, version)    — strict lookup (typed errors).
    validate_input(rule, input)  — validate evaluation inputs against the
                                   rule's declared fact types and
                                   configuration requirements (the
                                   canonical input boundary).

Immutability (Part 6): once registered, a definition is never mutated
in-place — the frozen ``RuleDefinition`` is stored as-is, and a duplicate
``(rule_id, rule_version)`` registration raises ``DuplicateRuleError``
instead of silently overwriting. Behavior changes require a new version.

The registry does NOT evaluate rules: evaluator execution, cooldown, and
duplicate suppression are later Task 16 steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.intelligence.rules.exceptions import (
    DuplicateRuleError,
    InvalidRuleDefinitionError,
    InvalidRuleInputError,
    MissingRuleConfigurationError,
    MixedScopeRuleInputError,
    RuleConfigurationMismatchError,
    RuleError,
    UnknownRuleError,
    UnsupportedDeterministicVersionError,
    UnsupportedEvaluatorError,
    UnsupportedFactTypeError,
    UnsupportedRuleVersionError,
)
from contracts.common import RuleId, RuleVersion, TenantId, VenueId, VideoSessionId
from contracts.rules import (
    FactType,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEventType,
)
from contracts.temporal import TEMPORAL_ENGINE_VERSION, TemporalStateKey


@dataclass(frozen=True, slots=True)
class RegistryValidation:
    """Deterministic result of validating the whole registry."""

    ok: bool
    issues: tuple[str, ...] = ()


class RuleRegistry:
    """A deterministic, versioned, immutable rule registry.

    ``supported_evaluators`` is the explicit allowlist of evaluator
    identities the registry accepts. Later Task 16 steps register the
    evaluator implementations and their identities here; an empty
    allowlist (the default) means no rule can be registered yet — the
    registry exists as the contract foundation.

    ``supported_deterministic_versions`` is the allowlist of
    deterministic contract versions (``RuleDefinition.deterministic_version``)
    the registry can interpret. It defaults to the current
    ``TEMPORAL_ENGINE_VERSION`` — a rule authored against an unknown
    deterministic contract version is REJECTED (Task 16.2 Part 11: no
    silent reinterpretation of rules written for a different engine).
    """

    def __init__(
        self,
        *,
        supported_evaluators: frozenset[str] = frozenset(),
        supported_deterministic_versions: frozenset[str] = frozenset({TEMPORAL_ENGINE_VERSION}),
    ) -> None:
        self._rules: dict[tuple[RuleId, RuleVersion], RuleDefinition] = {}
        self._supported_evaluators = frozenset(supported_evaluators)
        self._supported_deterministic_versions = frozenset(supported_deterministic_versions)

    # ------------------------------------------------------------------
    # Registration (Part 5)
    # ------------------------------------------------------------------

    def register(self, rule: RuleDefinition) -> None:
        """Validate and store one immutable rule definition.

        Rejects (typed errors): non-``RuleDefinition`` inputs, duplicate
        ``(rule_id, rule_version)``, missing metadata, invalid rule
        versions, unsupported evaluator identities, and invalid output
        event types. Nothing is silently overwritten (Part 6).
        """
        self._validate_definition(rule)
        key = (rule.rule_id, rule.rule_version)
        if key in self._rules:
            raise DuplicateRuleError(
                f"rule {rule.canonical_identity} is already registered — "
                "definitions are immutable; create a new rule_version instead"
            )
        self._rules[key] = rule

    # ------------------------------------------------------------------
    # Lookup (Part 5)
    # ------------------------------------------------------------------

    def get(self, rule_id: RuleId, version: RuleVersion) -> RuleDefinition | None:
        """Safe lookup: the registered definition or None."""
        return self._rules.get((rule_id, version))

    def has(self, rule_id: RuleId, version: RuleVersion) -> bool:
        """Whether the exact (rule_id, rule_version) is registered."""
        return (rule_id, version) in self._rules

    def list(self) -> tuple[RuleDefinition, ...]:
        """All registered rules, deterministic order (sorted by identity)."""
        return tuple(
            self._rules[key] for key in sorted(self._rules, key=lambda k: (str(k[0]), str(k[1])))
        )

    def resolve(self, rule_id: RuleId, version: RuleVersion) -> RuleDefinition:
        """Strict lookup: the registered definition or a typed error.

        Raises:
            UnknownRuleError: the rule_id is not registered at all.
            UnsupportedRuleVersionError: the rule_id exists but this
                version is not registered.
        """
        key = (rule_id, version)
        if key in self._rules:
            return self._rules[key]
        if any(existing[0] == rule_id for existing in self._rules):
            raise UnsupportedRuleVersionError(
                f"rule {rule_id} has no registered version {version}; "
                f"registered: {sorted(str(v) for _, v in self._rules if _ == rule_id)}"
            )
        raise UnknownRuleError(f"unknown rule {rule_id}")

    # ------------------------------------------------------------------
    # Validation (Part 5) — whole-registry and per-definition
    # ------------------------------------------------------------------

    def validate(self) -> RegistryValidation:
        """Re-validate every registered definition; never raises.

        Returns a deterministic ``RegistryValidation`` report; a rule that
        becomes invalid (e.g. its evaluator was removed from the allowlist)
        is reported, never silently dropped.
        """
        issues: list[str] = []
        for rule in self.list():
            try:
                self._validate_definition(rule)
            except RuleError as exc:
                issues.append(f"{rule.canonical_identity}: {exc.message}")
        return RegistryValidation(ok=not issues, issues=tuple(issues))

    # ------------------------------------------------------------------
    # Input boundary (Part 7 / 10) — deterministic input validation
    # ------------------------------------------------------------------

    def validate_input(self, rule: RuleDefinition, inp: RuleEvaluationInput) -> None:
        """Validate evaluation inputs against a rule's declared contract.

        Checks (typed errors):
          - ``inp`` is a canonical ``RuleEvaluationInput``;
          - the input's ``rule_version`` matches the rule's version
            (a rule is never evaluated under a different version);
          - every input fact is a canonical fact type the rule declared
            (unsupported facts are rejected, never ignored);
          - every required configuration key is present in the explicit
            configuration snapshot (no silent "latest" config).
        """
        if not isinstance(rule, RuleDefinition):
            raise InvalidRuleDefinitionError(
                f"rule must be a RuleDefinition, got {type(rule).__name__}"
            )
        if not isinstance(inp, RuleEvaluationInput):
            raise InvalidRuleInputError(
                f"input must be a RuleEvaluationInput, got {type(inp).__name__}"
            )
        if inp.rule_version != rule.rule_version:
            raise RuleConfigurationMismatchError(
                f"input rule_version {inp.rule_version} does not match rule "
                f"{rule.canonical_identity} — a rule is never evaluated under "
                "a different version"
            )
        missing = rule.configuration_requirements - set(inp.configuration)
        if missing:
            raise MissingRuleConfigurationError(
                f"rule {rule.canonical_identity} requires configuration keys "
                f"{sorted(missing)} for configuration version "
                f"{inp.configuration_version_id}, but they were not provided"
            )
        # Task 16.2 Part 18 — a single evaluation stays within ONE
        # tenant/venue/session scope. The scope comes from the canonical
        # facts' keys (never from raw CV objects); mixing scopes in one
        # input is a typed error, never a silent cross-tenant evaluation.
        scope: tuple[TenantId, VenueId, VideoSessionId] | None = None
        for fact in inp.facts:
            fact_type = self._fact_type_of(fact)
            if fact_type not in rule.input_fact_types:
                raise UnsupportedFactTypeError(
                    f"rule {rule.canonical_identity} does not declare fact type "
                    f"{fact_type.value!r}; declared: "
                    f"{sorted(t.value for t in rule.input_fact_types)}"
                )
            fact_scope = self._scope_of(fact)
            if scope is None:
                scope = fact_scope
            elif scope != fact_scope:
                raise MixedScopeRuleInputError(
                    f"rule {rule.canonical_identity} input mixes facts from "
                    f"different scopes (tenant/venue/session) — one evaluation "
                    "must stay within a single scope; a rule can never evaluate "
                    "facts belonging to another tenant or venue"
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_definition(self, rule: RuleDefinition) -> None:
        """The registry's own definition checks (typed errors).

        The frozen contract already enforces most invariants at
        construction; this re-checks them at the registry boundary so a
        registry can never hold a definition that violates its contract.
        """
        if not isinstance(rule, RuleDefinition):
            raise InvalidRuleDefinitionError(
                f"rule must be a RuleDefinition, got {type(rule).__name__}"
            )
        if not rule.rule_id or not str(rule.rule_id).strip():
            raise InvalidRuleDefinitionError("rule_id must be a non-empty canonical identifier")
        if not rule.rule_name or not rule.rule_name.strip():
            raise InvalidRuleDefinitionError("rule_name must be a non-empty string")
        if not rule.description or not rule.description.strip():
            raise InvalidRuleDefinitionError("description must be a non-empty string")
        if not rule.evaluator_id or not rule.evaluator_id.strip():
            raise InvalidRuleDefinitionError("evaluator_id must be a non-empty string")
        if not rule.input_fact_types:
            raise InvalidRuleDefinitionError(
                f"rule {rule.canonical_identity} must declare at least one input fact type"
            )
        if not isinstance(rule.output_event_type, RuleEventType):
            raise InvalidRuleDefinitionError(
                f"rule {rule.canonical_identity} output_event_type must be a "
                f"controlled RuleEventType, got {rule.output_event_type!r}"
            )
        if rule.deterministic_version not in self._supported_deterministic_versions:
            raise UnsupportedDeterministicVersionError(
                f"rule {rule.canonical_identity} references unsupported deterministic "
                f"contract version {rule.deterministic_version!r}; supported: "
                f"{sorted(self._supported_deterministic_versions)}"
            )
        if rule.evaluator_id not in self._supported_evaluators:
            raise UnsupportedEvaluatorError(
                f"rule {rule.canonical_identity} references unsupported evaluator "
                f"{rule.evaluator_id!r}; supported: "
                f"{sorted(self._supported_evaluators)}"
            )

    @staticmethod
    def _scope_of(fact: object) -> tuple[TenantId, VenueId, VideoSessionId]:
        """Extract the tenant/venue/session scope of one canonical fact.

        Every canonical Task 15 fact carries its provenance in a
        ``TemporalStateKey``; the scope is read from there — the same
        deterministic source the fact's own identity uses. A fact without
        a canonical key is rejected (never silently treated as unscoped).
        """
        key = getattr(fact, "key", None)
        if not isinstance(key, TemporalStateKey):
            raise InvalidRuleInputError(
                f"fact of type {type(fact).__name__} does not carry a canonical "
                "TemporalStateKey — scope isolation cannot be established"
            )
        return (key.tenant_id, key.venue_id, key.session_id)

    @staticmethod
    def _fact_type_of(fact: object) -> FactType:
        """Map a canonical Task 15 fact to its declared fact type.

        Deterministic isinstance dispatch — the exact mirror of the
        ``FactType`` enum. A fact outside the canonical union is rejected,
        never silently accepted.
        """
        from contracts.temporal import (  # local import — avoids import cycle at module load
            DwellInterval,
            MovementClassificationTransition,
            MovementMeasurement,
            OccupancySnapshot,
            TemporalTransition,
            WaitingInterval,
        )

        if isinstance(fact, TemporalTransition):
            return FactType.TEMPORAL_TRANSITION
        if isinstance(fact, DwellInterval):
            return FactType.DWELL_INTERVAL
        if isinstance(fact, OccupancySnapshot):
            return FactType.OCCUPANCY_SNAPSHOT
        if isinstance(fact, MovementMeasurement):
            return FactType.MOVEMENT_MEASUREMENT
        if isinstance(fact, MovementClassificationTransition):
            return FactType.MOVEMENT_CLASSIFICATION_TRANSITION
        if isinstance(fact, WaitingInterval):
            return FactType.WAITING_INTERVAL
        raise UnsupportedFactTypeError(
            f"unsupported temporal fact type {type(fact).__name__} — rules consume "
            "canonical Task 15 facts only"
        )
