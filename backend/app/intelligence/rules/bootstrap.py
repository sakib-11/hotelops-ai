"""Sanctioned wiring for the Task 16 operational rules (Part 25/26).

The operational rules are NEVER instantiated manually inside random
application modules. This module is the single sanctioned entry point:
it registers the operational rule definitions (Task 16.1 ``RuleRegistry``)
and their evaluator implementations (Task 16.3 ``RuleEvaluatorRegistry``)
and returns a ready ``RuleEvaluationEngine``.

Part 25/26 verification contract:

    registry.get(occupancy_session, requested_version)  # -> RuleDefinition
    registry.get(dwell_threshold, requested_version)    # -> RuleDefinition
    registry.get(queue_candidate, requested_version)    # -> RuleDefinition
    evaluators.resolve(rule.evaluator_id)               # -> the evaluator

All are satisfied by construction here; ``build_operational_engine``
returns the composed engine so application code never touches the
registries directly.
"""

from __future__ import annotations

from backend.app.intelligence.rules.data_quality import (
    DATA_QUALITY_EVALUATOR_ID,
    DataQualityEventEvaluator,
    data_quality_definition,
)
from backend.app.intelligence.rules.dwell_threshold import (
    DWELL_THRESHOLD_EVALUATOR_ID,
    DwellThresholdEvaluator,
    dwell_threshold_definition,
)
from backend.app.intelligence.rules.engine import RuleEvaluationEngine
from backend.app.intelligence.rules.evaluator import RuleEvaluatorRegistry
from backend.app.intelligence.rules.occupancy_session import (
    OCCUPANCY_SESSION_EVALUATOR_ID,
    OccupancySessionEvaluator,
    occupancy_session_definition,
)
from backend.app.intelligence.rules.queue_candidate import (
    QUEUE_CANDIDATE_EVALUATOR_ID,
    QUEUE_CANDIDATE_EVALUATOR_V2_ID,
    QueueCandidateEvaluator,
    QueueCandidateEvaluatorV2,
    queue_candidate_definition,
    queue_candidate_definition_v2,
)
from backend.app.intelligence.rules.registry import RuleRegistry
from backend.app.intelligence.rules.service_gap_candidate import (
    SERVICE_GAP_EVALUATOR_ID,
    ServiceGapCandidateEvaluator,
    service_gap_candidate_definition,
)
from backend.app.intelligence.rules.turnover_delay import (
    TURNOVER_DELAY_EVALUATOR_ID,
    TurnoverDelayEvaluator,
    turnover_delay_definition,
)

__all__ = [
    "build_operational_engine",
    "build_operational_registries",
]


def build_operational_registries(
    *,
    rule_version: str = "v1",
) -> tuple[RuleRegistry, RuleEvaluatorRegistry]:
    """Build the registries with all operational rules registered.

    Registers the occupancy_session (16.4), dwell_threshold (16.5),
    queue_candidate (16.6, v1 + v2), service_gap_candidate (16.7),
    turnover_delay (16.8), and data_quality (16.9) rules + their
    evaluators. Returns ``(rule_registry, evaluator_registry)`` — the
    explicit pair so callers can verify Part 25/26 (registry.get /
    evaluator resolve).
    """
    evaluators = RuleEvaluatorRegistry()
    evaluators.register(OccupancySessionEvaluator())
    evaluators.register(DwellThresholdEvaluator())
    evaluators.register(QueueCandidateEvaluator())
    evaluators.register(QueueCandidateEvaluatorV2())
    evaluators.register(ServiceGapCandidateEvaluator())
    evaluators.register(TurnoverDelayEvaluator())
    evaluators.register(DataQualityEventEvaluator())

    registry = RuleRegistry(
        supported_evaluators=frozenset({
            OCCUPANCY_SESSION_EVALUATOR_ID,
            DWELL_THRESHOLD_EVALUATOR_ID,
            QUEUE_CANDIDATE_EVALUATOR_ID,
            QUEUE_CANDIDATE_EVALUATOR_V2_ID,
            SERVICE_GAP_EVALUATOR_ID,
            TURNOVER_DELAY_EVALUATOR_ID,
            DATA_QUALITY_EVALUATOR_ID,
        })
    )
    registry.register(occupancy_session_definition(version=rule_version))
    registry.register(dwell_threshold_definition(version=rule_version))
    registry.register(queue_candidate_definition(version=rule_version))
    registry.register(queue_candidate_definition_v2())
    registry.register(service_gap_candidate_definition(version=rule_version))
    registry.register(turnover_delay_definition(version=rule_version))
    registry.register(data_quality_definition(version=rule_version))
    return registry, evaluators


def build_operational_engine(*, rule_version: str = "v1") -> RuleEvaluationEngine:
    """Build the ready-to-evaluate engine with all operational rules wired.

    This is the ONLY sanctioned way application modules obtain an engine
    for the operational rules — no manual instantiation of rules or
    evaluators (Part 25).
    """
    registry, evaluators = build_operational_registries(rule_version=rule_version)
    return RuleEvaluationEngine(registry, evaluators)
