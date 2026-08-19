"""SQLAlchemy ORM model for API/service-level idempotency (Task 7).

IdempotencyRecordModel backs the idempotency_records table (migration
016): a tenant-scoped uniqueness unit (tenant_id, operation,
idempotency_key) whose request_hash is the canonical SHA-256 of the
request payload. Behavior:

  - replay:      same tenant + operation + key + same payload hash →
                 the stored logical result is returned, the operation
                 is NOT executed again
  - conflict:    same tenant + operation + key + DIFFERENT payload hash
                 → rejected (409-conflict semantics at the service
                 layer) without executing the operation
  - concurrency: a worker lease (claimed_by/claimed_until) on the
                 'in_progress' record means simultaneous identical
                 requests race on INSERT ... ON CONFLICT DO NOTHING —
                 only the lease holder executes; the losers poll and
                 replay the stored result. An expired lease is
                 reclaimable (crash recovery).

tenant_id/actor_id/venue_id are recorded VALUES derived from the
trusted server-side ActorContext — never client-supplied. No RLS (the
platform-infrastructure rationale of outbox/inbox applies; repository
methods always scope lookups by the ActorContext tenant, and venue
scope is enforced at the service layer).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION

_IDEMPOTENCY_STATUSES = ("in_progress", "completed")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"

    __table_args__ = (
        # Tenant-scoped idempotency unit — a tenant's key can never
        # collide with another tenant's key.
        UniqueConstraint(
            "tenant_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_records_tenant_operation_key",
        ),
        CheckConstraint(
            "length(btrim(operation)) > 0",
            name="ck_idempotency_records_operation_not_empty",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_idempotency_records_key_not_empty",
        ),
        # Canonical SHA-256 hex digest of the request payload.
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_idempotency_records_request_hash_sha256",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_idempotency_records_updated_not_before_created",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_idempotency_records_completed_not_before_created",
        ),
        CheckConstraint(
            "claimed_until IS NULL OR claimed_until >= created_at",
            name="ck_idempotency_records_lease_not_before_created",
        ),
        # Query patterns (governance Section 9): tenant-scoped service
        # lookups, expiry-based pruning of stale records.
        Index("ix_idempotency_records_tenant_id", "tenant_id"),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    idempotency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schema_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SCHEMA_VERSION,
        server_default=SCHEMA_VERSION,
    )
    # Recorded tenant for scoping (not an RLS-scoped FK).
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Recorded VALUES from the trusted server-side ActorContext.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    venue_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    # Client-supplied key — validated by the service (length/charset).
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Canonical SHA-256 hex of the request payload.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_IDEMPOTENCY_STATUSES, name="idempotency_status"),
        nullable=False,
        default="in_progress",
        server_default="in_progress",
    )
    # The logical result reference of the completed operation.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    # Worker claim lease for concurrent/duplicate request safety.
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional housekeeping horizon for expired-record pruning.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<IdempotencyRecordModel({self.idempotency_id}) "
            f"operation={self.operation!r} key={self.idempotency_key!r} "
            f"status={self.status!r}>"
        )
