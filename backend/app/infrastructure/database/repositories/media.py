"""Media repository with multi-tenant and venue scope enforcement (Task 9.7).

Provides tenant-scoped and venue-scoped access to MediaAssetModel records.
All database access is constrained by the trusted server-side ActorContext.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.auth.scope import require_tenant_venue_access
from backend.app.infrastructure.database.models.media import MediaAssetModel
from contracts.common import MediaId, TenantId, VenueId
from contracts.identity import ActorContext

# Lifecycle states a cleanup worker treats as "content may exist".
RECONCILABLE_STATES = ("uploaded", "validating", "available", "expired", "deletion_pending")


def _uuid(value: TenantId | VenueId | MediaId | uuid.UUID | str) -> uuid.UUID:
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


class MediaRepository:
    """Tenant-scoped persistence operations for media metadata."""

    async def create_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media: MediaAssetModel,
    ) -> MediaAssetModel:
        """Create a new media record scoped to the actor's tenant and venue.

        Raises:
            AuthorizationError: If actor does not have access to the target tenant or venue.
        """
        require_tenant_venue_access(
            actor=actor,
            resource_tenant_id=TenantId(media.tenant_id),
            venue_id=VenueId(media.venue_id),
        )
        session.add(media)
        await session.flush()
        return media

    async def get_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
    ) -> MediaAssetModel | None:
        """Retrieve a media record by ID within the actor's tenant and venue scope."""
        uid = _uuid(media_id)
        stmt = select(MediaAssetModel).where(
            MediaAssetModel.media_id == uid,
            MediaAssetModel.tenant_id == actor.tenant_id,
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_by_object_key_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        object_key: str,
    ) -> MediaAssetModel | None:
        """Retrieve a media record by object key within the actor's tenant scope."""
        stmt = select(MediaAssetModel).where(
            MediaAssetModel.object_key == object_key,
            MediaAssetModel.tenant_id == actor.tenant_id,
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def update_state_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        from_state: str,
        to_state: str,
        *,
        extra_updates: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically transition a media record's lifecycle state if it matches from_state.

        Returns True if a row was updated; False otherwise.
        """
        uid = _uuid(media_id)
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "lifecycle_state": to_state,
            "updated_at": now,
        }
        if extra_updates:
            values.update(extra_updates)

        stmt = (
            update(MediaAssetModel)
            .where(
                MediaAssetModel.media_id == uid,
                MediaAssetModel.tenant_id == actor.tenant_id,
                MediaAssetModel.lifecycle_state == from_state,
            )
            .values(**values)
            # RETURNING avoids relying on rowcount and matches the
            # identity repository pattern (returns the updated row id).
            .returning(MediaAssetModel.media_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # =========================================================================
    # Worker / system-scoped queries (trusted background processes only —
    # these deliberately bypass actor scope; RLS still applies at the
    # database layer when a tenant context is set, which workers never set).
    # =========================================================================

    async def get_by_id(
        self,
        session: AsyncSession,
        media_id: uuid.UUID | MediaId | str,
    ) -> MediaAssetModel | None:
        """Unscoped fetch by media_id (cleanup workers)."""
        stmt = select(MediaAssetModel).where(MediaAssetModel.media_id == _uuid(media_id))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_object_key_unscoped(
        self,
        session: AsyncSession,
        object_key: str,
    ) -> MediaAssetModel | None:
        """Unscoped fetch by object_key (orphan reconciliation)."""
        stmt = select(MediaAssetModel).where(MediaAssetModel.object_key == object_key)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_expired(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        limit: int,
    ) -> list[MediaAssetModel]:
        """Records whose retention window has elapsed (worker sweep).

        Includes DELETION_PENDING records so a failed object delete is
        retried on the next cycle (a record never gets stuck).
        """
        stmt = (
            select(MediaAssetModel)
            .where(
                MediaAssetModel.lifecycle_state.in_(("available", "deletion_pending")),
                MediaAssetModel.expires_at.is_not(None),
                MediaAssetModel.expires_at <= now,
            )
            .order_by(MediaAssetModel.expires_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_stale_uploads(
        self,
        session: AsyncSession,
        *,
        older_than: datetime,
        limit: int,
    ) -> list[MediaAssetModel]:
        """UPLOADING records abandoned before the timeout (worker sweep)."""
        stmt = (
            select(MediaAssetModel)
            .where(
                MediaAssetModel.lifecycle_state == "uploading",
                MediaAssetModel.created_at < older_than,
            )
            .order_by(MediaAssetModel.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_reconciliation(
        self,
        session: AsyncSession,
        *,
        states: tuple[str, ...] = RECONCILABLE_STATES,
        older_than: datetime,
        limit: int,
    ) -> list[MediaAssetModel]:
        """Records whose object presence should be re-verified (grace applied)."""
        stmt = (
            select(MediaAssetModel)
            .where(
                MediaAssetModel.lifecycle_state.in_(states),
                MediaAssetModel.updated_at < older_than,
            )
            .order_by(MediaAssetModel.updated_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_tenant_venue_pairs(
        self,
        session: AsyncSession,
        *,
        limit: int = 1000,
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Distinct (tenant_id, venue_id) pairs — scoping for orphan scans."""
        stmt = select(MediaAssetModel.tenant_id, MediaAssetModel.venue_id).distinct().limit(limit)
        result = await session.execute(stmt)
        return [(row.tenant_id, row.venue_id) for row in result.all()]

    async def update_state_unscoped(
        self,
        session: AsyncSession,
        media_id: uuid.UUID | MediaId | str,
        from_state: str,
        to_state: str,
        *,
        extra_updates: dict[str, Any] | None = None,
    ) -> bool:
        """Atomic lifecycle transition without actor scoping (cleanup workers).

        Returns True only when a row in ``from_state`` was transitioned.
        """
        uid = _uuid(media_id)
        values: dict[str, Any] = {
            "lifecycle_state": to_state,
            "updated_at": datetime.now(UTC),
        }
        if extra_updates:
            values.update(extra_updates)

        stmt = (
            update(MediaAssetModel)
            .where(
                MediaAssetModel.media_id == uid,
                MediaAssetModel.lifecycle_state == from_state,
            )
            .values(**values)
            .returning(MediaAssetModel.media_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None
