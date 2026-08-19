"""Transactional inbox repository (Task 7 Phase 10).

Implements idempotent inbound processing on the Task 6.12 schema
(migration 014) + Task 7 extensions (migration 016):

  receive      — INSERT a message with deduplication: the unique
                 (source, source_message_id) constraint rejects
                 duplicate delivery (INSERT ... ON CONFLICT DO NOTHING),
                 so Consumer A + Event X and Consumer B + Event X are
                 distinct rows (source differs) while the SAME consumer
                 receiving Event X twice is one row. Returns None on a
                 duplicate — the caller must NOT run the effect again.
  claim_next   — atomic SKIP LOCKED claim (same lease semantics as the
                 outbox) for the processing poller.
  mark_processed / mark_failed / mark_dead_letter — the result
                 transitions. processed is terminal; failed rows are
                 re-queued after their persisted backoff; dead_letter is
                 terminal and never deleted.

Transaction ownership: the effect handler runs INSIDE the same
transaction as mark_processed — an inbox row is NEVER marked processed
before the business effect is safely committed (a rollback removes
both). On effect failure the consumer rolls back the effect via a
savepoint, then marks the row failed with a persisted retry time.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.audit_outbox_inbox import InboxMessageModel

# Row states the poller can claim (see OutboxRepository.claim_next).
_CLAIMABLE_STATUSES = ("pending", "failed", "processing")

_MAX_ERROR_LENGTH = 2000


def _truncate_error(error: str) -> str:
    return error[:_MAX_ERROR_LENGTH]


class InboxRepository:
    """Data access for the idempotent inbound inbox."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # =========================================================================
    # Receive (deduplicated ingress)
    # =========================================================================

    async def receive(
        self,
        *,
        source: str,
        source_message_id: str,
        tenant_id: uuid.UUID,
        event_type: str | None,
        payload: dict[str, Any],
        venue_id: uuid.UUID | None = None,
    ) -> InboxMessageModel | None:
        """Insert a message, deduplicating on (source, source_message_id).

        Returns:
            The new message row, or None if the message was already
            received (duplicate delivery — the caller must skip the
            business effect).
        """
        stmt = (
            insert(InboxMessageModel)
            .values(
                tenant_id=tenant_id,
                venue_id=venue_id,
                source=source,
                source_message_id=source_message_id,
                event_type=event_type,
                payload=payload,
            )
            .on_conflict_do_nothing(
                index_elements=["source", "source_message_id"],
            )
            .returning(InboxMessageModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # =========================================================================
    # Claiming / leasing
    # =========================================================================

    async def claim_next(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
        now: datetime,
    ) -> list[InboxMessageModel]:
        """Atomically claim up to batch_size due messages (SKIP LOCKED).

        Returns:
            The claimed rows (status='processing', attempts incremented,
            lease stamped).
        """
        claimed_until = now + timedelta(seconds=lease_seconds)
        subq = (
            select(InboxMessageModel.inbox_id)
            .where(
                InboxMessageModel.status.in_(_CLAIMABLE_STATUSES),
                InboxMessageModel.available_at <= now,
                or_(
                    InboxMessageModel.claimed_until.is_(None),
                    InboxMessageModel.claimed_until <= now,
                ),
            )
            .order_by(InboxMessageModel.available_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(InboxMessageModel)
            .where(InboxMessageModel.inbox_id.in_(subq))
            .values(
                status="processing",
                claimed_by=worker_id,
                claimed_until=claimed_until,
                attempts=InboxMessageModel.attempts + 1,
            )
            .returning(InboxMessageModel)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # =========================================================================
    # Result transitions
    # =========================================================================

    async def mark_processed(self, inbox_id: uuid.UUID, worker_id: str) -> bool:
        """processing -> processed (terminal), guarded by the claim.

        Must run in the SAME transaction as the business effect: the
        inbox row is only marked processed once the effect is committed.
        """
        stmt = (
            update(InboxMessageModel)
            .where(
                InboxMessageModel.inbox_id == inbox_id,
                InboxMessageModel.status == "processing",
                InboxMessageModel.claimed_by == worker_id,
            )
            .values(status="processed", claimed_by=None, claimed_until=None)
            .returning(InboxMessageModel.inbox_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_failed(
        self,
        inbox_id: uuid.UUID,
        worker_id: str,
        *,
        error: str,
        retry_at: datetime,
    ) -> bool:
        """processing -> failed with last_error and the persisted backoff."""
        stmt = (
            update(InboxMessageModel)
            .where(
                InboxMessageModel.inbox_id == inbox_id,
                InboxMessageModel.status == "processing",
                InboxMessageModel.claimed_by == worker_id,
            )
            .values(
                status="failed",
                claimed_by=None,
                claimed_until=None,
                available_at=retry_at,
                last_error=_truncate_error(error),
            )
            .returning(InboxMessageModel.inbox_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_dead_letter(
        self,
        inbox_id: uuid.UUID,
        worker_id: str,
        *,
        error: str,
    ) -> bool:
        """processing|failed -> dead_letter (terminal, row is preserved)."""
        stmt = (
            update(InboxMessageModel)
            .where(
                InboxMessageModel.inbox_id == inbox_id,
                InboxMessageModel.status == "processing",
                InboxMessageModel.claimed_by == worker_id,
            )
            .values(
                status="dead_letter",
                claimed_by=None,
                claimed_until=None,
                last_error=_truncate_error(error),
            )
            .returning(InboxMessageModel.inbox_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # =========================================================================
    # Observability / tests
    # =========================================================================

    async def get(self, inbox_id: uuid.UUID) -> InboxMessageModel | None:
        stmt = select(InboxMessageModel).where(InboxMessageModel.inbox_id == inbox_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_by_status(self, status: str) -> int:
        stmt = select(InboxMessageModel.inbox_id).where(InboxMessageModel.status == status)
        result = await self._session.execute(stmt)
        return len(result.scalars().all())
