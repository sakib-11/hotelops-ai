"""FastAPI routes for the operational vertical slice (Task 18.12).

The minimum retrieval surface for the slice's canonical fact/event:

    GET /operational/events/{event_id}  → OccupancyEventResponse
    GET /operational/facts/{fact_id}    → OccupancyFactResponse

Authorization is enforced entirely server-side, in layers:

    authenticated actor   — get_actor_context (JWT → server-resolved
                            ActorContext; 401 on missing/invalid/expired);
    permission           — require_permission(ANALYTICS_READ) (403);
    tenant context       — the ActorContext's server-side tenant is the
                            ONLY tenant that may be read — the route has
                            no client tenant parameter, so a client can
                            never select another tenant (no tenant
                            bypass);
    venue authorization  — the repository checks the record's venue
                            against the actor's venue scope;
    repository filtering — tenant_id is filtered in SQL by the
                            repository; out-of-scope == nonexistent
                            (both 404 — no enumeration);
    PostgreSQL RLS       — the request session is scoped to the actor's
                            tenant (SET LOCAL app.tenant_id) so the
                            FORCE row-level-security policies of
                            migrations 008/019 enforce the same boundary
                            at the database level.

The response is ALWAYS a canonical response DTO (contracts/operational)
— internal ORM models are never exposed. Telemetry: structured log
entries carry bounded identifiers (event/fact id, tenant, venue,
correlation id) — never payload bodies (Task 8.7).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.operational_errors import OperationalNotFoundError
from backend.app.application.services.operational_read import OperationalReadService
from backend.app.dependencies import get_db_session
from backend.app.infrastructure.auth.deps import get_actor_context, require_permission
from backend.app.infrastructure.database.rls import set_rls_on_session
from backend.app.infrastructure.observability.context import correlation_id
from contracts.common import EventId
from contracts.identity import ActorContext, Permission
from contracts.operational import (
    EvidenceAvailabilityResponse,
    OccupancyEventResponse,
    OccupancyFactResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/operational", tags=["Operational"])


@router.get(
    "/events/{event_id}",
    response_model=OccupancyEventResponse,
    summary="Retrieve one occupancy_session domain event",
)
async def get_operational_event(
    event_id: EventId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.ANALYTICS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> OccupancyEventResponse:
    """Retrieve the vertical-slice occupancy domain event (Task 16).

    The tenant is the actor's server-side tenant — the path carries only
    the event_id resource selector, never a tenant/venue. A missing or
    out-of-scope event returns 404 (never a partial or foreign row).
    """
    # PostgreSQL RLS: scope the request transaction to the actor's tenant
    # (defense in depth — migrations 008/019 enforce FORCE RLS).
    await set_rls_on_session(session, actor.tenant_id)

    record = await OperationalReadService().get_event(
        session=session, actor=actor, event_id=event_id
    )
    if record is None:
        raise OperationalNotFoundError(f"Operational event {event_id} not found")
    logger.info(
        "operational event retrieved",
        extra={
            "event_id": str(record.event_id),
            "tenant_id": str(record.tenant_id),
            "venue_id": str(record.venue_id),
            "correlation_id": correlation_id(),
        },
    )
    return record


@router.get(
    "/facts/{fact_id}",
    response_model=OccupancyFactResponse,
    summary="Retrieve one occupancy_snapshot canonical fact",
)
async def get_operational_fact(
    fact_id: EventId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.ANALYTICS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> OccupancyFactResponse:
    """Retrieve the vertical-slice canonical business fact (Task 15).

    Same server-side authorization boundary as the event endpoint.
    """
    # PostgreSQL RLS: scope the request transaction to the actor's tenant.
    await set_rls_on_session(session, actor.tenant_id)

    record = await OperationalReadService().get_fact(session=session, actor=actor, fact_id=fact_id)
    if record is None:
        raise OperationalNotFoundError(f"Operational fact {fact_id} not found")
    logger.info(
        "operational fact retrieved",
        extra={
            "fact_id": str(record.fact_id),
            "tenant_id": str(record.tenant_id),
            "venue_id": str(record.venue_id),
            "correlation_id": correlation_id(),
        },
    )
    return record


@router.get(
    "/events/{event_id}/evidence",
    response_model=EvidenceAvailabilityResponse,
    summary="Evidence availability for one occupancy_session event",
)
async def get_operational_event_evidence(
    event_id: EventId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.ANALYTICS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> EvidenceAvailabilityResponse:
    """Whether evidence exists for the vertical-slice event (Task 18.13).

    Same server-side authorization boundary as the event/fact endpoints.
    Evidence availability is a server-derived fact (the Task 18.9
    event → evidence request linkage) — the desktop reads it here and
    never derives it locally.
    """
    # PostgreSQL RLS: scope the request transaction to the actor's tenant.
    await set_rls_on_session(session, actor.tenant_id)

    record = await OperationalReadService().get_evidence_availability(
        session=session, actor=actor, event_id=event_id
    )
    if record is None:
        raise OperationalNotFoundError(f"Operational event {event_id} not found")
    logger.info(
        "operational event evidence availability",
        extra={
            "event_id": str(record.event_id),
            "tenant_id": str(actor.tenant_id),
            "available": record.available,
            "correlation_id": correlation_id(),
        },
    )
    return record
