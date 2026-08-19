"""Canonical deterministic rule registry contracts (Task 16.1).

The architecture + contract foundation for Task 16: strongly typed,
immutable, versioned ``RuleDefinition`` objects, a deterministic
``RuleEvaluationResult`` contract (NO_MATCH / MATCH / SUPPRESSED /
INVALID), the explicit versioned evaluation input, and the controlled
vocabularies (rule identifiers, event types, fact types). The individual
business rules are later Task 16 steps and are not implemented here.
"""

from contracts.rules.models import (
    EVIDENCE_REQUIREMENT_NONE,
    RULE_VERSION_PATTERN,
    CooldownPolicy,
    DataQualityPayload,
    DataQualitySeverity,
    DwellThresholdPayload,
    EvidenceRequirement,
    FactType,
    OccupancySessionPayload,
    OccupancySessionPhase,
    QualityFinding,
    QueueCandidatePayload,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
    ServiceGapCandidatePayload,
    TemporalFact,
    TurnoverDelayPayload,
    validate_rule_version,
)

__all__ = [
    "EVIDENCE_REQUIREMENT_NONE",
    "RULE_VERSION_PATTERN",
    "CooldownPolicy",
    "DataQualityPayload",
    "DataQualitySeverity",
    "DwellThresholdPayload",
    "EvidenceRequirement",
    "FactType",
    "OccupancySessionPayload",
    "OccupancySessionPhase",
    "QualityFinding",
    "QueueCandidatePayload",
    "RuleDefinition",
    "RuleEvaluationInput",
    "RuleEvaluationResult",
    "RuleEvaluationStatus",
    "RuleEventType",
    "RuleIdentifier",
    "ServiceGapCandidatePayload",
    "TemporalFact",
    "TurnoverDelayPayload",
    "validate_rule_version",
]
