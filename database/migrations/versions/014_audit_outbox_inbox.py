"""Create the audit, outbox, and inbox persistence schema (Task 6.12).

Three infrastructure tables that connect the transactional database to
the event transports (governance 3.11 / 3.12 / 3.13):

  audit_events   — the trusted audit log (contracts/audit/models.py
                   AuditEvent). Globally append-only: tenant_id and the
                   actor identity are recorded as VALUES, never as
                   cascade-deleting FKs, so the log survives tenant or
                   user deletion (governance 3.11 + 10.3). Actor
                   identity is the TRUSTED server-side ActorContext —
                   the schema has no client-supplied actor column, and
                   audit metadata rejects secret-like keys with the
                   audit contract's own first-segment semantics
                   (contracts/audit/models.py validator). Append-only
                   is enforced by grants: hotelops_app gets SELECT,
                   INSERT only — no UPDATE, no DELETE.
  outbox_events  — the transactional outbox (governance 3.12). Domain
                   state change + outbox row COMMIT atomically in the
                   same transaction (the service writes both inside
                   one DatabaseClient.session); a publisher later
                   transports the row. Nothing is published to Redis
                   before the database commit. At-most-once per event:
                   uq_outbox_events_event_id (idempotent delivery via
                   unique event ID + processed status). Worker claims
                   (claimed_by/claimed_until lease) + explicit status
                   lifecycle (pending -> processing -> published |
                   failed -> pending retry) enforced by a trigger.
                   Direct transitions pending -> failed (validate-on-
                   insert failure) and failed -> processing (direct
                   retry without re-queuing) are intentionally legal;
                   published is terminal.
  inbox_messages — idempotent inbound processing (governance 3.13).
                   Deduplication by uq_inbox_messages_source_message_id
                   on (source, source_message_id) — duplicate delivery
                   is detected by the unique key and rejected. Same
                   claim/lease + status lifecycle as the outbox.

Design decisions (each maps to a governance policy):

  - TENANT SCOPING: audit/outbox/inbox are NOT tenant-scoped RLS
    tables. Governance 3.12/3.13 scope them as "tenant_id recorded for
    scoping/claims" (not DIRECT ownership), and the outbox/inbox
    workers must poll rows for ALL tenants without an app.tenant_id
    context — RLS would fail closed and break the workers. audit_events
    is globally append-only (governance 10.3). tenant_id is therefore a
    recorded VALUE with no FK, and NO RLS policies are created
    (Section 10.4 rule 5 applies to tenant-scoped tables; these are
    platform infrastructure). The audit append-only property is instead
    enforced by grants.
  - TRUSTED AUDIT IDENTITY: actor_id/tenant_id/venue_id/membership_id
    are NOT NULL-or-nullable UUID VALUES populated only from the
    authenticated ActorContext by the service. No request payload can
    influence them; no FK ties audit rows to mutable identity rows
    (audit must survive their deletion).
  - NO SECRETS IN AUDIT: the metadata CHECK reuses the shared IMMUTABLE
    helper integration_config_has_secret (migration 013) — the exact
    same blocked-term vocabulary (password, token, secret, key,
    credential, authorization) and first-segment semantics as the
    AuditEvent contract validator, so 'secret_key' is blocked while
    'api_key' is allowed. A CHECK constraint (not a trigger) mirrors
    the Python validator 1:1.
  - TIMESTAMPS: received_at/created_at are server now() (UTC);
    published_at/processed_at are stamped by the transition triggers;
    claimed_until is the lease-expiry horizon for crash recovery.
  - INDEXES: partial indexes on the worker pollers' hot subset —
    ix_outbox_events_pending / ix_inbox_messages_pending WHERE
    status = 'pending' (governance Section 9 rule 3 explicitly cites
    outbox/inbox pending subsets). Unique keys serve idempotency.
  - Relational, NOT hypertables (governance 3.12/3.13: short-lived
    rows; audit is a hypertable candidate only if volume warrants —
    deferred, per Section 11).
  - Grants: audit SELECT/INSERT (append-only), outbox/inbox
    SELECT/INSERT/UPDATE (workers claim + transition). DELETE is not
    granted to the app role — pruning (outbox delivery cleanup, audit
    retention) is an operator/admin task.

Revision ID: 014_audit_outbox_inbox
Revises: 013_integration_storage
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "014_audit_outbox_inbox"
down_revision: str | None = "013_integration_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    # audit is append-only — no UPDATE/DELETE grants at all.
    "GRANT SELECT, INSERT ON audit_events TO hotelops_app;",
    # outbox/inbox workers must claim + transition rows.
    "GRANT SELECT, INSERT, UPDATE ON outbox_events TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE ON inbox_messages TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON audit_events FROM hotelops_app;",
    "REVOKE ALL ON outbox_events FROM hotelops_app;",
    "REVOKE ALL ON inbox_messages FROM hotelops_app;",
]

# Explicit worker lifecycle (governance 3.12/3.13: worker poll -> claim ->
# deliver/process -> retry on failure). A CHECK cannot compare OLD/NEW row
# values, so transition legality is enforced by BEFORE UPDATE triggers.
_SQL_OUTBOX_TRANSITION_FUNCTION = """
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

_SQL_OUTBOX_TRANSITION_TRIGGER = """
CREATE TRIGGER trg_outbox_events_status_transition
BEFORE UPDATE OF status ON outbox_events
FOR EACH ROW EXECUTE FUNCTION check_outbox_status_transition();
"""

_SQL_INBOX_TRANSITION_FUNCTION = """
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

_SQL_INBOX_TRANSITION_TRIGGER = """
CREATE TRIGGER trg_inbox_messages_status_transition
BEFORE UPDATE OF status ON inbox_messages
FOR EACH ROW EXECUTE FUNCTION check_inbox_status_transition();
"""

# Contract AuditActionCategory — the full stable set (contracts/audit/
# models.py). Kept in sync by hand; the enum name matches the contract
# naming convention (snake_case singular).
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


def upgrade() -> None:
    """Create the audit/outbox/inbox tables, triggers, and grants."""

    # --- AUDIT (globally append-only, trusted identity) ---
    op.create_table(
        "audit_events",
        sa.Column("audit_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        # Trusted actor context — recorded as VALUES from ActorContext,
        # never client-supplied; no FK (audit survives actor deletion).
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("membership_id", sa.UUID(), nullable=True),
        sa.Column("venue_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(512), nullable=False),
        sa.Column(
            "action_category",
            sa.Enum(*_AUDIT_ACTION_CATEGORIES, name="audit_action_category"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        # The contract field `timestamp` — when the audit event was recorded.
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Non-sensitive metadata ONLY (secret keys rejected by CHECK).
        sa.Column("metadata", JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.CheckConstraint(
            "length(btrim(action)) > 0",
            name="ck_audit_events_action_not_empty",
        ),
        # The audit contract's blocked-secret vocabulary applied at the
        # first key segment (contracts/audit/models.py) via the shared
        # IMMUTABLE helper from migration 013.
        sa.CheckConstraint(
            "metadata IS NULL OR NOT integration_config_has_secret(metadata)",
            name="ck_audit_events_metadata_no_secrets",
        ),
    )
    # Query patterns (governance Section 9): tenant-scoped security review,
    # time-range export/SIEM, "who did what" actor investigations.
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_timestamp", "audit_events", [sa.text("timestamp DESC")])

    # --- OUTBOX (transactional publication) ---
    op.create_table(
        "outbox_events",
        sa.Column("outbox_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        # The published event's identity — at most one outbox row per
        # event (idempotent delivery).
        sa.Column("event_id", sa.UUID(), nullable=False),
        # Recorded tenant for scoping/claims (not an RLS-scoped FK).
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        # The transport payload (a contract-validated event envelope).
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "published", "failed", name="outbox_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # Worker claim lease for crash recovery.
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("outbox_id"),
        # Idempotent delivery — one outbox row per event_id.
        sa.UniqueConstraint("event_id", name="uq_outbox_events_event_id"),
        sa.CheckConstraint(
            "length(btrim(event_type)) > 0",
            name="ck_outbox_events_event_type_not_empty",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_outbox_events_updated_not_before_created",
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at >= created_at",
            name="ck_outbox_events_published_not_before_created",
        ),
        sa.CheckConstraint(
            "claimed_until IS NULL OR claimed_until >= created_at",
            name="ck_outbox_events_lease_not_before_created",
        ),
    )
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"])
    # The worker poller's hot subset (governance 9 rule 3).
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.execute(_SQL_OUTBOX_TRANSITION_FUNCTION)
    op.execute(_SQL_OUTBOX_TRANSITION_TRIGGER)

    # --- INBOX (idempotent inbound processing) ---
    op.create_table(
        "inbox_messages",
        sa.Column("inbox_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        # Recorded tenant for scoping/claims (not an RLS-scoped FK).
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # The external source + its own message id — the deduplication key.
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_message_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=True),
        # The inbound message body (contract-validated).
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "processed", "failed", name="inbox_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # Worker claim lease for crash recovery.
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("inbox_id"),
        # Idempotency — duplicate delivery is detected and rejected.
        sa.UniqueConstraint(
            "source",
            "source_message_id",
            name="uq_inbox_messages_source_message_id",
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0",
            name="ck_inbox_messages_source_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(source_message_id)) > 0",
            name="ck_inbox_messages_source_message_id_not_empty",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= received_at",
            name="ck_inbox_messages_updated_not_before_received",
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR processed_at >= received_at",
            name="ck_inbox_messages_processed_not_before_received",
        ),
        sa.CheckConstraint(
            "claimed_until IS NULL OR claimed_until >= received_at",
            name="ck_inbox_messages_lease_not_before_received",
        ),
    )
    op.create_index("ix_inbox_messages_tenant_id", "inbox_messages", ["tenant_id"])
    # The worker poller's hot subset (governance 9 rule 3).
    op.create_index(
        "ix_inbox_messages_pending",
        "inbox_messages",
        ["received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.execute(_SQL_INBOX_TRANSITION_FUNCTION)
    op.execute(_SQL_INBOX_TRANSITION_TRIGGER)

    # --- Grants (no RLS — platform infrastructure, see header) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)


def downgrade() -> None:
    """Drop the audit/outbox/inbox tables, triggers, and grants."""
    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.execute("DROP TRIGGER IF EXISTS trg_outbox_events_status_transition ON outbox_events")
    op.execute("DROP TRIGGER IF EXISTS trg_inbox_messages_status_transition ON inbox_messages")
    op.execute("DROP FUNCTION IF EXISTS check_outbox_status_transition()")
    op.execute("DROP FUNCTION IF EXISTS check_inbox_status_transition()")

    op.drop_table("inbox_messages")
    op.drop_table("outbox_events")
    op.drop_table("audit_events")
    # Single-use enum types created by op.create_table; drop explicitly so
    # a downgrade fully reverses the upgrade (005/009/011/012/013 pattern).
    op.execute("DROP TYPE audit_action_category")
    op.execute("DROP TYPE outbox_status")
    op.execute("DROP TYPE inbox_status")
