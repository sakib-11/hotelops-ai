"""Create the operational event storage layer as a TimescaleDB hypertable (Task 6.6).

Persists the Task 4 `EventEnvelope` (contracts/events/envelope.py) as the
authoritative source of truth — event transport (Redis Streams/MQTT) is
NOT persistence. No competing event structures are invented:
DetectionObservation/TrackObservation travel inside the envelope payload.

  operational_events — append-only operational event stream

Envelope metadata becomes typed columns (queried/filtered/joined values
must be typed per governance Section 7):

    event_id, event_type, schema_version, event_time, produced_at,
    source, correlation_id, causation_id

Event time is explicit and never substituted by created_at (Section 6):

    event_time      — when the real-world event occurred (envelope)
    produced_at     — when the producer created the envelope
    ingestion_time  — when HotelOps received it (server now(), the
                      row-creation timestamp for this append-only log)
    processing_time — when processing occurred (nullable, set later)

The envelope `payload` (a generic PayloadT) is the genuinely variable
data — stored as JSONB. Detection/track observations persist inside it.

TimescaleDB policy (Section 11):

    - `operational_events` is the primary hypertable candidate and is
      created as a hypertable partitioned on `event_time`.
    - NO retention/compression policies are configured — retention is
      client-configurable per the privacy baseline (Section 11.4 rule 2).
    - Continuous aggregates for dashboards remain future work; the raw
      hypertable is the source of truth.
    - Clock-skew note: the CHECK ingestion_time >= event_time means a
      future-dated event_time (camera clock ahead of the server) is
      rejected. NTP time sync on cameras is an operational prerequisite
      (camera time is corrected to UTC before it reaches this table).
    - tenancy is DIRECT (`tenant_id` NOT NULL) with composite FKs
      (venue/camera/session_id, tenant_id) — the established pattern.
      A new unique target `uq_video_sessions_session_tenant` is added to
      video_sessions so events can FK sessions without cross-tenant
      references.
    - RLS + grants ship in the same migration (Section 10.4 rule 5).

Revision ID: 008_operational_events
Revises: 007_operational_config_schema
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "008_operational_events"
down_revision: str | None = "007_operational_config_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON operational_events TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON operational_events FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)


def upgrade() -> None:
    """Create the operational_events hypertable, then enable RLS on it."""

    # TimescaleDB extension — idempotent; required for hypertable creation.
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # Composite FK target for operational_events.session_id: a session can
    # only be referenced by events of its own tenant.
    op.execute(
        "ALTER TABLE video_sessions ADD CONSTRAINT "
        "uq_video_sessions_session_tenant UNIQUE (session_id, tenant_id);"
    )

    op.create_table(
        "operational_events",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingestion_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processing_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        sa.Column("causation_id", sa.String(255), nullable=True),
        sa.Column("source", sa.String(255), nullable=False),
        # Genuinely variable envelope payload (generic PayloadT) only.
        sa.Column("payload", JSONB(), nullable=False),
        # TimescaleDB requires the partitioning column inside any unique
        # constraint — the canonical composite PK (event_time, event_id).
        sa.PrimaryKeyConstraint("event_time", "event_id"),
        sa.CheckConstraint(
            "length(btrim(event_type)) > 0",
            name="ck_operational_events_event_type_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0",
            name="ck_operational_events_source_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(schema_version)) > 0",
            name="ck_operational_events_schema_version_not_empty",
        ),
        # Event time semantics: ingestion/processing can never precede the
        # real-world event (Section 6 — timestamps are distinct, not collapsed).
        sa.CheckConstraint(
            "ingestion_time >= event_time",
            name="ck_operational_events_ingestion_not_before_event",
        ),
        sa.CheckConstraint(
            "processing_time IS NULL OR processing_time >= event_time",
            name="ck_operational_events_processing_not_before_event",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_operational_events_venue_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_operational_events_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_operational_events_session_tenant",
        ),
    )
    # Query patterns (governance Section 9): time-range dashboards,
    # tenant-scoped time ranges, type-filtered time ranges, venue and
    # session correlation lookups.
    op.create_index(
        "ix_operational_events_event_time",
        "operational_events",
        ["event_time"],
    )
    op.create_index(
        "ix_operational_events_tenant_time",
        "operational_events",
        ["tenant_id", sa.text("event_time DESC")],
    )
    op.create_index(
        "ix_operational_events_type_time",
        "operational_events",
        ["event_type", sa.text("event_time DESC")],
    )
    op.create_index("ix_operational_events_venue_id", "operational_events", ["venue_id"])
    op.create_index(
        "ix_operational_events_session_id",
        "operational_events",
        ["session_id"],
    )

    # Convert to a hypertable partitioned on event_time. create_default_indexes
    # is disabled because ix_operational_events_event_time already covers the
    # time dimension (avoids a TimescaleDB-named index invisible to the ORM).
    op.execute(
        "SELECT create_hypertable('operational_events', 'event_time', "
        "create_default_indexes => FALSE, if_not_exists => TRUE);"
    )

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)
    op.execute("ALTER TABLE operational_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE operational_events FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY operational_events_all ON operational_events FOR ALL "
        "TO hotelops_app "
        f"USING (tenant_id = {_CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
    )


def downgrade() -> None:
    """Drop the event RLS policy, hypertable, and supporting constraint."""
    op.execute("DROP POLICY IF EXISTS operational_events_all ON operational_events;")
    op.execute("ALTER TABLE operational_events DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE operational_events NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    # Dropping the hypertable removes its chunk tables.
    op.drop_table("operational_events")
    op.execute(
        "ALTER TABLE video_sessions DROP CONSTRAINT IF EXISTS uq_video_sessions_session_tenant;"
    )
