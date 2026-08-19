"""Canonical API response DTOs for the operational vertical slice (Task 18.12).

The retrieval surface for the vertical-slice occupancy fact/event. These
are the ONLY wire shapes the API may return for these resources — the
internal ORM rows (``OperationalEventModel`` / ``TemporalFactModel``)
are NEVER exposed directly (the route's ``response_model`` and the
service's mapping boundary enforce this).

The DTOs carry the canonical contract values:

  OccupancyEventResponse — the domain event (Task 16): envelope metadata
      (event identity, source, times, correlation/causation) plus the
      canonical ``OccupancySessionPayload`` verbatim.
  OccupancyFactResponse  — the canonical business fact (Task 15): the
      typed fact scope columns plus the canonical ``OccupancySnapshot``
      payload verbatim.

The payloads are the canonical Task 15/16 contracts (never re-derived,
never re-typed): a stored payload that is not the canonical occupancy
contract fails validation deterministically instead of leaking a
different shape through the occupancy surface.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    EventId,
    TenantId,
    VenueId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)
from contracts.rules import OccupancySessionPayload
from contracts.temporal import OccupancySnapshot

__all__ = [
    "EvidenceAvailabilityResponse",
    "OccupancyEventResponse",
    "OccupancyFactResponse",
]


class OccupancyEventResponse(BaseModel, frozen=True):
    """Canonical retrieval DTO for one occupancy_session domain event.

    ``event_type`` is pinned to the canonical ``occupancy_session``
    vocabulary value — this surface only ever represents occupancy
    events; an event of any other type cannot be retrieved through it
    (the service maps that to not-found, never to a mismatched DTO).
    """

    model_config = {"extra": "forbid"}

    event_id: EventId
    event_type: str = Field(min_length=1, max_length=100)
    schema_version: str = Field(default=SCHEMA_VERSION)
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId | None = None
    camera_id: CameraId | None = None
    event_time: datetime
    produced_at: datetime
    source: str = Field(min_length=1, max_length=255)
    correlation_id: str | None = None
    causation_id: str | None = None
    # The canonical Task 16 payload — verbatim, never re-typed.
    payload: OccupancySessionPayload

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_produced = field_validator("produced_at")(validate_utc)


class EvidenceAvailabilityResponse(BaseModel, frozen=True):
    """Canonical DTO: whether evidence exists for one operational event.

    Evidence availability is a SERVER-derived fact (the durable
    event → evidence request linkage of Task 18.9, persisted by the
    Task 17.11 worker) — the desktop never derives it; it reads this
    canonical shape through the authorized retrieval surface. The
    tenant/venue scope is the same as the event itself (an event
    outside the actor's scope is 404, never an availability answer).
    """

    model_config = {"extra": "forbid"}

    event_id: EventId
    available: bool
    # The deterministic evidence request identity (ref_id) when a
    # linkage row exists — the desktop may display it as a reference.
    evidence_ref_id: EventId | None = None


class OccupancyFactResponse(BaseModel, frozen=True):
    """Canonical retrieval DTO for one occupancy snapshot business fact.

    ``fact_type`` is pinned to the canonical ``occupancy_snapshot``
    vocabulary value; the scope columns mirror the typed fact row and
    the payload is the canonical Task 15 ``OccupancySnapshot`` verbatim.
    """

    model_config = {"extra": "forbid"}

    fact_id: EventId
    fact_type: str = Field(min_length=1, max_length=100)
    fsm_kind: str = Field(min_length=1, max_length=50)
    schema_version: str = Field(default=SCHEMA_VERSION)
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId | None = None
    camera_id: CameraId | None = None
    configuration_version_id: ConfigurationVersionId | None = None
    event_time: datetime
    source_transition_id: EventId | None = None
    fsm_version: str = Field(min_length=1, max_length=50)
    policy_revision: str | None = None
    # The canonical Task 15 payload — verbatim, never re-typed.
    payload: OccupancySnapshot

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
