"""Task 7 reliability extensions (revision 016).

Extends the Task 6.12 outbox/inbox persistence with the operational
semantics required by Task 7 (transactional outbox, inbox, idempotency):

  OUTBOX / INBOX RETRY SCHEDULING
    available_at (timestamptz NOT NULL, default now()) — a worker only
    claims a row once available_at <= now(). New rows are immediately
    claimable; a failed delivery is scheduled for available_at =
    now() + backoff(attempts). This makes bounded exponential backoff a
    database-visible fact (the poller never computes it in memory).

    last_error (TEXT NULL) — the most recent delivery/processing error,
    preserved for operational recovery and dead-letter inspection.

  OUTBOX VENUE CONTEXT
    venue_id (UUID NULL) — the venue scope of a published event where
    applicable (recorded VALUE, no FK, same tenancy convention as
    tenant_id: recorded for scoping, NOT an RLS table).

  DEAD-LETTER STATE
    outbox_status gains 'dead_letter'; inbox_status gains 'dead_letter'.
    A row that exhausts its retry budget (attempts >= max_attempts) or
    hits a non-retryable error transitions to dead_letter and is NEVER
    deleted — it remains inspectable with payload, tenant, attempts and
    last_error intact. dead_letter is terminal (no transition out).
    The 014 transition triggers are REPLACED (CREATE OR REPLACE) with
    the extended transition sets; 014's legal transitions are all kept.

  PARTIAL INDEX REBUILD
    ix_outbox_events_pending / ix_inbox_messages_pending are rebuilt on
    (available_at) WHERE status IN ('pending','failed') — the poller's
    hot subset is now "due-for-claim" (pending or failed rows whose
    backoff has elapsed) instead of "ever-created pending".

  IDEMPOTENCY RECORDS
    idempotency_records — API/service-level idempotency (Task 7 Phase
    11): (tenant_id, operation, idempotency_key) is the tenant-scoped
    uniqueness unit; request_hash is the canonical SHA-256 of the
    request payload so a replayed key with a DIFFERENT payload can be
    rejected (409 conflict) without executing the operation. status
    enum idempotency_status ('in_progress' | 'completed') + a worker
    lease (claimed_by/claimed_until) makes simultaneous identical
    requests safe: only the lease holder executes; losers wait and
    replay the stored result. actor_id/venue_id are recorded VALUES
    from the trusted server-side ActorContext (never client-supplied).
    No RLS — same platform-infrastructure rationale as outbox/inbox
    (tenant_id recorded for scoping; application RBAC + repository
    filters enforce tenant/venue isolation; the repository always
    scopes lookups by the ActorContext tenant).

  GRANTS
    hotelops_app receives SELECT/INSERT/UPDATE on idempotency_records
    (the idempotency service claims + completes rows). No DELETE —
    pruning expired records is an operator task, like outbox/inbox.

Downgrade note: PostgreSQL cannot DROP a value from an enum type, so
'dead_letter' remains in the enum after a downgrade (harmless — the
restored 014 trigger functions refuse to transition INTO it). All other
changes (columns, indexes, table, triggers) are fully reversed.

Revision ID: 016_outbox_retry_idempotency
Revises: 015_constraint_index_review
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "016_outbox_retry_idempotency"
down_revision: str | None = "015_constraint_index_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_GRANT_IDEMPOTENCY = "GRANT SELECT, INSERT, UPDATE ON idempotency_records TO hotelops_app;"
_SQL_REVOKE_IDEMPOTENCY = "REVOKE ALL ON idempotency_records FROM hotelops_app;"

# Extended outbox lifecycle: 014's set + dead_letter (terminal).
_SQL_OUTBOX_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION check_outbox_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status = 'pending' AND NEW.status IN ('processing', 'failed'))
       OR (OLD.status = 'processing' AND NEW.status IN ('published', 'failed', 'pending', 'dead_letter'))
       OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'processing', 'dead_letter')) THEN
        NEW.updated_at := now();
        IF NEW.status = 'published' THEN
            NEW.published_at := COALESCE(NEW.published_at, now());
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal outbox status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""

# Extended inbox lifecycle: 014's set + dead_letter (terminal).
_SQL_INBOX_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION check_inbox_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status = 'pending' AND NEW.status IN ('processing', 'failed'))
       OR (OLD.status = 'processing' AND NEW.status IN ('processed', 'failed', 'pending', 'dead_letter'))
       OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'processing', 'dead_letter')) THEN
        NEW.updated_at := now();
        IF NEW.status = 'processed' THEN
            NEW.processed_at := COALESCE(NEW.processed_at, now());
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal inbox status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""

# The ORIGINAL 014 trigger bodies — restored by the downgrade so the
# post-downgrade schema enforces exactly the 014 transition semantics.
_SQL_OUTBOX_TRANSITION_FUNCTION_014 = """
CREATE OR REPLACE FUNCTION check_outbox_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status = 'pending' AND NEW.status IN ('processing', 'failed'))
       OR (OLD.status = 'processing' AND NEW.status IN ('published', 'failed', 'pending'))
       OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'processing')) THEN
        NEW.updated_at := now();
        IF NEW.status = 'published' THEN
            NEW.published_at := COALESCE(NEW.published_at, now());
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal outbox status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""

_SQL_INBOX_TRANSITION_FUNCTION_014 = """
CREATE OR REPLACE FUNCTION check_inbox_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status = 'pending' AND NEW.status IN ('processing', 'failed'))
       OR (OLD.status = 'processing' AND NEW.status IN ('processed', 'failed', 'pending'))
       OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'processing')) THEN
        NEW.updated_at := now();
        IF NEW.status = 'processed' THEN
            NEW.processed_at := COALESCE(NEW.processed_at, now());
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal inbox status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    """Extend outbox/inbox for retry+dead-letter and add idempotency."""

    # --- OUTBOX: venue context + retry scheduling + error preservation ---
    op.add_column("outbox_events", sa.Column("venue_id", sa.UUID(), nullable=True))
    op.add_column(
        "outbox_events",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # sa.String() (no length) renders as VARCHAR — matches the ORM
    # metadata exactly (models map ``last_error: Mapped[str | None]`` to
    # String), so ``alembic check`` reports no drift.
    op.add_column("outbox_events", sa.Column("last_error", sa.String(), nullable=True))
    op.create_index("ix_outbox_events_venue_id", "outbox_events", ["venue_id"])

    # --- INBOX: venue context + retry scheduling + error preservation ---
    op.add_column("inbox_messages", sa.Column("venue_id", sa.UUID(), nullable=True))
    op.add_column(
        "inbox_messages",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("inbox_messages", sa.Column("last_error", sa.String(), nullable=True))
    op.create_index("ix_inbox_messages_venue_id", "inbox_messages", ["venue_id"])

    # --- DEAD-LETTER: extend the status enums (additive; PG >= 12 allows
    # this inside a transaction; the new values are only used in trigger
    # bodies — never in a same-transaction DML statement). ---
    op.execute("ALTER TYPE outbox_status ADD VALUE 'dead_letter'")
    op.execute("ALTER TYPE inbox_status ADD VALUE 'dead_letter'")

    # --- REPLACE the transition triggers with the extended sets ---
    op.execute(_SQL_OUTBOX_TRANSITION_FUNCTION)
    op.execute(_SQL_INBOX_TRANSITION_FUNCTION)

    # --- REBUILD the poller partial indexes on the due-for-claim subset ---
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["available_at"],
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.drop_index("ix_inbox_messages_pending", table_name="inbox_messages")
    op.create_index(
        "ix_inbox_messages_pending",
        "inbox_messages",
        ["available_at"],
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )

    # --- IDEMPOTENCY RECORDS ---
    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        # Recorded tenant for scoping — the repository always filters by
        # the ActorContext tenant (tenant isolation at the query layer).
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # Recorded VALUES from the trusted server-side ActorContext.
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("venue_id", sa.UUID(), nullable=True),
        sa.Column("operation", sa.String(128), nullable=False),
        # Client-supplied key — validated by the service (length/charset).
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        # Canonical SHA-256 hex of the request payload (64 chars).
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("in_progress", "completed", name="idempotency_status"),
            nullable=False,
            server_default="in_progress",
        ),
        # The logical result reference of the completed operation.
        sa.Column("result", JSONB(), nullable=True),
        # Worker claim lease for concurrent/duplicate request safety.
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Optional housekeeping horizon for expired-record pruning.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("idempotency_id"),
        # Tenant-scoped idempotency unit — Tenant A's key cannot collide
        # with Tenant B's key (no cross-tenant lookup is even possible).
        sa.UniqueConstraint(
            "tenant_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_records_tenant_operation_key",
        ),
        sa.CheckConstraint(
            "length(btrim(operation)) > 0",
            name="ck_idempotency_records_operation_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_idempotency_records_key_not_empty",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_idempotency_records_request_hash_sha256",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_idempotency_records_updated_not_before_created",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_idempotency_records_completed_not_before_created",
        ),
        sa.CheckConstraint(
            "claimed_until IS NULL OR claimed_until >= created_at",
            name="ck_idempotency_records_lease_not_before_created",
        ),
    )
    # Query patterns: tenant-scoped service lookups, expiry-based pruning.
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])
    op.create_index("ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"])

    op.execute(_SQL_GRANT_IDEMPOTENCY)


def downgrade() -> None:
    """Reverse the Task 7 extensions (see header for the enum caveat)."""
    op.execute(_SQL_REVOKE_IDEMPOTENCY)

    # Restore the 014 trigger semantics (dead_letter becomes unreachable).
    op.execute(_SQL_OUTBOX_TRANSITION_FUNCTION_014)
    op.execute(_SQL_INBOX_TRANSITION_FUNCTION_014)

    # Restore the 014 poller indexes.
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_inbox_messages_pending", table_name="inbox_messages")
    op.create_index(
        "ix_inbox_messages_pending",
        "inbox_messages",
        ["received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.drop_table("idempotency_records")
    op.execute("DROP TYPE idempotency_status")

    op.drop_index("ix_outbox_events_venue_id", table_name="outbox_events")
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "available_at")
    op.drop_column("outbox_events", "venue_id")
    op.drop_index("ix_inbox_messages_venue_id", table_name="inbox_messages")
    op.drop_column("inbox_messages", "last_error")
    op.drop_column("inbox_messages", "available_at")
    op.drop_column("inbox_messages", "venue_id")
