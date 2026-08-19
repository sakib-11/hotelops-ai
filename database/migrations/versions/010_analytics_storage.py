"""Create the analytics persistence schema (Task 6.8).

Separates three layers (governance Section 3.6) — never conflated:

  operational_events — raw operational events (migration 008, hypertable)
  metrics           — DERIVED metrics (this migration, hypertable)
  opportunities     — BUSINESS opportunities (this migration, relational)

Persists the Task 4 contracts (contracts/analytics/models.py) without
inventing arbitrary analytics tables:

  metrics                — one row per derived metric sample: metric
                           identity (metric_name + metric_id), value,
                           unit, sample/effective time (event_time — the
                           partition column), aggregation window, tenant,
                           venue, aggregation dimensions (session/camera).
                           This is the MetricValue contract.
  opportunities          — relational opportunity records (low volume),
                           the OpportunityCandidate contract.
  opportunity_metrics    — M2M: which metric samples support an
                           opportunity (FK via the hypertable PK pair).
  opportunity_evidence_refs — M2M: which evidence supports an opportunity.

TimescaleDB policy (Section 11): `metrics` is the approved hypertable
candidate (high volume, time-range dashboards) and is created as a
hypertable partitioned on event_time. Continuous aggregates are NOT
created — the task directs no premature optimization: the raw hypertable
is the source of truth, and rollup views wait for actual dashboard query
patterns (governance 11.4 rule 5). No retention/compression policies
(client-configurable per the privacy baseline).

Design decisions (each maps to a governance policy):

  - Metric identity is explicit: `metric_name` (the business identity,
    CHECK non-empty) + `metric_id` (row UUID). Value is typed
    DOUBLE PRECISION; `unit` is typed, not embedded in the value.
  - Event/effective time is explicit (`event_time`, timestamptz,
    partition column); `ingestion_time` (server now()) records receipt —
    distinct timestamps, never collapsed (Section 6). Aggregated metrics
    carry an optional `window_start`/`window_end` pair (both-or-neither
    CHECK, window_end >= window_start).
  - Tenant ownership is DIRECT: `tenant_id NOT NULL` + composite FKs
    (venue/session/camera, tenant_id) — the established pattern.
    Cross-tenant references rejected by composite FKs and RLS.
  - `source_ref` (AnalysisJobId) is a nullable bare-UUID forward
    reference — the analysis-jobs table does not exist yet; it becomes a
    real FK when that schema lands (documented exception, governance 8).
  - opportunities link their supporting metric samples via
    (event_time, metric_id) — the metrics hypertable PK — because a
    hypertable cannot carry a unique (metric_id, tenant_id) (the
    partition column must be inside every unique constraint). Tenant
    correctness on links is enforced by RLS + repository scoping (the
    same documented limitation as evidence_refs.event_id, migration 009).
  - RLS + grants ship in the same migration (Section 10.4 rule 5).

Revision ID: 010_analytics_storage
Revises: 009_evidence_persistence
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "010_analytics_storage"
down_revision: str | None = "009_evidence_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON metrics TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON opportunities TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON opportunity_metrics TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON opportunity_evidence_refs TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON metrics FROM hotelops_app;",
    "REVOKE ALL ON opportunities FROM hotelops_app;",
    "REVOKE ALL ON opportunity_metrics FROM hotelops_app;",
    "REVOKE ALL ON opportunity_evidence_refs FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)


def upgrade() -> None:
    """Create the analytics tables, then enable RLS on them."""

    # TimescaleDB extension — idempotent; required for hypertable creation.
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # --- METRICS (derived metric samples; high-volume time-series) ---
    op.create_table(
        "metrics",
        sa.Column("metric_id", sa.UUID(), nullable=False),
        # Business metric identity — what was measured.
        sa.Column("metric_name", sa.String(100), nullable=False),
        # The measured/computed value and its unit (typed, never embedded).
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("unit", sa.String(50), nullable=True),
        # Sample/effective time — the hypertable partition column.
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        # Optional aggregation window (both-or-neither CHECK below).
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        # Forward reference to the analysis-jobs domain (no table yet).
        sa.Column("source_ref", sa.UUID(), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        # Aggregation dimensions (per-session / per-camera metrics).
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        # Genuinely variable metric context only (JSONB policy).
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "ingestion_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # TimescaleDB requires the partitioning column inside any unique
        # constraint — the canonical composite PK (event_time, metric_id).
        sa.PrimaryKeyConstraint("event_time", "metric_id"),
        sa.CheckConstraint(
            "length(btrim(metric_name)) > 0",
            name="ck_metrics_metric_name_not_empty",
        ),
        sa.CheckConstraint(
            "unit IS NULL OR length(btrim(unit)) > 0",
            name="ck_metrics_unit_not_empty",
        ),
        # Aggregation window: both columns or neither, ordered. The NULL
        # cases are spelled out — "X OR (window_end >= window_start)" would
        # evaluate to NULL (which CHECKs pass) for a half-populated pair.
        sa.CheckConstraint(
            "(window_start IS NULL AND window_end IS NULL) "
            "OR (window_start IS NOT NULL AND window_end IS NOT NULL "
            "AND window_end >= window_start)",
            name="ck_metrics_window_ordered",
        ),
        # Samples are derived server-side — never future-dated.
        sa.CheckConstraint(
            "ingestion_time >= event_time",
            name="ck_metrics_ingestion_not_before_event",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_metrics_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_metrics_session_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_metrics_camera_tenant",
        ),
    )
    # Query patterns (governance Section 9): tenant/venue-scoped time-range
    # dashboards, per-metric trends, session/camera aggregation.
    op.create_index("ix_metrics_tenant_time", "metrics", ["tenant_id", sa.text("event_time DESC")])
    op.create_index("ix_metrics_venue_time", "metrics", ["venue_id", sa.text("event_time DESC")])
    op.create_index("ix_metrics_name_time", "metrics", ["metric_name", sa.text("event_time DESC")])
    op.create_index("ix_metrics_session_id", "metrics", ["session_id"])
    op.create_index("ix_metrics_camera_id", "metrics", ["camera_id"])

    # Convert to a hypertable partitioned on event_time. create_default_indexes
    # is disabled because ix_metrics_tenant_time already covers the time
    # dimension (avoids a TimescaleDB-named index invisible to the ORM).
    op.execute(
        "SELECT create_hypertable('metrics', 'event_time', "
        "create_default_indexes => FALSE, if_not_exists => TRUE);"
    )

    # --- OPPORTUNITIES (relational records — low volume, NOT a hypertable) ---
    op.create_table(
        "opportunities",
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("opportunity_id"),
        # Composite FK target for the M2M link tables.
        sa.UniqueConstraint(
            "opportunity_id", "tenant_id", name="uq_opportunities_opportunity_tenant"
        ),
        sa.CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_opportunities_description_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_opportunities_venue_tenant",
        ),
    )
    op.create_index("ix_opportunities_tenant_id", "opportunities", ["tenant_id"])
    op.create_index("ix_opportunities_venue_id", "opportunities", ["venue_id"])
    op.create_index("ix_opportunities_event_time", "opportunities", [sa.text("event_time DESC")])

    # --- OPPORTUNITY <-> METRIC SAMPLES (M2M via the hypertable PK pair) ---
    op.create_table(
        "opportunity_metrics",
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_id", sa.UUID(), nullable=False),
        # Denormalized tenant (FK-derived) so links are RLS-scoped.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("opportunity_id", "event_time", "metric_id"),
        sa.ForeignKeyConstraint(
            ["opportunity_id", "tenant_id"],
            ["opportunities.opportunity_id", "opportunities.tenant_id"],
            ondelete="CASCADE",
            name="fk_opportunity_metrics_opportunity_tenant",
        ),
        # The metrics hypertable PK — the only FK target on a hypertable.
        sa.ForeignKeyConstraint(
            ["event_time", "metric_id"],
            ["metrics.event_time", "metrics.metric_id"],
            ondelete="CASCADE",
            name="fk_opportunity_metrics_metric",
        ),
    )
    # Metric-first lookups (which opportunities cite this sample?).
    op.create_index("ix_opportunity_metrics_metric_id", "opportunity_metrics", ["metric_id"])

    # --- OPPORTUNITY <-> EVIDENCE (M2M) ---
    op.create_table(
        "opportunity_evidence_refs",
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("ref_id", sa.UUID(), nullable=False),
        # Denormalized tenant (FK-derived) so links are RLS-scoped.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("opportunity_id", "ref_id"),
        sa.ForeignKeyConstraint(
            ["opportunity_id", "tenant_id"],
            ["opportunities.opportunity_id", "opportunities.tenant_id"],
            ondelete="CASCADE",
            name="fk_opportunity_evidence_refs_opportunity_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["ref_id", "tenant_id"],
            ["evidence_refs.ref_id", "evidence_refs.tenant_id"],
            ondelete="CASCADE",
            name="fk_opportunity_evidence_refs_ref_tenant",
        ),
    )
    # Evidence-first lookups (which opportunities cite this evidence?).
    op.create_index("ix_opportunity_evidence_refs_ref_id", "opportunity_evidence_refs", ["ref_id"])

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    for table in ("metrics", "opportunities", "opportunity_metrics", "opportunity_evidence_refs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
            f"USING (tenant_id = {_CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
        )


def downgrade() -> None:
    """Drop the analytics RLS policies, tables, and the hypertable."""
    for table in ("opportunity_evidence_refs", "opportunity_metrics", "opportunities", "metrics"):
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.drop_table("opportunity_evidence_refs")
    op.drop_table("opportunity_metrics")
    op.drop_table("opportunities")
    # Dropping the hypertable removes its chunk tables.
    op.drop_table("metrics")
