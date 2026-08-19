"""Create the canonical temporal-fact persistence layer (Task 18.10).

The authoritative vertical-slice persistence boundary persists, in ONE
business transaction:

    1. canonical business fact  → temporal_facts (this migration)
    2. domain event             → operational_events (Task 6.6)
    3. audit identity/context   → audit_events (Task 6.12)
    4. outbox message           → outbox_events (Task 7)

PostgreSQL is the source of truth: the fact + event + audit + outbox
rows commit ATOMICALLY or not at all, and nothing is published to Redis
before the database commit (the outbox publisher transports AFTER
commit). This table is the durable home of the Task 15 canonical facts
(confirmed ``OccupancySnapshot`` etc.) that DRIVE the Task 16 domain
events — the event can never diverge from the fact that produced it.

  temporal_facts — append-only canonical fact log

The fact's scope (tenant/venue/session/camera/configuration version)
becomes typed columns (governance Section 7 — queried/filtered/joined
values must be typed); the fact's genuinely variable payload (the
canonical Task 15 fact contract, e.g. OccupancySnapshot) is JSONB.
``fact_type`` is the controlled vocabulary of canonical facts
(``occupancy_snapshot`` …). ``source_transition_id`` is the provenance
hop to the temporal transition that produced the fact (§17 — no
unexplained fact).

Tenancy is DIRECT (``tenant_id`` NOT NULL) with composite FKs
(venue/session/camera + tenant_id) — the established pattern. RLS +
grants ship in the same migration (governance Section 10.4 rule 5).

NOT a hypertable: the fact identity (``fact_id``) is the referenceable
primary key and the volume does not yet justify time partitioning —
``event_time`` carries a plain index for time-range scans.

Revision ID: 019_temporal_fact_persistence
Revises: 018_configuration_domain_schema
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "019_temporal_fact_persistence"
down_revision: str | None = "018_configuration_domain_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON temporal_facts TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON temporal_facts FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)


def upgrade() -> None:
    """Create the temporal_facts table, then enable RLS on it."""

    op.create_table(
        "temporal_facts",
        sa.Column("fact_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("fact_type", sa.String(100), nullable=False),
        sa.Column("fsm_kind", sa.String(50), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        # The provenance hop to the temporal transition that produced
        # this fact (§17 — no unexplained facts).
        sa.Column("source_transition_id", sa.UUID(), nullable=True),
        sa.Column("fsm_version", sa.String(50), nullable=False),
        sa.Column("policy_revision", sa.String(50), nullable=True),
        # The canonical Task 15 fact payload (generic fact contract).
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.CheckConstraint(
            "length(btrim(fact_type)) > 0",
            name="ck_temporal_facts_fact_type_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(fsm_kind)) > 0",
            name="ck_temporal_facts_fsm_kind_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(schema_version)) > 0",
            name="ck_temporal_facts_schema_version_not_empty",
        ),
        # Timestamp semantics: the fact's own instant can never be after
        # its ingestion (facts are recorded when they happen, never in
        # the future).
        sa.CheckConstraint(
            "created_at >= event_time",
            name="ck_temporal_facts_ingestion_not_before_event",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_temporal_facts_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_temporal_facts_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_temporal_facts_session_tenant",
        ),
    )
    # Query patterns (governance Section 9): tenant-scoped time ranges,
    # type-filtered time ranges, venue/session/camera/config correlation.
    op.create_index(
        "ix_temporal_facts_event_time",
        "temporal_facts",
        ["event_time"],
    )
    op.create_index(
        "ix_temporal_facts_tenant_time",
        "temporal_facts",
        ["tenant_id", sa.text("event_time DESC")],
    )
    op.create_index(
        "ix_temporal_facts_type_time",
        "temporal_facts",
        ["fact_type", sa.text("event_time DESC")],
    )
    op.create_index("ix_temporal_facts_venue_id", "temporal_facts", ["venue_id"])
    op.create_index("ix_temporal_facts_session_id", "temporal_facts", ["session_id"])
    op.create_index("ix_temporal_facts_camera_id", "temporal_facts", ["camera_id"])
    op.create_index(
        "ix_temporal_facts_config_version_id",
        "temporal_facts",
        ["configuration_version_id"],
    )

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)
    op.execute("ALTER TABLE temporal_facts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE temporal_facts FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY temporal_facts_all ON temporal_facts FOR ALL "
        "TO hotelops_app "
        f"USING (tenant_id = {_CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
    )


def downgrade() -> None:
    """Drop the temporal-fact RLS policy and table."""
    op.execute("DROP POLICY IF EXISTS temporal_facts_all ON temporal_facts;")
    op.execute("ALTER TABLE temporal_facts DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE temporal_facts NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.drop_table("temporal_facts")
