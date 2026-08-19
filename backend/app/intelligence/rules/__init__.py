"""Deterministic rule registry + evaluation engine (Tasks 16.1-16.5).

Task 16 converts canonical Task 15 temporal facts into operational events
deterministically (no LLM):

- ``RuleRegistry`` — versioned, immutable ``RuleDefinition`` catalog
  (16.1/16.2): explicit ``(rule_id, rule_version)`` identity, immutability,
  duplicate rejection, deterministic-version allowlist, and the canonical
  input boundary (fact types, configuration requirements, tenant/venue/
  session scope isolation).
- ``RuleEvaluatorRegistry`` + ``RuleEvaluator`` — the PURE deterministic
  decision layer (16.3): side-effect-free implementations keyed by
  ``evaluator_id``.
- ``RuleEvaluationEngine`` — the end-to-end pipeline (16.3): explicit rule
  resolution → input/configuration validation → evaluator execution →
  result validation → deterministic ``EventEnvelope`` / ``EvidenceRef``
  construction → typed ``RuleEvaluationResult``.
- ``occupancy_session`` (16.4) — the FIRST production operational rule:
  converts a confirmed Task 15.4 ``OccupancySnapshot`` boundary into an
  ``occupancy_session`` started/ended event.
- ``dwell_threshold`` (16.5) — the SECOND production operational rule:
  converts a canonical Task 15.3 ``DwellInterval`` into a
  ``dwell_threshold`` event when the configured dwell duration threshold
  is crossed.

Both are registered via ``build_operational_engine`` (the sanctioned
wiring, Part 25/26). No other business rules, cooldown execution, or
duplicate suppression are implemented here — those are later Task 16
steps.
"""

from backend.app.intelligence.rules.bootstrap import (
    build_operational_engine,
    build_operational_registries,
)
from backend.app.intelligence.rules.data_quality import (
    DATA_QUALITY_EVALUATOR_ID,
    KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY,
    DataQualityEventEvaluator,
    QualityCheck,
    QualityCheckRegistry,
    data_quality_definition,
)
from backend.app.intelligence.rules.dwell_threshold import (
    DWELL_THRESHOLD_CONFIG_KEY,
    DWELL_THRESHOLD_EVALUATOR_ID,
    DwellThresholdEvaluator,
    dwell_threshold_definition,
)
from backend.app.intelligence.rules.engine import RuleEvaluationEngine
from backend.app.intelligence.rules.evaluator import (
    EVIDENCE_ID_PREFIX,
    RuleEvaluator,
    RuleEvaluatorRegistry,
    deterministic_event_id,
    deterministic_evidence_ref,
)
from backend.app.intelligence.rules.evidence_request import (
    EvidenceRequestBuilder,
    EvidenceRequestParams,
    scope_params_from_envelope,
)
from backend.app.intelligence.rules.exceptions import (
    DuplicateEvaluatorError,
    DuplicateRuleError,
    InvalidEvidenceRequestError,
    InvalidRuleDefinitionError,
    InvalidRuleEvaluationError,
    InvalidRuleInputError,
    MissingRuleConfigurationError,
    MixedScopeRuleInputError,
    RuleConfigurationMismatchError,
    RuleError,
    RuleEvaluationExecutionError,
    UnknownRuleError,
    UnsupportedDeterministicVersionError,
    UnsupportedEvaluatorError,
    UnsupportedFactTypeError,
    UnsupportedRuleVersionError,
)
from backend.app.intelligence.rules.occupancy_session import (
    OCCUPANCY_SESSION_EVALUATOR_ID,
    OccupancySessionEvaluator,
    occupancy_session_definition,
)
from backend.app.intelligence.rules.queue_candidate import (
    QUEUE_AREA_IDS_CONFIG_KEY,
    QUEUE_CANDIDATE_CONFIG_KEY,
    QUEUE_CANDIDATE_EVALUATOR_ID,
    QUEUE_CANDIDATE_EVALUATOR_V2_ID,
    QUEUE_MAX_LENGTH_CONFIG_KEY,
    QueueCandidateEvaluator,
    QueueCandidateEvaluatorV2,
    queue_candidate_definition,
    queue_candidate_definition_v2,
)
from backend.app.intelligence.rules.registry import (
    RegistryValidation,
    RuleRegistry,
)
from backend.app.intelligence.rules.service_gap_candidate import (
    SERVICE_AREA_IDS_CONFIG_KEY,
    SERVICE_GAP_CONFIG_KEY,
    SERVICE_GAP_EVALUATOR_ID,
    ServiceGapCandidateEvaluator,
    service_gap_candidate_definition,
)
from backend.app.intelligence.rules.turnover_delay import (
    SERVICE_WINDOW_CONFIG_KEY,
    TURNOVER_DELAY_CONFIG_KEY,
    TURNOVER_DELAY_EVALUATOR_ID,
    TurnoverDelayEvaluator,
    turnover_delay_definition,
)

__all__ = [
    "DATA_QUALITY_EVALUATOR_ID",
    "DWELL_THRESHOLD_CONFIG_KEY",
    "DWELL_THRESHOLD_EVALUATOR_ID",
    "EVIDENCE_ID_PREFIX",
    "KNOWN_SPATIAL_CONTEXTS_CONFIG_KEY",
    "OCCUPANCY_SESSION_EVALUATOR_ID",
    "QUEUE_AREA_IDS_CONFIG_KEY",
    "QUEUE_CANDIDATE_CONFIG_KEY",
    "QUEUE_CANDIDATE_EVALUATOR_ID",
    "QUEUE_CANDIDATE_EVALUATOR_V2_ID",
    "QUEUE_MAX_LENGTH_CONFIG_KEY",
    "SERVICE_AREA_IDS_CONFIG_KEY",
    "SERVICE_GAP_CONFIG_KEY",
    "SERVICE_GAP_EVALUATOR_ID",
    "SERVICE_WINDOW_CONFIG_KEY",
    "TURNOVER_DELAY_CONFIG_KEY",
    "TURNOVER_DELAY_EVALUATOR_ID",
    "DataQualityEventEvaluator",
    "DuplicateEvaluatorError",
    "DuplicateRuleError",
    "DwellThresholdEvaluator",
    "EvidenceRequestBuilder",
    "EvidenceRequestParams",
    "InvalidEvidenceRequestError",
    "InvalidRuleDefinitionError",
    "InvalidRuleEvaluationError",
    "InvalidRuleInputError",
    "MissingRuleConfigurationError",
    "MixedScopeRuleInputError",
    "OccupancySessionEvaluator",
    "QualityCheck",
    "QualityCheckRegistry",
    "QueueCandidateEvaluator",
    "QueueCandidateEvaluatorV2",
    "RegistryValidation",
    "RuleConfigurationMismatchError",
    "RuleError",
    "RuleEvaluationEngine",
    "RuleEvaluationExecutionError",
    "RuleEvaluator",
    "RuleEvaluatorRegistry",
    "RuleRegistry",
    "ServiceGapCandidateEvaluator",
    "TurnoverDelayEvaluator",
    "UnknownRuleError",
    "UnsupportedDeterministicVersionError",
    "UnsupportedEvaluatorError",
    "UnsupportedFactTypeError",
    "UnsupportedRuleVersionError",
    "build_operational_engine",
    "build_operational_registries",
    "data_quality_definition",
    "deterministic_event_id",
    "deterministic_evidence_ref",
    "dwell_threshold_definition",
    "occupancy_session_definition",
    "queue_candidate_definition",
    "queue_candidate_definition_v2",
    "scope_params_from_envelope",
    "service_gap_candidate_definition",
    "turnover_delay_definition",
]
