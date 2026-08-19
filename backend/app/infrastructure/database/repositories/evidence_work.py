"""Evidence worker durable work store (Task 17.11).

The evidence worker's queue is the durable state machine (Task 17.10)
persisted on the evidence ref's JSONB metadata — there is NO new queue
architecture. This store provides the ATOMIC claim/lease + guarded
persistence operations the worker needs, mirroring the Task 7 outbox
repository semantics exactly:

- ``claim_queued``      — atomic QUEUED → EXTRACTING with a lease and a
                          claimed_by owner (FOR UPDATE SKIP LOCKED), so a
                          crashed worker's claim is reclaimed after the
                          lease expires (crash recovery) and the retry
                          budget advances on every real delivery attempt.
- ``lock_stale``        — re-claims a stale EXTRACTING/UPLOADING row under
                          a row lock (the recovery serialization point).
- ``persist_transition``— guarded EXTRACTING → UPLOADING /
                          → RETRYABLE_FAILURE / → TERMINAL_FAILURE,
                          guarded by (from_state AND claimed_by) so a
                          worker whose lease was lost can never override
                          the new owner's state.
- ``save_finalized``    — atomic UPLOADING → FINALIZED + package row +
                          ref link in ONE transaction (a duplicate
                          delivery can never produce a second package).

Metadata keys (JSONB policy — the durable processing state lives on the
ref's variable metadata, alongside the Task 17.10 processing_state).
Lease/retry timestamps are stored as ISO-8601 UTC strings so the SQL
``<=`` comparison is chronologically exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.evidence import (
    EvidencePackageModel,
    EvidenceRefModel,
    package_evidence_refs,
)

# Durable worker metadata keys (JSONB policy).
EVIDENCE_CLAIMED_BY_KEY = "processing_claimed_by"
EVIDENCE_LEASE_UNTIL_KEY = "processing_lease_until"
EVIDENCE_ATTEMPTS_KEY = "processing_attempts"
EVIDENCE_RETRY_AT_KEY = "processing_retry_at"
EVIDENCE_LAST_ERROR_KEY = "processing_last_error"
EVIDENCE_RECOVERY_KEY = "processing_recovery"
# The durable EvidenceRef request contract (the worker's input).
EVIDENCE_REQUEST_KEY = "evidence_request"
# The stored artifact object key + finalized package id (discoverability).
EVIDENCE_ARTIFACT_KEY = "artifact_object_key"
EVIDENCE_PACKAGE_ID_KEY = "package_id"

# Bound error text before persistence (JSONB hygiene — same as the outbox).
_MAX_ERROR_LENGTH = 2000


def iso_timestamp(value: datetime) -> str:
    """Normalize a timestamp to a chronologically-sortable UTC ISO string.

    Shared by the store (SQL comparisons) and the worker (writing
    retry_at / lease timestamps into the same format).
    """
    return value.astimezone(UTC).isoformat()


@runtime_checkable
class EvidenceWorkStore(Protocol):
    """Durable work store for the evidence worker (Task 17.11).

    The state machine (Task 17.10) is the FSM authority; this store
    persists its transitions atomically. Timestamps are passed by the
    caller (wall clock lives at the worker boundary, never in the store).
    """

    async def queue_pending(
        self, session: AsyncSession, *, now: datetime, batch_size: int
    ) -> list[EvidenceRefModel]:
        """Durable enqueue: REQUESTED (or never-started) → QUEUED."""

    async def promote_due_retries(
        self, session: AsyncSession, *, now: datetime, batch_size: int
    ) -> list[EvidenceRefModel]:
        """RETRYABLE_FAILURE with retry_at <= now → QUEUED."""

    async def claim_queued(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
        batch_size: int,
    ) -> list[EvidenceRefModel]:
        """Atomically claim QUEUED rows → EXTRACTING (lease + owner + attempts)."""

    async def expire_abandoned(
        self,
        session: AsyncSession,
        *,
        cutoff: datetime,
        batch_size: int,
        reason: str,
    ) -> list[EvidenceRefModel]:
        """Atomically expire REQUESTED/QUEUED rows abandoned past the cutoff."""

    async def list_stale(
        self, session: AsyncSession, *, now: datetime, limit: int
    ) -> list[EvidenceRefModel]:
        """Stale EXTRACTING/UPLOADING rows whose lease expired (no lock)."""

    async def lock_stale(
        self,
        session: AsyncSession,
        ref_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> EvidenceRefModel | None:
        """Re-claim a stale row under a row lock; None when no longer stale."""

    async def persist_transition(
        self,
        session: AsyncSession,
        ref: EvidenceRefModel,
        *,
        from_state: str,
        to_state: str,
        claimed_by: str,
        updates: dict[str, Any],
    ) -> bool:
        """Guarded state transition persist; False = the claim was lost."""

    async def save_finalized(
        self,
        session: AsyncSession,
        ref: EvidenceRefModel,
        *,
        claimed_by: str,
        package: EvidencePackageModel,
        link: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        """Atomic UPLOADING → FINALIZED + package + link; False = claim lost."""


class EvidenceWorkRepository:
    """SQLAlchemy implementation of ``EvidenceWorkStore``.

    Stateless: every method takes the caller's transaction-scoped session
    (the worker opens a short session per operation, exactly like the
    outbox publisher).
    """

    # =========================================================================
    # Enqueue + promotion
    # =========================================================================

    async def queue_pending(
        self, session: AsyncSession, *, now: datetime, batch_size: int
    ) -> list[EvidenceRefModel]:
        """Durable enqueue: REQUESTED (or never-started) → QUEUED."""
        rows = await self._select_for_update(
            session,
            predicate=or_(
                EvidenceRefModel.metadata_["processing_state"].astext.is_(None),
                EvidenceRefModel.metadata_["processing_state"].astext == "requested",
            ),
            limit=batch_size,
        )
        for row in rows:
            self._set_state(row, "queued")
        return rows

    async def promote_due_retries(
        self, session: AsyncSession, *, now: datetime, batch_size: int
    ) -> list[EvidenceRefModel]:
        """RETRYABLE_FAILURE with retry_at <= now → QUEUED."""
        rows = await self._select_for_update(
            session,
            predicate=(
                (EvidenceRefModel.metadata_["processing_state"].astext == "retryable_failure")
                & (
                    EvidenceRefModel.metadata_["processing_retry_at"].astext.is_(None)
                    | (
                        EvidenceRefModel.metadata_["processing_retry_at"].astext
                        <= iso_timestamp(now)
                    )
                )
            ),
            limit=batch_size,
        )
        for row in rows:
            self._set_state(row, "queued")
            metadata = dict(row.metadata_ or {})
            metadata.pop(EVIDENCE_RETRY_AT_KEY, None)
            row.metadata_ = metadata
        return rows

    async def expire_abandoned(
        self,
        session: AsyncSession,
        *,
        cutoff: datetime,
        batch_size: int,
        reason: str,
    ) -> list[EvidenceRefModel]:
        """Atomically expire REQUESTED/QUEUED rows abandoned past the cutoff.

        The EXPIRED transition is the state machine's terminal — the row
        is preserved for audit, never dropped. Guarded by the same
        SKIP LOCKED claim pattern as ``claim_queued`` so concurrent
        workers never double-expire.
        """
        rows = await self._select_for_update(
            session,
            predicate=(
                EvidenceRefModel.metadata_["processing_state"].astext.in_(("requested", "queued"))
                & (EvidenceRefModel.created_at <= cutoff)
            ),
            limit=batch_size,
        )
        for row in rows:
            metadata = dict(row.metadata_ or {})
            metadata["processing_state"] = "expired"
            metadata[EVIDENCE_LAST_ERROR_KEY] = reason[:_MAX_ERROR_LENGTH]
            row.metadata_ = metadata
        return rows

    # =========================================================================
    # Claiming / leasing (short transaction, SKIP LOCKED — the outbox pattern)
    # =========================================================================

    async def claim_queued(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
        batch_size: int,
    ) -> list[EvidenceRefModel]:
        """Atomically claim QUEUED rows → EXTRACTING.

        A single SELECT ... FOR UPDATE SKIP LOCKED claims each row exactly
        once even under concurrent workers; the lease/state guards in the
        WHERE ensure only claimable rows are taken. Attempts increments
        per real delivery attempt (the retry budget).
        """
        claimed_until = iso_timestamp(now + timedelta(seconds=lease_seconds))
        rows = await self._select_for_update(
            session,
            predicate=(
                (EvidenceRefModel.metadata_["processing_state"].astext == "queued")
                & (
                    EvidenceRefModel.metadata_["processing_lease_until"].astext.is_(None)
                    | (
                        EvidenceRefModel.metadata_["processing_lease_until"].astext
                        <= iso_timestamp(now)
                    )
                )
            ),
            limit=batch_size,
        )
        for row in rows:
            metadata = dict(row.metadata_ or {})
            metadata["processing_state"] = "extracting"
            metadata[EVIDENCE_CLAIMED_BY_KEY] = worker_id
            metadata[EVIDENCE_LEASE_UNTIL_KEY] = claimed_until
            metadata[EVIDENCE_ATTEMPTS_KEY] = int(metadata.get(EVIDENCE_ATTEMPTS_KEY, 0)) + 1
            row.metadata_ = metadata
        return rows

    async def list_stale(
        self, session: AsyncSession, *, now: datetime, limit: int
    ) -> list[EvidenceRefModel]:
        """Stale EXTRACTING/UPLOADING rows whose lease expired (no lock)."""
        now_iso = iso_timestamp(now)
        stmt = (
            select(EvidenceRefModel)
            .where(
                EvidenceRefModel.metadata_["processing_state"].astext.in_((
                    "extracting",
                    "uploading",
                )),
                EvidenceRefModel.metadata_["processing_lease_until"].astext <= now_iso,
            )
            .order_by(EvidenceRefModel.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def lock_stale(
        self,
        session: AsyncSession,
        ref_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> EvidenceRefModel | None:
        """Re-claim a stale row under a row lock (the recovery serialization point).

        The FOR UPDATE lock serializes concurrent recovery workers; the
        re-check on the locked row means a row another worker just
        recovered (state moved / lease renewed) is skipped.
        """
        now_iso = iso_timestamp(now)
        stmt = (
            select(EvidenceRefModel)
            .where(
                EvidenceRefModel.ref_id == ref_id,
                EvidenceRefModel.metadata_["processing_state"].astext.in_((
                    "extracting",
                    "uploading",
                )),
                EvidenceRefModel.metadata_["processing_lease_until"].astext <= now_iso,
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        metadata = dict(row.metadata_ or {})
        metadata[EVIDENCE_CLAIMED_BY_KEY] = worker_id
        metadata[EVIDENCE_LEASE_UNTIL_KEY] = iso_timestamp(now + timedelta(seconds=lease_seconds))
        row.metadata_ = metadata
        return row

    # =========================================================================
    # Guarded transitions (never override a lost claim)
    # =========================================================================

    async def persist_transition(
        self,
        session: AsyncSession,
        ref: EvidenceRefModel,
        *,
        from_state: str,
        to_state: str,
        claimed_by: str,
        updates: dict[str, Any],
    ) -> bool:
        """Persist a guarded state transition.

        The UPDATE is conditional on (state == from_state AND claimed_by
        == worker), so a worker whose lease was lost can never override
        the new owner's state — the same guard the outbox repository
        applies to mark_published/mark_failed.
        """
        metadata = dict(ref.metadata_ or {})
        metadata.update(updates)
        metadata["processing_state"] = to_state
        stmt = (
            update(EvidenceRefModel)
            .where(
                EvidenceRefModel.ref_id == ref.ref_id,
                EvidenceRefModel.metadata_["processing_state"].astext == from_state,
                EvidenceRefModel.metadata_["processing_claimed_by"].astext == claimed_by,
            )
            .values(metadata=metadata)
            .returning(EvidenceRefModel.ref_id)
        )
        result = await session.execute(stmt)
        ok = result.scalar_one_or_none() is not None
        if ok:
            ref.metadata_ = metadata
        return ok

    async def save_finalized(
        self,
        session: AsyncSession,
        ref: EvidenceRefModel,
        *,
        claimed_by: str,
        package: EvidencePackageModel,
        link: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        """Atomic UPLOADING → FINALIZED + package row + ref link.

        One transaction: the guarded transition, the package row, and the
        M2M link commit together — a duplicate delivery can never persist
        a second package, and the package can never exist without the
        finalized state (and vice versa).
        """
        metadata = dict(ref.metadata_ or {})
        metadata.update(updates)
        metadata["processing_state"] = "finalized"
        stmt = (
            update(EvidenceRefModel)
            .where(
                EvidenceRefModel.ref_id == ref.ref_id,
                EvidenceRefModel.metadata_["processing_state"].astext == "uploading",
                EvidenceRefModel.metadata_["processing_claimed_by"].astext == claimed_by,
            )
            .values(metadata=metadata)
            .returning(EvidenceRefModel.ref_id)
        )
        result = await session.execute(stmt)
        if result.scalar_one_or_none() is None:
            return False
        session.add(package)
        session.add(
            package_evidence_refs.insert().values(
                package_id=link["package_id"],
                ref_id=link["ref_id"],
                tenant_id=link["tenant_id"],
            )
        )
        ref.metadata_ = metadata
        return True

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _select_for_update(
        self,
        session: AsyncSession,
        *,
        predicate: Any,
        limit: int,
    ) -> list[EvidenceRefModel]:
        stmt = (
            select(EvidenceRefModel)
            .where(predicate)
            .order_by(EvidenceRefModel.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _set_state(row: EvidenceRefModel, state: str) -> None:
        metadata = dict(row.metadata_ or {})
        metadata["processing_state"] = state
        row.metadata_ = metadata


__all__ = [
    "EVIDENCE_ARTIFACT_KEY",
    "EVIDENCE_ATTEMPTS_KEY",
    "EVIDENCE_CLAIMED_BY_KEY",
    "EVIDENCE_LAST_ERROR_KEY",
    "EVIDENCE_LEASE_UNTIL_KEY",
    "EVIDENCE_PACKAGE_ID_KEY",
    "EVIDENCE_RECOVERY_KEY",
    "EVIDENCE_REQUEST_KEY",
    "EVIDENCE_RETRY_AT_KEY",
    "EvidenceWorkRepository",
    "EvidenceWorkStore",
    "iso_timestamp",
]
