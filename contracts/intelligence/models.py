"""Canonical evidence/intelligence contract models.

Architecture:
    Observation (what happened) -> Finding (what evidence supports)
    -> Recommendation (what should be done)

Do NOT merge these concepts. LLMs consume bounded evidence, they do not
replace deterministic operational truth.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    EvidenceId,
    FindingId,
    OpportunityId,
    RecommendationId,
    validate_schema_version,
    validate_utc,
)
from contracts.events.evidence import EvidenceRef


class EvidencePackage(BaseModel, frozen=True):
    """A bounded package of evidence used for investigation/reasoning.

    References evidence rather than embedding arbitrary large media.
    Supports evidence-first AI reasoning.
    """

    model_config = {"extra": "forbid"}

    package_id: EvidenceId
    schema_version: str = Field(default=SCHEMA_VERSION)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: datetime
    description: str | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)


class Finding(BaseModel, frozen=True):
    """A conclusion supported by evidence.

    Distinguishable from raw observations (what happened) and
    recommendations (what should be done).
    """

    model_config = {"extra": "forbid"}

    finding_id: FindingId
    schema_version: str = Field(default=SCHEMA_VERSION)
    evidence_package_id: EvidenceId
    description: str = Field(min_length=1)
    event_time: datetime
    finding_type: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)


class Priority(StrEnum):
    """Priority level for recommendations and actions."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Recommendation(BaseModel, frozen=True):
    """An evidence-grounded proposed action/advice.

    Must be traceable back to findings and evidence.
    The LLM is the reasoning engine, NOT the source of truth.
    """

    model_config = {"extra": "forbid"}

    recommendation_id: RecommendationId
    schema_version: str = Field(default=SCHEMA_VERSION)
    finding_ids: list[FindingId] = Field(default_factory=list)
    opportunity_id: OpportunityId | None = None
    description: str = Field(min_length=1)
    priority: Priority = Priority.MEDIUM
    created_at: datetime

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)
