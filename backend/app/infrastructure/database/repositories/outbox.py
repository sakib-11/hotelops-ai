"""Transactional outbox repository (Task 7 Phase 4/6/7/8/9).

Implements the outbox worker's data operations on top of the Task 6.12
schema (migration 014) + Task 7 extensions (migration 016):

  enqueue        — INSERT a validated event; the unique event_id
                   constraint makes duplicate enqueues idempotent
                   (DuplicateEventError).
  claim_next     — atomically claim due rows with FOR UPDATE SKIP LOCKED.
                   Eligible: status IN (pending, failed, processing) with
                   available_at <= now and no live lease. Attempts is
                   incremented per claim, so a crashed worker's row is
                   re-claimed after lease expiry (crash recovery) and the
                   retry budget advances on every real delivery attempt.
  mark_published — processing -> published (terminal). Guarded by
                   (status='processing' AND claimed_by=worker) so a
                   worker whose lease was lost can never override a
                   fresh claim.
  mark_failed    — processing -> failed with last_error and the next
                   available_at = retry_at (persisted backoff).
  mark_dead_letter — processing|failed -> dead_letter (terminal, never
                   deleted — payload/attempts/error stay inspectable).

All status transitions are additionally enforced by the DB trigger from
migration 014 (extended in 016). The publisher NEVER holds a database
transaction open while doing external work: claim commits, the external
publish happens, the result transition commits separately.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.audit_outbox_inbox import OutboxEventModel
from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_OUTBOX,
    record_pipeline_metric,
)
from backend.app.infrastructure.reliability.exceptions import DuplicateEventError

# Row states the poller can claim: pending rows, failed rows whose
# backoff has elapsed, and processing rows whose lease has expired.
_CLAIMABLE_STATUSES = ("pending", "failed", "processing")

# Error text is bounded before persistence (JSONB/Text hygiene).
_MAX_ERROR_LENGTH = 2000


def _truncate_error(error: str) -> str:
    return error[:_MAX_ERROR_LENGTH]


class OutboxRepository:
    """Data access for the transactional outbox."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # =========================================================================
    # Enqueue (transactional boundary — caller commits)
    # =========================================================================

    async def enqueue(
        self,
        *,
        event_id: uuid.UUID,
        tenant_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        venue_id: uuid.UUID | None = None,
        available_at: datetime | None = None,
    ) -> OutboxEventModel:
        """Insert a validated outbox event.

        The event commits atomically with the caller's business state and
        audit row (one DatabaseClient.session).

        Raises:
            DuplicateEventError: If an outbox row with the same event_id
                already exists (idempotent enqueue — the caller treats
                this as a no-op, never an error).
        """
        row = OutboxEventModel(
            event_id=event_id,
            tenant_id=tenant_id,
            venue_id=venue_id,
            event_type=event_type,
            payload=payload,
            available_at=available_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_outbox_events_event_id" in str(exc.orig):
                raise DuplicateEventError(
                    f"outbox event {event_id} already exists (idempotent enqueue)"
                ) from exc
            raise
        # Task 18.18 — one durable outbox row enqueued (idempotent
        # duplicates never reach this point).
        record_pipeline_metric(PIPELINE_METRIC_OUTBOX)
        return row

    # =========================================================================
    # Claiming / leasing (short transaction, SKIP LOCKED)
    # =========================================================================

    async def claim_next(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
        now: datetime,
    ) -> list[OutboxEventModel]:
        """Atomically claim up to batch_size due events.

        A single UPDATE ... WHERE outbox_id IN (SELECT ... FOR UPDATE
        SKIP LOCKED) claims each row exactly once even under concurrent
        workers: SKIP LOCKED skips rows another worker just locked, and
        the status/lease guards in the WHERE clause ensure only
        claimable rows are taken.

        Returns:
            The claimed rows (status='processing', attempts incremented,
            lease stamped).
        """
        claimed_until = now + timedelta(seconds=lease_seconds)
        subq = (
            select(OutboxEventModel.outbox_id)
            .where(
                OutboxEventModel.status.in_(_CLAIMABLE_STATUSES),
                OutboxEventModel.available_at <= now,
                or_(
                    OutboxEventModel.claimed_until.is_(None),
                    OutboxEventModel.claimed_until <= now,
                ),
            )
            .order_by(OutboxEventModel.available_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(OutboxEventModel)
            .where(OutboxEventModel.outbox_id.in_(subq))
            .values(
                status="processing",
                claimed_by=worker_id,
                claimed_until=claimed_until,
                attempts=OutboxEventModel.attempts + 1,
            )
            .returning(OutboxEventModel)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # =========================================================================
    # Result transitions (short transactions — never hold open for I/O)
    # =========================================================================

    async def mark_published(self, outbox_id: uuid.UUID, worker_id: str) -> bool:
        """processing -> published, guarded by the worker's own claim.

        Returns False if the row is no longer ours (lease was lost and
        another worker reclaimed it) — the caller must not override the
        new owner's state.
        """
        stmt = (
            update(OutboxEventModel)
            .where(
                OutboxEventModel.outbox_id == outbox_id,
                OutboxEventModel.status == "processing",
                OutboxEventModel.claimed_by == worker_id,
            )
            .values(status="published", claimed_by=None, claimed_until=None)
            .returning(OutboxEventModel.outbox_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_failed(
        self,
        outbox_id: uuid.UUID,
        worker_id: str,
        *,
        error: str,
        retry_at: datetime,
    ) -> bool:
        """processing -> failed with last_error and the persisted backoff.

        The poller picks the row up again once available_at (retry_at)
        elapses. Returns False if the claim was lost.
        """
        stmt = (
            update(OutboxEventModel)
            .where(
                OutboxEventModel.outbox_id == outbox_id,
                OutboxEventModel.status == "processing",
                OutboxEventModel.claimed_by == worker_id,
            )
            .values(
                status="failed",
                claimed_by=None,
                claimed_until=None,
                available_at=retry_at,
                last_error=_truncate_error(error),
            )
            .returning(OutboxEventModel.outbox_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_dead_letter(
        self,
        outbox_id: uuid.UUID,
        worker_id: str,
        *,
        error: str,
    ) -> bool:
        """processing|failed -> dead_letter (terminal, row is preserved).

        The row remains inspectable with its payload, tenant, venue,
        attempts, and last_error intact. Returns False if the claim was
        lost (only 'processing' rows owned by this worker can be moved by
        the worker; 'failed' rows are handled by the poller claim path).
        """
        stmt = (
            update(OutboxEventModel)
            .where(
                OutboxEventModel.outbox_id == outbox_id,
                OutboxEventModel.status == "processing",
                OutboxEventModel.claimed_by == worker_id,
            )
            .values(
                status="dead_letter",
                claimed_by=None,
                claimed_until=None,
                last_error=_truncate_error(error),
            )
            .returning(OutboxEventModel.outbox_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # =========================================================================
    # Observability / tests
    # =========================================================================

    async def get(self, outbox_id: uuid.UUID) -> OutboxEventModel | None:
        stmt = select(OutboxEventModel).where(OutboxEventModel.outbox_id == outbox_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_by_status(self, status: str) -> int:
        stmt = select(OutboxEventModel.outbox_id).where(OutboxEventModel.status == status)
        result = await self._session.execute(stmt)
        return len(result.scalars().all())
