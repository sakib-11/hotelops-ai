"""SQLAlchemy ORM models for the analytics persistence layer (Task 6.8).

Separates three layers (governance Section 3.6) — never conflated:

  operational_events — raw operational events (Task 6.6, hypertable)
  metrics           — DERIVED metrics (this module, hypertable)
  opportunities     — BUSINESS opportunities (this module, relational)

Persists the Task 4 contracts (contracts/analytics/models.py):

  MetricModel            — the MetricValue contract: metric identity
                           (metric_name + metric_id), value, unit,
                           sample/effective time (event_time, partition
                           column), aggregation window, tenant, venue,
                           aggregation dimensions (session/camera).
  OpportunityModel       — the OpportunityCandidate contract: relational,
                           low-volume candidate records.
  opportunity_metrics    — M2M: metric samples supporting an opportunity
                           (FK via the hypertable PK pair).
  opportunity_evidence_refs — M2M: evidence supporting an opportunity.

The metrics table is a TimescaleDB hypertable partitioned on event_time.
No continuous aggregates are created (no premature optimization — the
raw hypertable is the source of truth). No ORM relationships are
declared — this is a query-by-range store (the schema is the
deliverable), consistent with the event model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MetricModel(Base):
    __tablename__ = "metrics"

    __table_args__ = (
        # TimescaleDB requires the partitioning column inside any unique
        # constraint — the canonical composite PK (event_time, metric_id).
        PrimaryKeyConstraint("event_time", "metric_id"),
        CheckConstraint(
            "length(btrim(metric_name)) > 0",
            name="ck_metrics_metric_name_not_empty",
        ),
        CheckConstraint(
            "unit IS NULL OR length(btrim(unit)) > 0",
            name="ck_metrics_unit_not_empty",
        ),
        # Aggregation window: both columns or neither, ordered. The NULL
        # cases are spelled out — "X OR (window_end >= window_start)" would
        # evaluate to NULL (which CHECKs pass) for a half-populated pair.
        CheckConstraint(
            "(window_start IS NULL AND window_end IS NULL) "
            "OR (window_start IS NOT NULL AND window_end IS NOT NULL "
            "AND window_end >= window_start)",
            name="ck_metrics_window_ordered",
        ),
        # Samples are derived server-side — never future-dated.
        CheckConstraint(
            "ingestion_time >= event_time",
            name="ck_metrics_ingestion_not_before_event",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_metrics_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_metrics_session_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_metrics_camera_tenant",
        ),
        # Query patterns: tenant/venue-scoped time ranges, per-metric
        # trends, session/camera aggregation.
        Index("ix_metrics_tenant_time", "tenant_id", text("event_time DESC")),
        Index("ix_metrics_venue_time", "venue_id", text("event_time DESC")),
        Index("ix_metrics_name_time", "metric_name", text("event_time DESC")),
        Index("ix_metrics_session_id", "session_id"),
        Index("ix_metrics_camera_id", "camera_id"),
    )

    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid.uuid4,
    )
    # Business metric identity — what was measured.
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # The measured/computed value and its unit (typed, never embedded).
    value: Mapped[float] = mapped_column(Double, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Sample/effective time — the hypertable partition column.
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Optional aggregation window (both-or-neither CHECK).
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Forward reference to the analysis-jobs domain (no table yet) — it
    # becomes a real FK when the analysis-jobs schema lands.
    source_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Aggregation dimensions (per-session / per-camera metrics).
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Genuinely variable metric context only (JSONB policy).
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )
    ingestion_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<MetricModel({self.metric_id}) {self.metric_name!r} "
            f"value={self.value} @ {self.event_time.isoformat() if self.event_time else None}>"
        )


class OpportunityModel(Base):
    __tablename__ = "opportunities"

    __table_args__ = (
        # Composite FK target for the M2M link tables.
        UniqueConstraint("opportunity_id", "tenant_id", name="uq_opportunities_opportunity_tenant"),
        CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_opportunities_description_not_empty",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_opportunities_venue_tenant",
        ),
        Index("ix_opportunities_tenant_id", "tenant_id"),
        Index("ix_opportunities_venue_id", "venue_id"),
        Index("ix_opportunities_event_time", text("event_time DESC")),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
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
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityModel({self.opportunity_id}) "
            f"venue={self.venue_id} {self.description[:40]!r}>"
        )


# =============================================================================
# Opportunity <-> Metric samples (M2M via the hypertable PK pair)
# =============================================================================


opportunity_metrics = Table(
    "opportunity_metrics",
    Base.metadata,
    Column("opportunity_id", UUID(as_uuid=True), primary_key=True),
    Column("event_time", DateTime(timezone=True), primary_key=True),
    Column("metric_id", UUID(as_uuid=True), primary_key=True),
    # Denormalized tenant (FK-derived) so links are RLS-scoped and the
    # composite FK rejects cross-tenant links.
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    ForeignKeyConstraint(
        ["opportunity_id", "tenant_id"],
        ["opportunities.opportunity_id", "opportunities.tenant_id"],
        ondelete="CASCADE",
        name="fk_opportunity_metrics_opportunity_tenant",
    ),
    # The metrics hypertable PK — the only FK target on a hypertable.
    # (A unique (metric_id, tenant_id) on metrics is impossible: the
    # partition column must sit inside every unique constraint.)
    ForeignKeyConstraint(
        ["event_time", "metric_id"],
        ["metrics.event_time", "metrics.metric_id"],
        ondelete="CASCADE",
        name="fk_opportunity_metrics_metric",
    ),
    # Metric-first lookups (which opportunities cite this sample?).
    Index("ix_opportunity_metrics_metric_id", "metric_id"),
    PrimaryKeyConstraint("opportunity_id", "event_time", "metric_id"),
)


# =============================================================================
# Opportunity <-> Evidence (M2M)
# =============================================================================


opportunity_evidence_refs = Table(
    "opportunity_evidence_refs",
    Base.metadata,
    Column("opportunity_id", UUID(as_uuid=True), primary_key=True),
    Column("ref_id", UUID(as_uuid=True), primary_key=True),
    # Denormalized tenant (FK-derived) so links are RLS-scoped.
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    ForeignKeyConstraint(
        ["opportunity_id", "tenant_id"],
        ["opportunities.opportunity_id", "opportunities.tenant_id"],
        ondelete="CASCADE",
        name="fk_opportunity_evidence_refs_opportunity_tenant",
    ),
    ForeignKeyConstraint(
        ["ref_id", "tenant_id"],
        ["evidence_refs.ref_id", "evidence_refs.tenant_id"],
        ondelete="CASCADE",
        name="fk_opportunity_evidence_refs_ref_tenant",
    ),
    # Evidence-first lookups (which opportunities cite this evidence?).
    Index("ix_opportunity_evidence_refs_ref_id", "ref_id"),
    PrimaryKeyConstraint("opportunity_id", "ref_id"),
)
