"""Operational vertical-slice read repository (Task 18.12).

Tenant- and venue-scoped retrieval of the authoritative vertical-slice
rows — the Task 16 domain event (``operational_events``) and the Task 15
canonical fact (``temporal_facts``). Every query is constrained by the
trusted server-side ``ActorContext``:

    tenant  — filtered in SQL (``tenant_id = actor.tenant_id``): a
              resource of another tenant can never be returned, even if
              the actor knows its UUID (cross-tenant IDOR is denied);
    venue   — checked after the fetch: an actor with SPECIFIC_VENUES
              scope cannot read a record of a venue outside that scope
              (an empty scope = ALL_VENUES = tenant-wide access).

The repository NEVER accepts a client-controlled tenant/venue: the only
resource selector is the resource id; authorization comes from the
actor. Out-of-scope resources are indistinguishable from nonexistent
ones (both return None → the API maps both to 404, never leaking which
tenant/venue a UUID belongs to).

This is the application-level layer of the defense-in-depth stack
(application authorization → repository scope → PostgreSQL RLS — the
tables ship with FORCE row-level security in migrations 008/019, and
the API route scopes the request session's RLS context to the actor's
tenant).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.database.models.temporal import TemporalFactModel
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext


def _uuid(value: TenantId | VenueId | uuid.UUID | str) -> uuid.UUID:
    """Normalize an identifier to a raw UUID for SQLAlchemy comparisons."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _has_venue_access(actor: ActorContext, venue_id: uuid.UUID) -> bool:
    """The actor's venue scope for a record's venue.

    An empty venue_scope means ALL_VENUES (tenant-wide access); a
    SPECIFIC_VENUES scope requires the venue to be listed.
    """
    if not actor.venue_scope:
        return True
    return _uuid(venue_id) in {_uuid(v) for v in actor.venue_scope}


class OperationalRepository:
    """Tenant- and venue-scoped reads over the vertical-slice rows."""

    async def get_event_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        event_id: uuid.UUID | str,
    ) -> OperationalEventModel | None:
        """Retrieve one operational event within the actor's tenant + venue scope.

        Returns None when the event does not exist OR is outside the
        actor's scope — the caller maps both to not-found (no
        enumeration, no cross-tenant/cross-venue leak).
        """
        stmt = select(OperationalEventModel).where(
            OperationalEventModel.event_id == _uuid(event_id),
            OperationalEventModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_fact_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        fact_id: uuid.UUID | str,
    ) -> TemporalFactModel | None:
        """Retrieve one canonical temporal fact within the actor's scope.

        Same deny-by-default semantics as ``get_event_for_actor``.
        """
        stmt = select(TemporalFactModel).where(
            TemporalFactModel.fact_id == _uuid(fact_id),
            TemporalFactModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_evidence_for_event(
        self,
        session: AsyncSession,
        actor: ActorContext,
        event_id: uuid.UUID | str,
    ) -> EvidenceRefModel | None:
        """The evidence request linked to one event, within the actor's scope.

        The linkage row (Task 18.9) inherits the event's tenant and venue
        — the same tenant filter + venue check as every other read, so
        availability is never answered for an event the actor cannot see.
        The event itself must be in scope for a meaningful answer; the
        service combines this with ``get_event_for_actor`` so an
        out-of-scope event is 404 (never an availability answer).
        """
        stmt = select(EvidenceRefModel).where(
            EvidenceRefModel.event_id == _uuid(event_id),
            EvidenceRefModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record
