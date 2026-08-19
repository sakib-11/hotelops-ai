"""Create the alert & approval persistence schema (Task 6.10).

Persists the Task 4 operational contracts (contracts/operations/
models.py) — `Alert`, `ApprovalRequest`, `ApprovalStatus` — with
EXPLICIT state transitions and no uncontrolled boolean combinations
(no is_approved / is_rejected / is_pending columns):

  alerts              — a notification/operational signal with a
                        stateful lifecycle (governance 3.8): raised ->
                        acknowledged -> resolved/expired. Carries the
                        Alert contract fields (alert_type, severity,
                        title, description, event_time, source_ref)
                        plus direct tenant/venue ownership and a status
                        enum `alert_status`.
  approval_requests   — a request for human approval before an action
                        (governance 3.9): identifies the request,
                        actor/context (requested_by), subject
                        (recommendation_id), explicit state enum
                        `approval_status` (pending/approved/rejected/
                        cancelled — the contract ApprovalStatus), and
                        timestamps (requested_at/resolved_at).
  approval_decisions  — APPEND-ONLY decision history (governance 3.9:
                        "decisions append-only"). One row per decision:
                        actor, decision, reason, decided_at. A partial
                        unique index guarantees at most one terminal
                        decision per request (duplicate-approval guard).

Design decisions (each maps to a governance policy):

  - EXPLICIT STATE, NOT BOOLEANS: status is a single enum column on
    alerts and approval_requests. Transition LEGALITY is enforced at
    the database by BEFORE UPDATE triggers (a CHECK cannot compare
    OLD/NEW): alerts raised->acknowledged/resolved/expired and
    acknowledged->resolved/expired; approvals pending->approved/
    rejected/cancelled. Terminal states have no outgoing transitions —
    an illegal UPDATE raises and is rolled back (tested).
  - Alerts tenant/venue ownership: DIRECT — tenant_id NOT NULL +
    composite FK (venue_id, tenant_id). source_ref is polymorphic in
    the contract (FindingId | RecommendationId | None); represented as
    two nullable real composite FKs (finding_id / recommendation_id)
    with an at-most-one CHECK — never an unconstrained ID column
    (governance Section 8).
  - Approvals tenancy: DIRECT tenant_id; the subject is a real
    composite FK (recommendation_id, tenant_id) -> recommendations so
    a request can never reference another tenant's recommendation.
    Actor/context: requested_by + decisions.actor_id FK -> users
    (users is a platform catalog, governance 10.3).
  - Duplicate approval handling: the state trigger blocks any
    transition out of a terminal state, and the partial unique index
    on approval_decisions (one terminal decision per request) rejects
    a second decision row (tested). NOTE: `approval_decision` currently
    only admits terminal values, so the partial index is effectively a
    unique on request_id — it is kept partial so a future non-terminal
    decision value would be excluded from the guard.
  - Triggers guard UPDATE OF status only. A direct INSERT may carry any
    enum value (a raised-first / pending-first workflow); the transition
    state machine applies to lifecycle changes, not initial creation.
  - Timestamps: created_at (server now(), UTC), updated_at for status
    transitions, requested_at/resolved_at per the contract, decided_at
    on the append-only decision.
  - Relational, NOT hypertables (governance 3.8/3.9: current-state,
    low volume; alert history deferral documented in 3.8).
  - RLS + grants ship in the same migration (Section 10.4 rule 5).

Revision ID: 012_alert_approval_storage
Revises: 011_ai_domain_storage
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "012_alert_approval_storage"
down_revision: str | None = "011_ai_domain_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON alerts TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON approval_requests TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON approval_decisions TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON alerts FROM hotelops_app;",
    "REVOKE ALL ON approval_requests FROM hotelops_app;",
    "REVOKE ALL ON approval_decisions FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)

# Explicit state machines (governance 3.8 / 3.9). A CHECK cannot compare
# OLD/NEW row values, so transition legality is enforced by BEFORE UPDATE
# triggers; illegal transitions RAISE and the UPDATE rolls back.
_SQL_ALERT_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION check_alert_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status = 'raised' AND NEW.status IN ('acknowledged', 'resolved', 'expired'))
       OR (OLD.status = 'acknowledged' AND NEW.status IN ('resolved', 'expired')) THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal alert status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""

_SQL_ALERT_TRANSITION_TRIGGER = """
CREATE TRIGGER trg_alerts_status_transition
BEFORE UPDATE OF status ON alerts
FOR EACH ROW EXECUTE FUNCTION check_alert_status_transition();
"""

_SQL_APPROVAL_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION check_approval_request_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected', 'cancelled') THEN
        NEW.resolved_at := COALESCE(NEW.resolved_at, now());
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal approval status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""

_SQL_APPROVAL_TRANSITION_TRIGGER = """
CREATE TRIGGER trg_approval_requests_status_transition
BEFORE UPDATE OF status ON approval_requests
FOR EACH ROW EXECUTE FUNCTION check_approval_request_status_transition();
"""


def upgrade() -> None:
    """Create the alert/approval tables, triggers, and RLS policies."""

    # --- ALERTS (stateful operational signals) ---
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("alert_type", sa.String(100), nullable=False),
        sa.Column(
            "severity",
            sa.Enum("critical", "high", "medium", "low", "info", name="alert_severity"),
            nullable=False,
            server_default="info",
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.String(4096), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        # Polymorphic source_ref (FindingId | RecommendationId | None) as two
        # real composite FKs — at most one set (CHECK below).
        sa.Column("finding_id", sa.UUID(), nullable=True),
        sa.Column("recommendation_id", sa.UUID(), nullable=True),
        # Explicit lifecycle state — NEVER boolean flags.
        sa.Column(
            "status",
            sa.Enum("raised", "acknowledged", "resolved", "expired", name="alert_status"),
            nullable=False,
            server_default="raised",
        ),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Last status transition (mutations are legal — lifecycle state).
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("alert_id"),
        sa.CheckConstraint(
            "length(btrim(alert_type)) > 0",
            name="ck_alerts_alert_type_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0",
            name="ck_alerts_title_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_alerts_description_not_empty",
        ),
        # At most one source ref — never both a finding and a recommendation.
        sa.CheckConstraint(
            "finding_id IS NULL OR recommendation_id IS NULL",
            name="ck_alerts_source_single",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_alerts_updated_not_before_created",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_alerts_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "tenant_id"],
            ["findings.finding_id", "findings.tenant_id"],
            ondelete="CASCADE",
            name="fk_alerts_finding_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id", "tenant_id"],
            ["recommendations.recommendation_id", "recommendations.tenant_id"],
            ondelete="CASCADE",
            name="fk_alerts_recommendation_tenant",
        ),
    )
    # Query patterns (governance Section 9): tenant/venue-scoped alert-center
    # dashboard (status filter), time-range review, source-provenance.
    op.create_index("ix_alerts_tenant_id", "alerts", ["tenant_id"])
    op.create_index("ix_alerts_venue_id", "alerts", ["venue_id"])
    op.create_index("ix_alerts_status", "alerts", ["status"])
    op.create_index("ix_alerts_event_time", "alerts", [sa.text("event_time DESC")])
    op.create_index("ix_alerts_finding_id", "alerts", ["finding_id"])
    op.create_index("ix_alerts_recommendation_id", "alerts", ["recommendation_id"])

    op.execute(_SQL_ALERT_TRANSITION_FUNCTION)
    op.execute(_SQL_ALERT_TRANSITION_TRIGGER)

    # --- APPROVAL REQUESTS (explicit state machine) ---
    op.create_table(
        "approval_requests",
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # Subject — the recommendation being approved.
        sa.Column("recommendation_id", sa.UUID(), nullable=False),
        # Actor/context — who requested the approval.
        sa.Column("requested_by", sa.UUID(), nullable=False),
        # Explicit state — the contract ApprovalStatus enum.
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", "cancelled", name="approval_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
        # Composite FK target for approval_decisions.
        sa.UniqueConstraint("request_id", "tenant_id", name="uq_approval_requests_request_tenant"),
        sa.CheckConstraint(
            "reason IS NULL OR length(btrim(reason)) > 0",
            name="ck_approval_requests_reason_not_empty",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= requested_at",
            name="ck_approval_requests_resolved_after_requested",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_approval_requests_updated_not_before_created",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id", "tenant_id"],
            ["recommendations.recommendation_id", "recommendations.tenant_id"],
            ondelete="CASCADE",
            name="fk_approval_requests_recommendation_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.user_id"],
            ondelete="RESTRICT",
            name="fk_approval_requests_requested_by",
        ),
    )
    op.create_index("ix_approval_requests_tenant_id", "approval_requests", ["tenant_id"])
    op.create_index("ix_approval_requests_status", "approval_requests", ["status"])
    op.create_index(
        "ix_approval_requests_recommendation_id", "approval_requests", ["recommendation_id"]
    )
    op.create_index(
        "ix_approval_requests_requested_at", "approval_requests", [sa.text("requested_at DESC")]
    )

    op.execute(_SQL_APPROVAL_TRANSITION_FUNCTION)
    op.execute(_SQL_APPROVAL_TRANSITION_TRIGGER)

    # --- APPROVAL DECISIONS (append-only decision history) ---
    op.create_table(
        "approval_decisions",
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        # Denormalized tenant (FK-derived) so rows are RLS-scoped.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("approved", "rejected", "cancelled", name="approval_decision"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(1024), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.CheckConstraint(
            "reason IS NULL OR length(btrim(reason)) > 0",
            name="ck_approval_decisions_reason_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            ["approval_requests.request_id", "approval_requests.tenant_id"],
            ondelete="CASCADE",
            name="fk_approval_decisions_request_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
            name="fk_approval_decisions_actor",
        ),
    )
    # Duplicate-approval guard: at most one terminal decision per request
    # (partial unique index — the 007 pattern, so alembic check sees the
    # same index shape it reflects from PostgreSQL).
    op.create_index(
        "uq_approval_decisions_terminal",
        "approval_decisions",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("decision IN ('approved', 'rejected', 'cancelled')"),
    )
    op.create_index("ix_approval_decisions_request_id", "approval_decisions", ["request_id"])
    op.create_index("ix_approval_decisions_actor_id", "approval_decisions", ["actor_id"])
    op.create_index(
        "ix_approval_decisions_decided_at", "approval_decisions", [sa.text("decided_at DESC")]
    )

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    for table in ("alerts", "approval_requests", "approval_decisions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
            f"USING (tenant_id = {_CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
        )


def downgrade() -> None:
    """Drop the alert/approval RLS policies, tables, triggers, enums."""
    for table in ("approval_decisions", "approval_requests", "alerts"):
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.execute("DROP TRIGGER IF EXISTS trg_alerts_status_transition ON alerts")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_approval_requests_status_transition ON approval_requests"
    )
    op.execute("DROP FUNCTION IF EXISTS check_alert_status_transition()")
    op.execute("DROP FUNCTION IF EXISTS check_approval_request_status_transition()")

    op.drop_table("approval_decisions")
    op.drop_table("approval_requests")
    op.drop_table("alerts")
    # Single-use enum types created by op.create_table; drop explicitly so
    # a downgrade fully reverses the upgrade (005/009/011 pattern).
    op.execute("DROP TYPE alert_severity")
    op.execute("DROP TYPE alert_status")
    op.execute("DROP TYPE approval_status")
    op.execute("DROP TYPE approval_decision")
