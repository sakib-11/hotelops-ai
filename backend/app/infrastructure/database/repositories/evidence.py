"""Evidence repository with multi-tenant and venue scope enforcement (Task 17.8).

Every evidence operation is constrained by the TRUSTED server-side
``ActorContext`` and the canonical ``EvidenceAuthorizer`` policy:

- All reads filter by ``tenant_id == actor.tenant_id`` in SQL — a
  cross-tenant ID simply returns ``None`` (never an existence leak).
- Venue access is checked on the resolved row (empty venue_scope =
  tenant-wide access).
- Creation/state-change operations authorize through
  ``EvidenceAuthorizer`` before touching the database.
- The storage key is NEVER an authorization input: object-key lookups
  are tenant-filtered SQL (``object_key AND tenant_id == actor.tenant_id``),
  and the authorizer is applied to the resolved row's scope.

Client-supplied tenant_id / venue_id from request bodies or query
parameters never reach these queries — scope always comes from the
server-side actor.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.auth.evidence import (
    EvidenceAuthorizer,
    EvidenceOperation,
)
from backend.app.infrastructure.auth.scope import require_tenant_venue_access
from backend.app.infrastructure.database.models.evidence import (
    EvidencePackageModel,
    EvidenceRefModel,
)
from contracts.common import EvidenceId, TenantId, VenueId
from contracts.identity import ActorContext


def _uuid(value: Any) -> uuid.UUID:
    """Normalize identifier to raw UUID for SQLAlchemy comparisons."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _has_venue_access(actor: ActorContext, venue_id: uuid.UUID) -> bool:
    """Check actor's venue scope for a given venue.

    Empty venue_scope means ALL_VENUES (tenant-wide access).
    """
    if not actor.venue_scope:
        return True
    return _uuid(venue_id) in {_uuid(v) for v in actor.venue_scope}


class EvidenceRepository:
    """Actor-scoped persistence for evidence refs and packages."""

    def __init__(self, authorizer: EvidenceAuthorizer | None = None) -> None:
        self._authorizer = authorizer or EvidenceAuthorizer()

    # =========================================================================
    # Evidence creation (CREATE)
    # =========================================================================

    async def create_ref_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        ref: EvidenceRefModel,
    ) -> EvidenceRefModel:
        """Create an evidence ref scoped to the actor's tenant and venue.

        The ref's OWNER scope (its tenant/venue columns) must be within
        the actor's scope — a client-supplied tenant_id in the payload
        is never trusted; the server-side actor is the boundary.
        """
        require_tenant_venue_access(
            actor=actor,
            resource_tenant_id=TenantId(ref.tenant_id),
            venue_id=VenueId(ref.venue_id),
        )
        session.add(ref)
        await session.flush()
        return ref

    async def create_package_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        package: EvidencePackageModel,
    ) -> EvidencePackageModel:
        """Create an evidence package within the actor's tenant/venue scope."""
        require_tenant_venue_access(
            actor=actor,
            resource_tenant_id=TenantId(package.tenant_id),
            venue_id=VenueId(package.venue_id),
        )
        session.add(package)
        await session.flush()
        return package

    # =========================================================================
    # Evidence retrieval (RETRIEVE / METADATA / SIGNED_URL)
    # =========================================================================

    async def get_ref_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        ref_id: EvidenceId | uuid.UUID | str,
    ) -> EvidenceRefModel | None:
        """Retrieve an evidence ref within the actor's tenant scope.

        The SQL filter is ``tenant_id == actor.tenant_id`` — a cross-
        tenant ref_id returns ``None`` (no existence leak). Venue access
        is checked on the resolved row.
        """
        stmt = select(EvidenceRefModel).where(
            EvidenceRefModel.ref_id == _uuid(ref_id),
            EvidenceRefModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_package_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        package_id: uuid.UUID | str,
    ) -> EvidencePackageModel | None:
        """Retrieve an evidence package within the actor's tenant scope."""
        stmt = select(EvidencePackageModel).where(
            EvidencePackageModel.package_id == _uuid(package_id),
            EvidencePackageModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_ref_by_object_key_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        object_key: str,
    ) -> EvidenceRefModel | None:
        """Resolve an object key to an evidence ref within actor scope.

        The storage key alone NEVER authorizes: the lookup is filtered
        by the actor's tenant, and the authorizer is applied to the
        resolved row's scope by the caller (``authorize_object_key_scope``).
        """
        stmt = select(EvidenceRefModel).where(
            EvidenceRefModel.ref_uri == object_key,
            EvidenceRefModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    # =========================================================================
    # Destructive / management operations (DELETE / RETENTION)
    # =========================================================================

    async def delete_ref_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        ref_id: EvidenceId | uuid.UUID | str,
    ) -> bool:
        """Delete an evidence ref within actor scope (EVIDENCE_MANAGE).

        The authorizer is applied to the RESOLVED row (never client
        scope); the SQL delete is additionally tenant-filtered so a
        cross-tenant ref_id deletes nothing.
        """
        record = await self.get_ref_for_actor(session, actor, ref_id)
        if record is None:
            return False
        self._authorizer.authorize(
            actor,
            EvidenceOperation.DELETE,
            TenantId(record.tenant_id),
            VenueId(record.venue_id),
        )
        await session.delete(record)
        await session.flush()
        return True

    async def update_ref_retention_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        ref_id: EvidenceId | uuid.UUID | str,
        *,
        retention_class: str,
    ) -> bool:
        """Set an evidence ref's retention class (EVIDENCE_MANAGE).

        Authorizes against the resolved row, then applies the tenant-
        filtered update. Returns True when a row was updated.
        """
        record = await self.get_ref_for_actor(session, actor, ref_id)
        if record is None:
            return False
        self._authorizer.authorize(
            actor,
            EvidenceOperation.RETENTION,
            TenantId(record.tenant_id),
            VenueId(record.venue_id),
        )
        stmt = (
            update(EvidenceRefModel)
            .where(
                EvidenceRefModel.ref_id == _uuid(ref_id),
                EvidenceRefModel.tenant_id == _uuid(actor.tenant_id),
            )
            .values(metadata={**(record.metadata_ or {}), "retention_class": retention_class})
            .returning(EvidenceRefModel.ref_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def delete_package_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        package_id: uuid.UUID | str,
    ) -> bool:
        """Delete an evidence package within actor scope (EVIDENCE_MANAGE).

        Authorizes against the resolved row, then deletes. A cross-tenant
        package_id deletes nothing (tenant-filtered resolution).
        """
        record = await self.get_package_for_actor(session, actor, package_id)
        if record is None:
            return False
        self._authorizer.authorize(
            actor,
            EvidenceOperation.DELETE,
            TenantId(record.tenant_id),
            VenueId(record.venue_id),
        )
        await session.delete(record)
        await session.flush()
        return True
