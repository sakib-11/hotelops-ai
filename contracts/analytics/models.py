"""Canonical analytics contract models.

MetricValue          — a measured/computed metric with context.
OpportunityCandidate — a deterministic or analytical candidate for evaluation.

OpportunityCandidate is NOT automatically an AI recommendation.
Architecture: Deterministic Analytics -> OpportunityCandidate -> Evidence
-> AI Reasoning -> Recommendation. Keep these concepts separate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    AnalysisJobId,
    EvidenceId,
    OpportunityId,
    validate_schema_version,
    validate_utc,
)


class MetricValue(BaseModel, frozen=True):
    """A measured/computed metric with enough context to avoid ambiguous numbers.

    A metric is not simply 42.7 — it must be possible to understand
    what was measured, its unit, and its temporal context.
    """

    model_config = {"extra": "forbid"}

    metric_name: str = Field(min_length=1)
    value: float
    unit: str | None = None
    event_time: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    source_ref: AnalysisJobId | None = None
    metadata: dict[str, Any] | None = None

    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_window_start = field_validator("window_start")(validate_utc)
    _validate_window_end = field_validator("window_end")(validate_utc)


class OpportunityCandidate(BaseModel, frozen=True):
    """A deterministic or analytical candidate for further evaluation.

    Produced by deterministic analytics before any LLM reasoning.
    Represents a potential operational opportunity that needs evidence
    gathering and AI evaluation before becoming a recommendation.
    """

    model_config = {"extra": "forbid"}

    opportunity_id: OpportunityId
    schema_version: str = Field(default=SCHEMA_VERSION)
    description: str = Field(min_length=1)
    metric_values: list[MetricValue] = Field(default_factory=list)
    event_time: datetime
    evidence_refs: list[EvidenceId] = Field(default_factory=list)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
