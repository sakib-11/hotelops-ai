"""Canonical event transport envelope.

Separates event metadata from payload. Redis-specific fields are NOT part
of this contract — Redis is transport, not source of truth.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    EventId,
    validate_schema_version,
    validate_utc,
)


class EventEnvelope[PayloadT](BaseModel, frozen=True):
    """Canonical envelope wrapping all HotelOps AI events.

    Generic parameter PayloadT allows typed payloads at consumption points.
    """

    model_config = {"extra": "forbid"}

    event_id: EventId
    event_type: str = Field(min_length=1)
    schema_version: str = Field(default=SCHEMA_VERSION)
    event_time: datetime
    produced_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    source: str = Field(min_length=1)
    payload: PayloadT
    # Task 8.8 — async observability propagation (optional telemetry context;
    # absent = no upstream trace = start a new trace).
    trace_id: str | None = None
    span_id: str | None = None
    trace_sampled: bool | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_produced = field_validator("produced_at")(validate_utc)
