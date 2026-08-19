"""Operational vertical-slice read service (Task 18.12).

The application boundary between the API routes and the tenant/venue-
scoped repository. It is the ONLY place where the internal ORM rows
(``OperationalEventModel`` / ``TemporalFactModel``) are mapped to the
canonical response DTOs — routes and callers never see ORM models.

Authorization is enforced at the repository (tenant filter in SQL +
venue access check); the service maps out-of-scope/missing records to
``None`` (the route maps that to 404) and maps in-scope records to the
canonical DTO. The repository is injectable so unit tests can drive the
service without a database.
"""

from __future__ import annotations

import uuid
from typing import Any

from backend.app.application.services.operational_errors import OperationalNotFoundError
from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.database.models.temporal import TemporalFactModel
from backend.app.infrastructure.database.repositories.operational import OperationalRepository
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    TenantId,
    VenueId,
    VideoSessionId,
)
from contracts.identity import ActorContext
from contracts.operational import (
    EvidenceAvailabilityResponse,
    OccupancyEventResponse,
    OccupancyFactResponse,
)
from contracts.rules import OccupancySessionPayload, RuleEventType
from contracts.temporal import OccupancySnapshot

__all__ = [
    "FACT_TYPE_OCCUPANCY_SNAPSHOT",
    "OperationalReadService",
]

# The controlled fact_type of the occupancy vertical slice (the same
# value the authoritative persistence boundary writes — Task 18.10).
FACT_TYPE_OCCUPANCY_SNAPSHOT = "occupancy_snapshot"


class OperationalReadService:
    """Scoped reads that map to canonical response DTOs."""

    def __init__(self, repository: OperationalRepository | None = None) -> None:
        self._repository = repository or OperationalRepository()

    async def get_event(
        self,
        *,
        session: Any,
        actor: ActorContext,
        event_id: uuid.UUID | str,
    ) -> OccupancyEventResponse | None:
        """Retrieve one occupancy domain event as the canonical DTO.

        Returns None when the event does not exist, is outside the
        actor's tenant/venue scope, or is not an occupancy_session event
        (this surface only represents the occupancy slice — a different
        event type is treated as not retrievable here).
        """
        record = await self._repository.get_event_for_actor(session, actor, event_id)
        if record is None:
            return None
        return _to_event_response(record)

    async def get_fact(
        self,
        *,
        session: Any,
        actor: ActorContext,
        fact_id: uuid.UUID | str,
    ) -> OccupancyFactResponse | None:
        """Retrieve one occupancy fact as the canonical DTO.

        Same deny-by-default semantics as ``get_event``.
        """
        record = await self._repository.get_fact_for_actor(session, actor, fact_id)
        if record is None:
            return None
        return _to_fact_response(record)

    async def get_evidence_availability(
        self,
        *,
        session: Any,
        actor: ActorContext,
        event_id: uuid.UUID | str,
    ) -> EvidenceAvailabilityResponse | None:
        """Whether evidence exists for one event, as the canonical DTO.

        Returns None when the EVENT is missing or out of scope (the route
        maps that to 404 — availability is never answered for an event
        the actor cannot see). An in-scope event with no evidence row
        answers ``available=False`` (a legitimate answer, not an error).
        """
        event = await self._repository.get_event_for_actor(session, actor, event_id)
        if event is None:
            return None
        ref = await self._repository.get_evidence_for_event(session, actor, event_id)
        return EvidenceAvailabilityResponse(
            event_id=EventId(event.event_id),
            available=ref is not None,
            evidence_ref_id=EventId(ref.ref_id) if ref is not None else None,
        )


# =============================================================================
# ORM → canonical DTO mapping (the ONLY place rows become wire shapes)
# =============================================================================


def _to_event_response(record: OperationalEventModel) -> OccupancyEventResponse:
    """Map the event row to the canonical occupancy-event DTO.

    The occupancy surface only represents ``occupancy_session`` events:
    a record of any other event type cannot be represented by the
    occupancy DTO and is treated as not-found (never a mismatched
    shape). The payload is re-validated as the canonical Task 16
    contract — a corrupted payload fails deterministically instead of
    leaking a different shape.
    """
    if record.event_type != RuleEventType.OCCUPANCY_SESSION.value:
        raise OperationalNotFoundError(
            f"Operational event {record.event_id} is not an occupancy_session event"
        )
    return OccupancyEventResponse(
        event_id=EventId(record.event_id),
        event_type=record.event_type,
        schema_version=record.schema_version,
        tenant_id=TenantId(record.tenant_id),
        venue_id=VenueId(record.venue_id),
        session_id=VideoSessionId(record.session_id) if record.session_id else None,
        camera_id=CameraId(record.camera_id) if record.camera_id else None,
        event_time=record.event_time,
        produced_at=record.produced_at,
        source=record.source,
        correlation_id=record.correlation_id,
        causation_id=record.causation_id,
        payload=OccupancySessionPayload.model_validate(record.payload),
    )


def _to_fact_response(record: TemporalFactModel) -> OccupancyFactResponse:
    """Map the fact row to the canonical occupancy-fact DTO.

    The occupancy surface only represents ``occupancy_snapshot`` facts;
    the payload is re-validated as the canonical Task 15 contract.
    """
    if record.fact_type != FACT_TYPE_OCCUPANCY_SNAPSHOT:
        raise OperationalNotFoundError(
            f"Temporal fact {record.fact_id} is not an occupancy_snapshot fact"
        )
    return OccupancyFactResponse(
        fact_id=EventId(record.fact_id),
        fact_type=record.fact_type,
        fsm_kind=record.fsm_kind,
        schema_version=record.schema_version,
        tenant_id=TenantId(record.tenant_id),
        venue_id=VenueId(record.venue_id),
        session_id=VideoSessionId(record.session_id) if record.session_id else None,
        camera_id=CameraId(record.camera_id) if record.camera_id else None,
        configuration_version_id=(
            ConfigurationVersionId(record.configuration_version_id)
            if record.configuration_version_id
            else None
        ),
        event_time=record.event_time,
        source_transition_id=(
            EventId(record.source_transition_id) if record.source_transition_id else None
        ),
        fsm_version=record.fsm_version,
        policy_revision=record.policy_revision,
        payload=OccupancySnapshot.model_validate(record.payload),
    )
