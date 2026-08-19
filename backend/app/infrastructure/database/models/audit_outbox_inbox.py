"""SQLAlchemy ORM models for the audit, outbox, and inbox persistence
layer (Task 6.12).

Three infrastructure tables connecting the transactional database to the
event transports (governance 3.11 / 3.12 / 3.13):

  AuditEventModel   — the trusted audit log (contracts/audit/models.py
                      AuditEvent). Globally append-only: actor and
                      tenant identity recorded as VALUES with no FKs so
                      the log survives tenant/user deletion (governance
                      3.11 + 10.3). Actor identity comes from the
                      trusted server-side ActorContext — there is no
                      client-supplied actor column. Metadata rejects
                      secret-like keys via the shared IMMUTABLE helper
                      (migration 013) with the audit contract's
                      first-segment semantics.
  OutboxEventModel  — the transactional outbox (governance 3.12).
                      Domain state + outbox row commit atomically in one
                      transaction; a publisher transports the row AFTER
                      commit (nothing is published to Redis before the
                      database commit). Unique event_id gives idempotent
                      delivery; explicit status lifecycle (pending ->
                      processing -> published | failed -> retry) is
                      trigger-enforced (migration 014); lease-based
                      worker claims support crash recovery.
  InboxMessageModel — idempotent inbound processing (governance 3.13).
                      Dedup by unique (source, source_message_id);
                      duplicate delivery is rejected by the unique key.

These are NOT tenant-scoped RLS tables — tenant_id is recorded for
scoping/claims, and workers poll across all tenants (governance 3.12/
3.13; see the migration 014 header for the full rationale). No ORM
relationships are declared — this is infrastructure storage (the schema
is the deliverable), consistent with the other domain models.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION

# Enum values — the AuditEvent contract's action categories and the
# explicit worker lifecycle states (governance 3.12/3.13).
_AUDIT_ACTION_CATEGORIES = (
    "authentication",
    "authorization",
    "venue",
    "video",
    "analytics",
    "evidence",
    "recommendation",
    "alert",
    "user",
    "membership",
    "tenant",
    "system",
)
_OUTBOX_STATUSES = ("pending", "processing", "published", "failed", "dead_letter")
_INBOX_STATUSES = ("pending", "processing", "processed", "failed", "dead_letter")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditEventModel(Base):
    __tablename__ = "audit_events"

    __table_args__ = (
        CheckConstraint(
            "length(btrim(action)) > 0",
            name="ck_audit_events_action_not_empty",
        ),
        # The audit contract's blocked-secret vocabulary applied at the
        # first key segment (contracts/audit/models.py) via the shared
        # IMMUTABLE helper from migration 013 (migration 014 reuses it).
        CheckConstraint(
            "metadata IS NULL OR NOT integration_config_has_secret(metadata)",
            name="ck_audit_events_metadata_no_secrets",
        ),
        # Query patterns (governance Section 9): tenant-scoped security
        # review, time-range export/SIEM, "who did what" investigations.
        Index("ix_audit_events_tenant_id", "tenant_id"),
        Index("ix_audit_events_actor_id", "actor_id"),
        Index("ix_audit_events_timestamp", text("timestamp DESC")),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
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
    # Trusted actor context — VALUES from ActorContext, never client
    # supplied; no FK (audit survives actor/tenant deletion).
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    membership_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    venue_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(512), nullable=False)
    action_category: Mapped[str] = mapped_column(
        Enum(*_AUDIT_ACTION_CATEGORIES, name="audit_action_category"),
        nullable=False,
    )
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The contract field `timestamp` — when the audit event was recorded.
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    # Non-sensitive metadata ONLY (secret keys rejected by CHECK).
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEventModel({self.audit_id}) "
            f"actor={self.actor_id!r} category={self.action_category!r}>"
        )


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"

    __table_args__ = (
        # Idempotent delivery — one outbox row per event_id.
        UniqueConstraint("event_id", name="uq_outbox_events_event_id"),
        CheckConstraint(
            "length(btrim(event_type)) > 0",
            name="ck_outbox_events_event_type_not_empty",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_outbox_events_updated_not_before_created",
        ),
        CheckConstraint(
            "published_at IS NULL OR published_at >= created_at",
            name="ck_outbox_events_published_not_before_created",
        ),
        CheckConstraint(
            "claimed_until IS NULL OR claimed_until >= created_at",
            name="ck_outbox_events_lease_not_before_created",
        ),
        Index("ix_outbox_events_tenant_id", "tenant_id"),
        Index("ix_outbox_events_venue_id", "venue_id"),
        # The worker poller's hot subset — pending/failed rows whose
        # backoff window has elapsed (governance 9 rule 3; migration 016).
        Index(
            "ix_outbox_events_pending",
            "available_at",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
    )

    outbox_id: Mapped[uuid.UUID] = mapped_column(
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
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Recorded tenant for scoping/claims (not an RLS-scoped FK).
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Venue scope of the published event where applicable (recorded VALUE).
    venue_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_OUTBOX_STATUSES, name="outbox_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    # Worker claim lease for crash recovery.
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Retry scheduling — the worker only claims rows once available_at has
    # elapsed (bounded exponential backoff is persisted, not in-memory).
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The most recent delivery error — preserved for dead-letter recovery.
    last_error: Mapped[str | None] = mapped_column(nullable=True, default=None)

    def __repr__(self) -> str:
        return (
            f"<OutboxEventModel({self.outbox_id}) event={self.event_id!r} status={self.status!r}>"
        )


class InboxMessageModel(Base):
    __tablename__ = "inbox_messages"

    __table_args__ = (
        # Idempotency — duplicate delivery is detected and rejected.
        UniqueConstraint(
            "source",
            "source_message_id",
            name="uq_inbox_messages_source_message_id",
        ),
        CheckConstraint(
            "length(btrim(source)) > 0",
            name="ck_inbox_messages_source_not_empty",
        ),
        CheckConstraint(
            "length(btrim(source_message_id)) > 0",
            name="ck_inbox_messages_source_message_id_not_empty",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= received_at",
            name="ck_inbox_messages_updated_not_before_received",
        ),
        CheckConstraint(
            "processed_at IS NULL OR processed_at >= received_at",
            name="ck_inbox_messages_processed_not_before_received",
        ),
        CheckConstraint(
            "claimed_until IS NULL OR claimed_until >= received_at",
            name="ck_inbox_messages_lease_not_before_received",
        ),
        Index("ix_inbox_messages_tenant_id", "tenant_id"),
        Index("ix_inbox_messages_venue_id", "venue_id"),
        # The worker poller's hot subset — pending/failed rows whose
        # backoff window has elapsed (governance 9 rule 3; migration 016).
        Index(
            "ix_inbox_messages_pending",
            "available_at",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
    )

    inbox_id: Mapped[uuid.UUID] = mapped_column(
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
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Venue context of the inbound message where applicable (recorded VALUE).
    venue_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_INBOX_STATUSES, name="inbox_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    # Worker claim lease for crash recovery.
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Retry scheduling — the worker only claims rows once available_at has
    # elapsed (bounded exponential backoff is persisted, not in-memory).
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The most recent processing error — preserved for dead-letter recovery.
    last_error: Mapped[str | None] = mapped_column(nullable=True, default=None)

    def __repr__(self) -> str:
        return f"<InboxMessageModel({self.inbox_id}) source={self.source!r} status={self.status!r}>"
