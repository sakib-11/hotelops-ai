"""Canonical analysis job contract.

Represents a requested/bounded analysis workload. Supports the architecture
where live and recorded processing share the same downstream pipeline.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    AnalysisJobId,
    VideoAssetId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)


class JobStatus(StrEnum):
    """Lifecycle state of an analysis job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisJob(BaseModel, frozen=True):
    """A requested/bounded unit of analysis work.

    Describes what to analyze, not how to execute it.
    """

    model_config = {"extra": "forbid"}

    job_id: AnalysisJobId
    schema_version: str = Field(default=SCHEMA_VERSION)
    session_id: VideoSessionId | None = None
    asset_id: VideoAssetId | None = None
    job_type: str = Field(min_length=1)
    status: JobStatus = JobStatus.PENDING
    created_at: datetime
    config: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)
