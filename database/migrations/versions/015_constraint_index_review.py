"""Drop indexes proven redundant by the Task 6.13 constraint & index review.

Every Task 6 table was audited against the live PostgreSQL catalog
(pg_index left-prefix analysis). Each index was accepted only if it
serves a query pattern NOT already covered by an existing index whose
leftmost columns are a prefix of it. Three single-column indexes are
left-prefixes of composite indexes on the same table and add write
amplification with no query benefit — they are dropped here:

  camera_configs.ix_camera_configs_camera_id
      camera_id-only lookups are served by the unique constraint
      uq_camera_configs_version (camera_id, version) — camera_id is
      its leftmost column. (uq_camera_configs_active is a partial
      unique on camera_id WHERE status='active'; the version unique
      covers ALL rows, so the single-column index is fully redundant.)

  analysis_configs.ix_analysis_configs_venue_id
      venue_id-only lookups are served by the unique constraint
      uq_analysis_configs_version (venue_id, name, version) — venue_id
      is its leftmost column. The partial unique
      uq_analysis_configs_active (venue_id, name) WHERE status='active'
      also covers the hot active subset. The single-column index adds
      nothing.

  operational_events.ix_operational_events_event_time
      event_time-only (global time-range) lookups are served by the
      hypertable primary key (event_time, event_id) — event_time is
      its leftmost column and is the TimescaleDB partition column, so
      the PK is the partitioning index (create_default_indexes was
      already FALSE). The dedicated event_time index duplicates the
      PK's leftmost prefix on the highest-volume table; dropping it
      removes write amplification with no read regression. Tenant- and
      type-scoped time ranges keep their composite indexes
      (ix_operational_events_tenant_time / type_time).

REJECTED as redundant (left-prefix detector false positives):

  - *_pkey(id) vs uq_*_tenant(id, tenant_id): the composite uniques
    are FK TARGETS for composite foreign keys (migration 003 pattern:
    child tables reference (id, tenant_id)); the PK alone cannot serve
    as a composite FK target. Both are required, not redundant.
  - integrations.ix_integrations_tenant_id: the covering unique
    uq_integrations_active_provider is PARTIAL (WHERE status='active'),
    so it only indexes active rows; the full tenant index is required
    for pending/disabled/error rows. Not redundant.
  - memberships.ix_memberships_tenant_id: a genuine left-prefix
    redundancy with ix_memberships_tenant_user, but memberships is a
    Task 2/3 identity table (migration 001) — OUT OF SCOPE for this
    Task 6 review; documented in governance Section 9 for a future
    identity-schema cleanup.

Revision ID: 015_constraint_index_review
Revises: 014_audit_outbox_inbox
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015_constraint_index_review"
down_revision: str | None = "014_audit_outbox_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the three redundant indexes (left-prefixes of composites)."""

    op.drop_index("ix_camera_configs_camera_id", table_name="camera_configs")
    op.drop_index("ix_analysis_configs_venue_id", table_name="analysis_configs")
    # Dropping on the hypertable cascades to its chunk tables.
    op.drop_index("ix_operational_events_event_time", table_name="operational_events")


def downgrade() -> None:
    """Re-create the dropped indexes (downgrade restores the prior schema)."""

    op.create_index("ix_camera_configs_camera_id", "camera_configs", ["camera_id"])
    op.create_index("ix_analysis_configs_venue_id", "analysis_configs", ["venue_id"])
    op.create_index("ix_operational_events_event_time", "operational_events", ["event_time"])
