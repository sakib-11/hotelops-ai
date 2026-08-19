"""Deterministic configuration validation (Task 10.9)."""

from backend.app.domain.configuration.validation.codes import RuleCode, severity_of
from backend.app.domain.configuration.validation.engine import (
    VALIDATOR_VERSION,
    CameraStatusResolver,
    ConfigurationValidationEngine,
    ValidationOutcome,
    ValidatorContext,
)
from backend.app.domain.configuration.validation.findings import Finding, FindingCollector
from backend.app.domain.configuration.validation.policy import (
    DEFAULT_SPATIAL_POLICY_REGISTRY,
    EntityKind,
    RelationshipPolicy,
    SpatialPolicyRegistry,
)
from backend.app.domain.configuration.validation.spatial import (
    AREA_TOLERANCE,
    SpatialEngine,
    SpatialMath,
)
from backend.app.domain.configuration.validation.validators import run_all_validators

__all__ = [
    "AREA_TOLERANCE",
    "DEFAULT_SPATIAL_POLICY_REGISTRY",
    "VALIDATOR_VERSION",
    "CameraStatusResolver",
    "ConfigurationValidationEngine",
    "EntityKind",
    "Finding",
    "FindingCollector",
    "RelationshipPolicy",
    "RuleCode",
    "SpatialEngine",
    "SpatialMath",
    "SpatialPolicyRegistry",
    "ValidationOutcome",
    "ValidatorContext",
    "run_all_validators",
    "severity_of",
]
