"""Idempotency record repository (Task 7 Phase 11).

Tenant-scoped idempotency storage. The unique constraint
(tenant_id, operation, idempotency_key) is the idempotency unit — a
tenant's key can never collide with another tenant's key, so Tenant A
can never observe or replay Tenant B's idempotency records.

Concurrency safety: simultaneous identical requests race on
INSERT ... ON CONFLICT DO NOTHING — exactly one request wins the claim
(the unique key blocks the others until the winner's transaction
commits or rolls back), and the winner executes the operation while the
losers replay the stored result. The lease columns (claimed_by/
claimed_until) provide crash recovery for the claim: a claim that never
completed becomes reclaimable after its lease expires.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.idempotency import IdempotencyRecordModel


class IdempotencyRepository:
    """Data access for idempotency_records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # =========================================================================
    # Lookup (always tenant-scoped by the ActorContext tenant)
    # =========================================================================

    async def get(
        self,
        *,
        tenant_id: uuid.UUID,
        operation: str,
        key: str,
    ) -> IdempotencyRecordModel | None:
        """Look up an idempotency record within a tenant.

        The tenant_id in the WHERE clause comes from the server-side
        ActorContext — a cross-tenant key can never be found.
        """
        stmt = select(IdempotencyRecordModel).where(
            IdempotencyRecordModel.tenant_id == tenant_id,
            IdempotencyRecordModel.operation == operation,
            IdempotencyRecordModel.idempotency_key == key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # =========================================================================
    # Claiming
    # =========================================================================

    async def create_claim(
        self,
        *,
        tenant_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
        actor_id: uuid.UUID | None,
        venue_id: uuid.UUID | None,
        claimed_by: str,
        lease_seconds: int,
        now: datetime,
    ) -> IdempotencyRecordModel | None:
        """Attempt to claim the idempotency unit (INSERT ON CONFLICT).

        Returns:
            The claimed record when this request WON the claim, or None
            when a concurrent request already holds the unit (the caller
            must then replay/compare against the existing record).
        """
        claimed_until = now + timedelta(seconds=lease_seconds)
        stmt = (
            insert(IdempotencyRecordModel)
            .values(
                tenant_id=tenant_id,
                actor_id=actor_id,
                venue_id=venue_id,
                operation=operation,
                idempotency_key=key,
                request_hash=request_hash,
                status="in_progress",
                claimed_by=claimed_by,
                claimed_until=claimed_until,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "operation", "idempotency_key"],
            )
            .returning(IdempotencyRecordModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def reclaim(
        self,
        *,
        idempotency_id: uuid.UUID,
        request_hash: str,
        actor_id: uuid.UUID | None,
        venue_id: uuid.UUID | None,
        claimed_by: str,
        lease_seconds: int,
        now: datetime,
    ) -> bool:
        """Re-claim a stale in_progress record whose lease has expired.

        Only transitions in_progress records with claimed_until < now —
        a live claim is never stolen. The request hash/context is
        refreshed so the new execution's payload is the one recorded.

        Returns:
            True if the record was reclaimed, False otherwise.
        """
        claimed_until = now + timedelta(seconds=lease_seconds)
        stmt = (
            update(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.idempotency_id == idempotency_id,
                IdempotencyRecordModel.status == "in_progress",
                IdempotencyRecordModel.claimed_until < now,
            )
            .values(
                request_hash=request_hash,
                actor_id=actor_id,
                venue_id=venue_id,
                claimed_by=claimed_by,
                claimed_until=claimed_until,
                updated_at=now,
            )
            .returning(IdempotencyRecordModel.idempotency_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # =========================================================================
    # Completion
    # =========================================================================

    async def complete(
        self,
        *,
        idempotency_id: uuid.UUID,
        claimed_by: str,
        result: dict[str, Any],
        now: datetime,
    ) -> bool:
        """in_progress -> completed, storing the logical result.

        Guarded by the claim: only the claim owner can complete, so a
        stolen/stale claim cannot overwrite a completed result.

        Returns:
            True if this call performed the completion, False if the
            record was already completed.
        """
        stmt = (
            update(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.idempotency_id == idempotency_id,
                IdempotencyRecordModel.status == "in_progress",
                IdempotencyRecordModel.claimed_by == claimed_by,
            )
            .values(
                status="completed",
                result=result,
                claimed_by=None,
                claimed_until=None,
                completed_at=now,
                updated_at=now,
            )
            .returning(IdempotencyRecordModel.idempotency_id)
        )
        result_count = await self._session.execute(stmt)
        return result_count.scalar_one_or_none() is not None
