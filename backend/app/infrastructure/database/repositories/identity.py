"""Tenant and venue repositories with scope enforcement.

Every query includes tenant_id and/or venue scope filters so that
unsafe data access is difficult by default. The ActorContext
determines authorization — client-supplied IDs are resource
selectors only.

BAD:  repository.get(resource_id)          → then check tenant afterward
GOOD: repository.get_for_actor(actor, id)  → WHERE id=:id AND tenant_id=:actor_tenant

Repository methods:
  - get_for_actor    — single resource, tenant+venue scoped
  - list_for_actor   — list resources, tenant+venue scoped
  - update_for_actor — update resource, tenant+venue scoped
  - delete_for_actor — delete resource, tenant+venue scoped
  - count_for_actor  — count resources, tenant+venue scoped

All scoped operations return None / empty list / False when the actor
has no access, rather than raising an error. This prevents information
leakage about whether a foreign resource exists.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.identity import (
    TenantModel,
    VenueModel,
)
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext


def _uuid(value: TenantId | VenueId | uuid.UUID | str) -> uuid.UUID:
    """Normalize a contract ID to a raw UUID for SQLAlchemy comparison."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _has_venue_access(actor: ActorContext, venue_id: uuid.UUID) -> bool:
    """Check actor's venue scope for a given venue.

    Empty venue_scope means ALL_VENUES (tenant-wide access).
    Non-empty venue_scope means SPECIFIC_VENUES — check membership.
    """
    if not actor.venue_scope:
        return True  # ALL_VENUES — no venue filter needed
    return _uuid(venue_id) in {_uuid(v) for v in actor.venue_scope}


# =============================================================================
# Tenant Repository
# =============================================================================


class TenantRepository:
    """Tenant-scoped repository for the tenants table.

    A tenant actor can only access their own tenant row.
    Cross-tenant access is prevented at the query level.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_actor(
        self,
        actor: ActorContext,
        tenant_id: TenantId,
    ) -> TenantModel | None:
        """Get the actor's own tenant.

        The tenant_id in the WHERE clause must match the actor's
        tenant_id. Cross-tenant lookups return None without leaking
        whether the foreign tenant exists.
        """
        stmt = select(TenantModel).where(
            TenantModel.tenant_id == _uuid(tenant_id),
            TenantModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_for_actor(
        self,
        actor: ActorContext,
        tenant_id: TenantId,
        **values: Any,
    ) -> TenantModel | None:
        """Update the actor's own tenant fields.

        Only the matching tenant row is updated. Returns the updated
        model, or None if the actor has no access.
        """
        stmt = (
            update(TenantModel)
            .where(
                TenantModel.tenant_id == _uuid(tenant_id),
                TenantModel.tenant_id == _uuid(actor.tenant_id),
            )
            .values(**values)
            .returning(TenantModel)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()


# =============================================================================
# Venue Repository
# =============================================================================


class VenueRepository:
    """Venue repository with tenant and venue scope enforcement.

    All queries include tenant_id = actor.tenant_id.
    For SPECIFIC_VENUES memberships, venue access is additionally
    enforced against the actor's venue_scope.
    ALL_VENUES memberships (empty venue_scope) grant access to all
    venues within the tenant.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_actor(
        self,
        actor: ActorContext,
        venue_id: VenueId,
    ) -> VenueModel | None:
        """Get a venue, scoped by actor's tenant and venue access.

        Cross-tenant venue lookups return None. Venue-scoped actors
        can only access venues in their venue_scope.
        """
        stmt = select(VenueModel).where(
            VenueModel.venue_id == _uuid(venue_id),
            VenueModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await self._session.execute(stmt)
        venue = result.scalar_one_or_none()
        if venue is None:
            return None

        # Enforce venue scope for SPECIFIC_VENUES memberships
        if not _has_venue_access(actor, venue.venue_id):
            return None

        return venue

    async def list_for_actor(
        self,
        actor: ActorContext,
    ) -> list[VenueModel]:
        """List venues, scoped by actor's tenant and venue access.

        ALL_VENUES scope returns all venues in the tenant.
        SPECIFIC_VENUES scope filters to the actor's accessible venues.
        """
        stmt = select(VenueModel).where(
            VenueModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await self._session.execute(stmt)
        venues = list(result.scalars().all())

        if actor.venue_scope:
            scope_ids = {_uuid(v) for v in actor.venue_scope}
            venues = [v for v in venues if _uuid(v.venue_id) in scope_ids]

        return venues

    async def update_for_actor(
        self,
        actor: ActorContext,
        venue_id: VenueId,
        **values: Any,
    ) -> VenueModel | None:
        """Update a venue, scoped by actor's tenant and venue access.

        Returns the updated VenueModel, or None if the actor has no
        access to this venue.
        """
        venue = await self.get_for_actor(actor, venue_id)
        if venue is None:
            return None

        for key, val in values.items():
            setattr(venue, key, val)
        await self._session.flush()
        return venue

    async def delete_for_actor(
        self,
        actor: ActorContext,
        venue_id: VenueId,
    ) -> bool:
        """Delete a venue, scoped by actor's tenant and venue access.

        Returns True if the venue was deleted, False if the actor had
        no access or the venue doesn't exist.
        """
        venue = await self.get_for_actor(actor, venue_id)
        if venue is None:
            return False

        await self._session.delete(venue)
        await self._session.flush()
        return True

    async def count_for_actor(
        self,
        actor: ActorContext,
    ) -> int:
        """Count venues accessible to the actor.

        ALL_VENUES scope counts all venues in the tenant.
        SPECIFIC_VENUES scope counts only accessible venues.
        """
        stmt = select(func.count(VenueModel.venue_id)).where(
            VenueModel.tenant_id == _uuid(actor.tenant_id),
        )

        if actor.venue_scope:
            scope_ids = [_uuid(v) for v in actor.venue_scope]
            stmt = stmt.where(VenueModel.venue_id.in_(scope_ids))

        result = await self._session.execute(stmt)
        return result.scalar_one()
