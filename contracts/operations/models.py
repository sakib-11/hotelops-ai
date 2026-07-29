"""Canonical operational/action contract models.

Architecture:
    Recommendation -> (ApprovalRequest) -> ActionCommand

Alert is a notification/operational signal.
It is NOT automatically an ActionCommand.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    ActionCommandId,
    AlertId,
    ApprovalRequestId,
    FindingId,
    RecommendationId,
    validate_schema_version,
    validate_utc,
)


class Severity(StrEnum):
    """Severity level for alerts."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Alert(BaseModel, frozen=True):
    """A notification/operational signal.

    Alerts inform operators about conditions requiring attention.
    An alert is not automatically an action command.
    """

    model_config = {"extra": "forbid"}

    alert_id: AlertId
    schema_version: str = Field(default=SCHEMA_VERSION)
    alert_type: str = Field(min_length=1)
    severity: Severity = Severity.INFO
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    event_time: datetime
    source_ref: FindingId | RecommendationId | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)


class ApprovalStatus(StrEnum):
    """Explicit approval lifecycle states."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalRequest(BaseModel, frozen=True):
    """A request for human approval before an action is taken.

    Makes approval state explicit. Does not implement persistence or UI.
    """

    model_config = {"extra": "forbid"}

    request_id: ApprovalRequestId
    schema_version: str = Field(default=SCHEMA_VERSION)
    recommendation_id: RecommendationId
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime
    resolved_at: datetime | None = None
    reason: str | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_requested = field_validator("requested_at")(validate_utc)
    _validate_resolved = field_validator("resolved_at")(validate_utc)


class ActionCommand(BaseModel, frozen=True):
    """A bounded command issued after sufficient authority is obtained.

    Represents an action the system has been authorized to perform.
    Does not execute the action — defines the command contract only.
    """

    model_config = {"extra": "forbid"}

    command_id: ActionCommandId
    schema_version: str = Field(default=SCHEMA_VERSION)
    approval_request_id: ApprovalRequestId | None = None
    command_type: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_issued = field_validator("issued_at")(validate_utc)
