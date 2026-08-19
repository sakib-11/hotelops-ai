"""Deterministic configuration validation engine (Task 10.9).

The engine:
  - is READ-ONLY (never mutates the version)
  - validates ONE exact configuration version snapshot
  - collects independent errors instead of failing on the first
  - suppresses cascading spatial errors when geometry/reference failed
  - records ``validator_version`` and the exact ``content_revision`` so
    a stale validation result can never authorize publication
  - is deterministic: same content + same validator/policy version
    always produce the same result
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.app.domain.configuration.validation.codes import RuleCode, severity_of
from backend.app.domain.configuration.validation.findings import Finding, FindingCollector
from backend.app.domain.configuration.validation.policy import SpatialPolicyRegistry
from backend.app.domain.configuration.validation.spatial import SpatialEngine, SpatialMath
from backend.app.domain.configuration.validation.validators import (
    VALIDATOR_VERSION,
    CameraStatusResolver,
    ValidatorContext,
    run_all_validators,
)
from contracts.common import ConfigurationId, ConfigurationVersionId, TenantId, VenueId, utc_now
from contracts.configuration import (
    ConfigurationVersionModel,
    ValidationFindingModel,
    ValidationResultModel,
)

__all__ = [
    "VALIDATOR_VERSION",
    "CameraStatusResolver",
    "ConfigurationValidationEngine",
    "ValidationOutcome",
    "ValidatorContext",
]


@dataclass(frozen=True)
class ValidationOutcome:
    """Plain result of one validation pass (pre-persistence)."""

    valid: bool
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    checks_performed: int = 0

    def to_result_model(
        self,
        *,
        version: ConfigurationVersionModel,
        content_revision: str,
        validated_by: str,
        validated_at: datetime | None = None,
    ) -> ValidationResultModel:
        """Build the persisted, revision-bound ValidationResultModel."""
        now = validated_at or utc_now()
        return ValidationResultModel(
            valid=self.valid,
            validator_version=VALIDATOR_VERSION,
            content_revision=content_revision,
            configuration_version_id=version.configuration_version_id,
            configuration_id=version.configuration_id,
            tenant_id=version.tenant_id,
            venue_id=version.venue_id,
            validated_at=now,
            validated_by=validated_by,
            errors=[_to_finding_model(f) for f in self.errors],
            warnings=[_to_finding_model(f) for f in self.warnings],
            checks_performed=self.checks_performed,
        )


def _to_finding_model(finding: Finding) -> ValidationFindingModel:
    return ValidationFindingModel(
        code=finding.code.value,
        severity=finding.severity,
        message=finding.message,
        entity_type=finding.entity_type,
        entity_id=finding.entity_id,
        related_entity_id=finding.related_entity_id,
    )


class ConfigurationValidationEngine:
    """Runs the modular validators over one exact configuration version.

    Deterministic by construction: iteration order is fixed (list order
    preserved), the spatial math is pure, and no wall-clock state,
    random IDs, or database ordering participates in the decision.
    """

    def __init__(
        self,
        *,
        spatial: SpatialEngine | None = None,
        policies: SpatialPolicyRegistry | None = None,
        camera_resolver: CameraStatusResolver | None = None,
    ) -> None:
        self._context = ValidatorContext(
            spatial=spatial or SpatialMath(),
            policies=policies or SpatialPolicyRegistry.default(),
            camera_resolver=camera_resolver,
        )

    @property
    def validator_version(self) -> str:
        return VALIDATOR_VERSION

    async def validate(self, version: ConfigurationVersionModel) -> ValidationOutcome:
        """Validate the version snapshot; returns findings (no mutation).

        ``content_revision`` is captured at validation time — the caller
        stores it with the result so publication can detect staleness.
        """
        collector = FindingCollector()
        await run_all_validators(version, collector, self._context)
        errors = [f for f in collector.findings if f.is_error]
        warnings = [f for f in collector.findings if not f.is_error]
        return ValidationOutcome(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            checks_performed=collector.checks_performed,
        )


__all__ = [
    "UTC",
    "VALIDATOR_VERSION",
    "CameraStatusResolver",
    "ConfigurationId",
    "ConfigurationValidationEngine",
    "ConfigurationVersionId",
    "RuleCode",
    "TenantId",
    "ValidationOutcome",
    "ValidatorContext",
    "VenueId",
    "datetime",
    "severity_of",
    "utc_now",
]
