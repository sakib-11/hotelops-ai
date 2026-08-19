"""Task 16.9 — the ``data_quality`` operational rule (the FINAL Task 16 rule).

Detects deterministic data-quality failures in canonical Task 15 facts and
emits a versioned ``data_quality`` event (Part 1). The rule is NOT a
replacement for schema validation — the canonical fact models already
validate their contracts at construction; this rule is the second line of
defense that classifies quality conditions on facts that reached the
evaluation boundary (e.g. facts reconstructed from storage/checkpoints or
produced by non-validating paths) into deterministic, machine-readable
findings (Part 4).

Checks are implemented through a centralized deterministic quality-check
registry (Part 6/37), never scattered if/else: each ``QualityCheck`` has a
stable ``check_id`` (== its quality code), description, deterministic
severity, applicability (which canonical fact types it inspects), a pure
evaluator, and a version. The registry is deterministic (sorted by
check_id) and rejects duplicates.

Implemented checks (only conditions provable from existing contracts —
Part 4/9):

    DATA_MISSING_REQUIRED_IDENTITY   (ERROR)   — source/subject identity
        (``camera_id`` / ``track_id``) missing from the fact's key. Missing
        tenant/venue/session is NOT a finding: an un-attributable fact can
        never emit an event (the Task 16.3 engine requires full scope
        provenance on MATCH — Parts 22/23); such input is INVALID, never a
        quality event.
    DATA_INVALID_EVENT_TIME          (ERROR)   — the fact's event-time
        field (``event_time`` / ``last_seen``) is missing.
    DATA_NON_MONOTONIC_EVENT_TIME    (ERROR)   — impossible ordering:
        interval end / last_seen precedes the start, or a measurement's
        event_time precedes its previous_event_time.
    DATA_NEGATIVE_DURATION           (ERROR)   — ``duration_seconds`` < 0
        (interval) or ``time_delta_seconds`` < 0 (measurement).
    DATA_TEMPORAL_INCONSISTENCY      (ERROR)   — contradictory state: an
        interval flagged ``qualified`` while below its configured minimum,
        or an occupancy snapshot whose counts do not reconcile
        (``previous_count + delta != occupancy_count``).
    DATA_INVALID_PROVENANCE          (WARNING) — missing/blank
        ``fsm_version`` / ``policy_revision``, or a key without a
        configuration version.
    DATA_UNKNOWN_SPATIAL_REFERENCE   (WARNING) — ``semantic_context`` set
        but not in the EXPLICIT configured known-spatial-context list
        (never geometry recomputation — Part 7E).
    DATA_CONFIGURATION_MISMATCH      (CRITICAL) — the fact's key
        ``configuration_version_id`` differs from the evaluation's pinned
        configuration version (Part 7H/32 — never a silent switch).

Aggregation (Part 13/14): the rule result carries exactly one
EventEnvelope, so one AGGREGATE quality event is emitted per evaluation
with ALL findings, deterministically sorted by ``quality_code``; the
payload's ``primary_quality_code``/``primary_severity`` are the first
finding (the headline). Multiple independent failures are never collapsed
away — every finding is preserved in the payload tuple.

Classification (Part 12): INVALID is reserved for malformed input (missing
tenant/venue/session provenance, non-canonical fact, invalid configuration
value); a well-formed fact with a quality condition is MATCH. A quality
problem is NEVER turned into NO_MATCH.

Determinism (Part 25/29): same fact + same rule version + same configuration
version + same configuration → same findings, same event identity (the
engine's content-derived ``deterministic_event_id``). No wall clock, no
randomness, no network/database state. Idempotency (Part 15/35): repeated
evaluation of the same fact reproduces the same logical event identity —
no second deduplication system.

Event-time (Part 16): the event carries ``inp.event_time`` (the caller's
authoritative event time — the canonical fallback the EventEnvelope
contract requires); processing time appears only as ``produced_at``. A
fact's OWN missing event-time is a reported finding
(``DATA_INVALID_EVENT_TIME``), never silently repaired.

The rule is pure (Part 38): no PostgreSQL, Redis, S3, HTTP, LLM, or Task 15
state mutation — it returns a deterministic ``RuleEvaluationResult`` only.
It never inspects raw video/frames/detections/tracker objects (Part 1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from contracts.common import RuleId, RuleVersion
from contracts.events import EventEnvelope
from contracts.rules import (
    CooldownPolicy,
    DataQualityPayload,
    DataQualitySeverity,
    EvidenceRequirement,
    FactType,
    QualityFinding,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    DwellInterval,
    MovementClassificationTransition,
    MovementMeasurement,
    OccupancySnapshot,
    TemporalTransition,
    WaitingInterval,
)

__all__ = [
    "DATA_QUALITY_EVALUATOR_ID",
    "KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY",
    "DataQualityEventEvaluator",
    "QualityCheck",
    "QualityCheckRegistry",
    "data_quality_definition",
]

# The explicit configuration key the rule reads (Part 6): the known spatial
# contexts (canonical semantic-context ids) of the pinned configuration
# version. The rule NEVER hardcodes a spatial catalog — only the key name,
# which is the canonical configuration contract for this rule.
KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY: Final = "known_spatial_contexts"

# The canonical evaluator identity for the data_quality rule v1.
DATA_QUALITY_EVALUATOR_ID: Final = "data_quality_evaluator.v1"


def data_quality_definition(*, version: str = "v1") -> RuleDefinition:
    """The canonical ``data_quality`` v1 rule definition (Part 3).

    ``(rule_id, rule_version)`` = ``(data_quality, v1)`` — explicit,
    immutable, registry-governed. Version handling lives in the versioning
    system (Task 16.2), never hardcoded inside the evaluator.
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.DATA_QUALITY.value),
        rule_version=RuleVersion(version),
        rule_name="Data Quality",
        description=(
            "Detects deterministic data-quality conditions in canonical "
            "Task 15 facts and emits an aggregate data_quality event with "
            "stable machine-readable quality codes."
        ),
        enabled=True,
        input_fact_types=frozenset({
            FactType.TEMPORAL_TRANSITION,
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
            FactType.MOVEMENT_MEASUREMENT,
            FactType.MOVEMENT_CLASSIFICATION_TRANSITION,
            FactType.WAITING_INTERVAL,
        }),
        configuration_requirements=frozenset({KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY}),
        evaluator_id=DATA_QUALITY_EVALUATOR_ID,
        output_event_type=RuleEventType.DATA_QUALITY,
        evidence_requirement=EvidenceRequirement.OPTIONAL,
        cooldown_policy=CooldownPolicy(enabled=False, duration_seconds=0.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


# =============================================================================
# Part 6/37 — the deterministic quality-check registry
# =============================================================================


class QualityCheck:
    """One deterministic quality check (Part 6).

    ``check_id`` is the stable machine-readable quality code; ``severity``
    is deterministic (declared here, never computed at runtime); the
    ``evaluator`` is a PURE callable ``(fact) -> QualityFinding | None``
    that returns a finding when the condition holds, else None.
    ``applicability`` declares which canonical fact types the check
    inspects. Immutable by convention — a behavior change is a NEW
    ``check_id``/version, never an in-place mutation.
    """

    __slots__ = (
        "applicability",
        "check_id",
        "description",
        "evaluator",
        "severity",
        "version",
    )

    def __init__(
        self,
        *,
        check_id: str,
        description: str,
        severity: DataQualitySeverity,
        applicability: frozenset[FactType],
        version: str,
        evaluator: Callable[[object], QualityFinding | None],
    ) -> None:
        if not check_id or not str(check_id).strip():
            raise ValueError("check_id must be a non-empty stable quality code")
        if not description or not str(description).strip():
            raise ValueError("description must be a non-empty string")
        if not applicability:
            raise ValueError("applicability must declare at least one fact type")
        if not version or not str(version).strip():
            raise ValueError("version must be a non-empty string")
        self.check_id = check_id
        self.description = description
        self.severity = severity
        self.applicability = applicability
        self.version = version
        self.evaluator = evaluator

    def finding(
        self,
        *,
        affected_fact_type: str,
        affected_fact_id: str,
        detail: str,
    ) -> QualityFinding:
        """Wrap a fired condition into a canonical ``QualityFinding``."""
        return QualityFinding(
            quality_code=self.check_id,
            severity=self.severity,
            description=f"{self.description}: {detail}",
            affected_fact_type=affected_fact_type,
            affected_fact_id=affected_fact_id,
            check_version=self.version,
        )


class QualityCheckRegistry:
    """Centralized deterministic quality-check registry (Part 6/37).

    Mirrors the governance style of ``RuleRegistry``/``RuleEvaluatorRegistry``:
    duplicate identities are rejected, lookup is O(1), iteration is
    deterministic (sorted by check_id). Pure — no I/O, no unbounded state.
    """

    def __init__(self) -> None:
        self._checks: dict[str, QualityCheck] = {}

    def register(self, check: QualityCheck) -> None:
        """Register one quality check.

        Raises:
            ValueError: the check_id is already registered — nothing is
                silently overwritten.
        """
        if not isinstance(check, QualityCheck):
            raise ValueError(f"quality check must be a QualityCheck, got {type(check).__name__}")
        if check.check_id in self._checks:
            raise ValueError(
                f"quality check {check.check_id!r} is already registered — "
                "checks are immutable; register a new check_id instead"
            )
        self._checks[check.check_id] = check

    def get(self, check_id: str) -> QualityCheck | None:
        """Safe lookup: the registered check or None."""
        return self._checks.get(check_id)

    def has(self, check_id: str) -> bool:
        """Whether the check identity is registered."""
        return check_id in self._checks

    def list(self) -> tuple[QualityCheck, ...]:
        """All registered checks in deterministic (sorted) order."""
        return tuple(self._checks[key] for key in sorted(self._checks))


# =============================================================================
# The implemented checks (Part 7 A/B/C/D/E/G/H)
# =============================================================================


def _fact_type_of(fact: object) -> FactType:
    """Deterministic isinstance dispatch mirroring the registry's mapping."""
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
    raise TypeError(f"not a canonical temporal fact: {type(fact).__name__}")


def _fact_id_of(fact: object) -> str:
    """The affected fact's canonical identity (interval/transition/snapshot id)."""
    for attr in ("interval_id", "transition_id", "snapshot_id", "measurement_id"):
        value = getattr(fact, attr, None)
        if value is not None:
            return str(value)
    return type(fact).__name__


def _build_identity_check() -> QualityCheck:
    """Part 7A — missing source/subject identity (camera_id / track_id).

    tenant/venue/session are deliberately NOT part of this check: the
    engine (Task 16.3) requires full scope provenance for any MATCH, so an
    un-attributable fact is INVALID input, never a quality event (Parts
    12/22/23).
    """

    def evaluate(fact: object) -> QualityFinding | None:
        key = fact.key  # type: ignore[attr-defined]
        missing = [name for name in ("camera_id", "track_id") if getattr(key, name, None) is None]
        if not missing:
            return None
        return check.finding(
            affected_fact_type=_fact_type_of(fact).value,
            affected_fact_id=_fact_id_of(fact),
            detail=f"missing required identity: {', '.join(sorted(missing))}",
        )

    check = QualityCheck(
        check_id="DATA_MISSING_REQUIRED_IDENTITY",
        description="Source/subject identity (camera_id, track_id) missing from the fact key",
        severity=DataQualitySeverity.ERROR,
        applicability=frozenset({
            FactType.TEMPORAL_TRANSITION,
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
            FactType.MOVEMENT_MEASUREMENT,
            FactType.MOVEMENT_CLASSIFICATION_TRANSITION,
            FactType.WAITING_INTERVAL,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_invalid_event_time() -> QualityCheck:
    """Part 7B — the fact's event-time field is missing."""

    def evaluate(fact: object) -> QualityFinding | None:
        event_time = getattr(fact, "event_time", None)
        last_seen = getattr(fact, "last_seen", None)
        if event_time is not None or last_seen is not None:
            return None
        return check.finding(
            affected_fact_type=_fact_type_of(fact).value,
            affected_fact_id=_fact_id_of(fact),
            detail="fact carries no event_time/last_seen",
        )

    check = QualityCheck(
        check_id="DATA_INVALID_EVENT_TIME",
        description="The fact's event-time field (event_time/last_seen) is missing",
        severity=DataQualitySeverity.ERROR,
        applicability=frozenset({
            FactType.TEMPORAL_TRANSITION,
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
            FactType.MOVEMENT_MEASUREMENT,
            FactType.MOVEMENT_CLASSIFICATION_TRANSITION,
            FactType.WAITING_INTERVAL,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_non_monotonic_event_time() -> QualityCheck:
    """Part 7C — impossible event-time ordering within the fact."""

    def evaluate(fact: object) -> QualityFinding | None:
        if isinstance(fact, (DwellInterval, WaitingInterval)):
            start = fact.dwell_start if isinstance(fact, DwellInterval) else fact.waiting_start
            end = fact.dwell_end if isinstance(fact, DwellInterval) else fact.waiting_end
            last_seen = fact.last_seen
            if (end is not None and start is not None and end < start) or (
                last_seen is not None and start is not None and last_seen < start
            ):
                return check.finding(
                    affected_fact_type=_fact_type_of(fact).value,
                    affected_fact_id=_fact_id_of(fact),
                    detail=("end/last_seen precedes the interval start — non-monotonic event time"),
                )
        if isinstance(fact, MovementMeasurement) and (
            fact.event_time is not None
            and fact.previous_event_time is not None
            and fact.event_time < fact.previous_event_time
        ):
            return check.finding(
                affected_fact_type=_fact_type_of(fact).value,
                affected_fact_id=_fact_id_of(fact),
                detail="measurement event_time precedes previous_event_time",
            )
        return None

    check = QualityCheck(
        check_id="DATA_NON_MONOTONIC_EVENT_TIME",
        description="Impossible event-time ordering within the canonical fact",
        severity=DataQualitySeverity.ERROR,
        applicability=frozenset({
            FactType.DWELL_INTERVAL,
            FactType.WAITING_INTERVAL,
            FactType.MOVEMENT_MEASUREMENT,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_negative_duration() -> QualityCheck:
    """Part 7G — a negative derived duration on the fact."""

    def evaluate(fact: object) -> QualityFinding | None:
        if isinstance(fact, (DwellInterval, WaitingInterval)) and (
            fact.duration_seconds is not None and fact.duration_seconds < 0
        ):
            return check.finding(
                affected_fact_type=_fact_type_of(fact).value,
                affected_fact_id=_fact_id_of(fact),
                detail=f"duration_seconds={fact.duration_seconds} < 0",
            )
        if isinstance(fact, MovementMeasurement) and (
            fact.time_delta_seconds is not None and fact.time_delta_seconds < 0
        ):
            return check.finding(
                affected_fact_type=_fact_type_of(fact).value,
                affected_fact_id=_fact_id_of(fact),
                detail=f"time_delta_seconds={fact.time_delta_seconds} < 0",
            )
        return None

    check = QualityCheck(
        check_id="DATA_NEGATIVE_DURATION",
        description="The fact carries a negative duration",
        severity=DataQualitySeverity.ERROR,
        applicability=frozenset({
            FactType.DWELL_INTERVAL,
            FactType.WAITING_INTERVAL,
            FactType.MOVEMENT_MEASUREMENT,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_temporal_inconsistency() -> QualityCheck:
    """Part 7G — contradictory canonical state.

    An interval flagged ``qualified`` while its duration is below the
    configured minimum is a contradiction; an occupancy snapshot whose
    counts do not reconcile violates the count invariant.
    """

    def evaluate(fact: object) -> QualityFinding | None:
        if isinstance(fact, DwellInterval) and (
            fact.qualified
            and fact.duration_seconds is not None
            and fact.duration_seconds < fact.minimum_dwell_seconds
        ):
            return check.finding(
                affected_fact_type=_fact_type_of(fact).value,
                affected_fact_id=_fact_id_of(fact),
                detail=(
                    f"qualified=True but duration {fact.duration_seconds} < "
                    f"minimum_dwell_seconds {fact.minimum_dwell_seconds}"
                ),
            )
        if isinstance(fact, WaitingInterval) and (
            fact.qualified
            and fact.duration_seconds is not None
            and fact.duration_seconds < fact.minimum_waiting_seconds
        ):
            return check.finding(
                affected_fact_type=_fact_type_of(fact).value,
                affected_fact_id=_fact_id_of(fact),
                detail=(
                    f"qualified=True but duration {fact.duration_seconds} < "
                    f"minimum_waiting_seconds {fact.minimum_waiting_seconds}"
                ),
            )
        if isinstance(fact, OccupancySnapshot) and (
            fact.previous_count + fact.delta != fact.occupancy_count
        ):
            return check.finding(
                affected_fact_type=_fact_type_of(fact).value,
                affected_fact_id=_fact_id_of(fact),
                detail=(
                    f"previous_count {fact.previous_count} + delta {fact.delta} "
                    f"!= occupancy_count {fact.occupancy_count}"
                ),
            )
        return None

    check = QualityCheck(
        check_id="DATA_TEMPORAL_INCONSISTENCY",
        description="Contradictory canonical state (qualified below minimum / unreconciled counts)",
        severity=DataQualitySeverity.ERROR,
        applicability=frozenset({
            FactType.DWELL_INTERVAL,
            FactType.WAITING_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_invalid_provenance() -> QualityCheck:
    """Part 7D — missing/blank provenance on the fact."""

    def evaluate(fact: object) -> QualityFinding | None:
        issues: list[str] = []
        fsm_version = getattr(fact, "fsm_version", None)
        policy_revision = getattr(fact, "policy_revision", None)
        if fsm_version is None or not str(fsm_version).strip():
            issues.append("fsm_version missing/blank")
        if policy_revision is None or not str(policy_revision).strip():
            issues.append("policy_revision missing/blank")
        key = fact.key  # type: ignore[attr-defined]
        if getattr(key, "configuration_version_id", None) is None:
            issues.append("key configuration_version_id missing")
        if not issues:
            return None
        return check.finding(
            affected_fact_type=_fact_type_of(fact).value,
            affected_fact_id=_fact_id_of(fact),
            detail="; ".join(issues),
        )

    check = QualityCheck(
        check_id="DATA_INVALID_PROVENANCE",
        description="Missing/blank provenance (fsm_version, policy_revision, configuration version)",
        severity=DataQualitySeverity.WARNING,
        applicability=frozenset({
            FactType.TEMPORAL_TRANSITION,
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
            FactType.MOVEMENT_MEASUREMENT,
            FactType.MOVEMENT_CLASSIFICATION_TRANSITION,
            FactType.WAITING_INTERVAL,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_unknown_spatial_reference(known_contexts: frozenset[str]) -> QualityCheck:
    """Part 7E — semantic_context set but not in the EXPLICIT known set.

    Never recomputes geometry — it only validates the canonical spatial
    identity against the configured catalog (Part 24).
    """

    def evaluate(fact: object) -> QualityFinding | None:
        key = fact.key  # type: ignore[attr-defined]
        semantic_context = getattr(key, "semantic_context", None)
        if semantic_context is None:
            return None
        if semantic_context in known_contexts:
            return None
        return check.finding(
            affected_fact_type=_fact_type_of(fact).value,
            affected_fact_id=_fact_id_of(fact),
            detail=f"semantic_context {semantic_context!r} not in the configured known set",
        )

    check = QualityCheck(
        check_id="DATA_UNKNOWN_SPATIAL_REFERENCE",
        description="Fact references a spatial context outside the configured known set",
        severity=DataQualitySeverity.WARNING,
        applicability=frozenset({
            FactType.TEMPORAL_TRANSITION,
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
            FactType.MOVEMENT_MEASUREMENT,
            FactType.MOVEMENT_CLASSIFICATION_TRANSITION,
            FactType.WAITING_INTERVAL,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def _check_configuration_mismatch(input_config_version: object) -> QualityCheck:
    """Part 7H — the fact's key config version differs from the evaluation's.

    Never silently switches configuration (Part 17/32): the finding is
    reported, and the event preserves the evaluation's pinned version.
    """

    def evaluate(fact: object) -> QualityFinding | None:
        key = fact.key  # type: ignore[attr-defined]
        key_version = getattr(key, "configuration_version_id", None)
        if key_version is None:
            return None  # missing → DATA_INVALID_PROVENANCE, not a mismatch
        if str(key_version) == str(input_config_version):
            return None
        return check.finding(
            affected_fact_type=_fact_type_of(fact).value,
            affected_fact_id=_fact_id_of(fact),
            detail=(
                f"fact key configuration {key_version} != evaluation "
                f"configuration {input_config_version}"
            ),
        )

    check = QualityCheck(
        check_id="DATA_CONFIGURATION_MISMATCH",
        description="Fact key configuration version differs from the evaluation's pinned version",
        severity=DataQualitySeverity.CRITICAL,
        applicability=frozenset({
            FactType.TEMPORAL_TRANSITION,
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
            FactType.MOVEMENT_MEASUREMENT,
            FactType.MOVEMENT_CLASSIFICATION_TRANSITION,
            FactType.WAITING_INTERVAL,
        }),
        version="v1",
        evaluator=evaluate,
    )
    return check


def build_quality_check_registry(
    *,
    known_spatial_contexts: frozenset[str] = frozenset(),
    configuration_version_id: object | None = None,
) -> QualityCheckRegistry:
    """Build the deterministic quality-check registry (Part 6/37).

    The spatial-reference and configuration-mismatch checks are
    parameterized by the EXPLICIT evaluation configuration — never by a
    "latest" lookup. All other checks are configuration-independent.
    """
    registry = QualityCheckRegistry()
    registry.register(_build_identity_check())
    registry.register(_check_invalid_event_time())
    registry.register(_check_non_monotonic_event_time())
    registry.register(_check_negative_duration())
    registry.register(_check_temporal_inconsistency())
    registry.register(_check_invalid_provenance())
    registry.register(_check_unknown_spatial_reference(known_spatial_contexts))
    if configuration_version_id is not None:
        registry.register(_check_configuration_mismatch(configuration_version_id))
    return registry


# =============================================================================
# The evaluator (Part 3)
# =============================================================================


class DataQualityEventEvaluator:
    """Deterministic, side-effect-free evaluator for ``data_quality``.

    Pure (Part 38/40): no database, Redis, S3, HTTP, LLM, wall clock,
    randomness, frame decoding, or geometry computation. Reads only the
    explicit ``RuleEvaluationInput`` and runs the deterministic
    quality-check registry against the primary fact.
    """

    evaluator_id = DATA_QUALITY_EVALUATOR_ID

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        fact = inp.facts[0]

        # Structural INVALID gates (Part 12) — malformed input, never MATCH.
        if not isinstance(
            fact,
            (
                TemporalTransition,
                DwellInterval,
                OccupancySnapshot,
                MovementMeasurement,
                MovementClassificationTransition,
                WaitingInterval,
            ),
        ):
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"data_quality requires a canonical temporal fact, got {type(fact).__name__}"
                ),
            )
        key = fact.key
        if key.tenant_id is None or key.venue_id is None or key.session_id is None:
            # An un-attributable fact can never emit an event: the engine
            # requires full tenant/venue/session provenance on MATCH
            # (Parts 12/22/23). INVALID is the only correct classification.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    "data_quality cannot attribute the fact: tenant_id, "
                    "venue_id and session_id are all required for an event"
                ),
            )

        known_raw = inp.configuration.get(KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY)
        known_contexts = self._known_contexts_of(known_raw)
        if known_contexts is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"configuration key {KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY!r} "
                    "must be a list of non-empty spatial-context strings"
                ),
            )

        registry = build_quality_check_registry(
            known_spatial_contexts=known_contexts,
            configuration_version_id=inp.configuration_version_id,
        )
        findings = [
            finding
            for check in registry.list()
            if _fact_type_of(fact) in check.applicability
            and (finding := check.evaluator(fact)) is not None
        ]

        if not findings:
            # Part 11 — all applicable checks pass: NO_MATCH, no envelope,
            # no evidence, no side effect.
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

        # Part 14 — deterministic ordering by quality_code (never random,
        # never dict-iteration semantics).
        findings.sort(key=lambda f: f.quality_code)
        event = self._build_event(rule, inp, fact, findings)
        from backend.app.intelligence.rules.evaluator import deterministic_evidence_ref

        evidence = (deterministic_evidence_ref(rule, inp, event_id=event.event_id),)
        return RuleEvaluationResult(
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            status=RuleEvaluationStatus.MATCH,
            event_time=inp.event_time,
            configuration_version_id=inp.configuration_version_id,
            event=event,
            evidence_requests=evidence,
            tenant_id=key.tenant_id,
            venue_id=key.venue_id,
            session_id=key.session_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _known_contexts_of(raw: object) -> frozenset[str] | None:
        """Validate + normalize the explicit known-spatial-context config.

        Returns None (→ INVALID) unless the value is a list/tuple of
        non-empty strings — never a silent default (Part 6).
        """
        if not isinstance(raw, (list, tuple)) or len(raw) == 0:
            # An empty catalog is a legitimate explicit value: no context is
            # known → any semantic_context is unknown. Missing/non-list → INVALID.
            if isinstance(raw, (list, tuple)):
                return frozenset()
            return None
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                return None
        return frozenset(raw)

    @staticmethod
    def _build_event(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        fact: object,
        findings: list[QualityFinding],
    ) -> EventEnvelope[DataQualityPayload]:
        """Construct the canonical deterministic EventEnvelope (Part 20)."""
        from backend.app.intelligence.rules.evaluator import deterministic_event_id

        key = fact.key  # type: ignore[attr-defined]
        event_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        primary = findings[0]
        payload = DataQualityPayload(
            findings=tuple(findings),
            primary_quality_code=primary.quality_code,
            primary_severity=primary.severity,
            affected_fact_type=_fact_type_of(fact).value,
            affected_fact_id=_fact_id_of(fact),
            tenant_id=key.tenant_id,
            venue_id=key.venue_id,
            session_id=key.session_id,
            camera_id=key.camera_id,
            track_id=key.track_id,
            spatial_context_id=getattr(key, "semantic_context", None),
            event_time=inp.event_time,
            configuration_version_id=inp.configuration_version_id,
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
        )
        return EventEnvelope(
            event_id=event_id,
            event_type=rule.output_event_type.value,
            event_time=inp.event_time,
            produced_at=inp.processing_time or inp.event_time,
            source=f"rule:{rule.canonical_identity}",
            payload=payload,
        )
