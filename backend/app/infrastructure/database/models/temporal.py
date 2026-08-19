"""SQLAlchemy ORM model for the canonical temporal-fact persistence layer
(Task 18.10).

Persists the Task 15 canonical business facts (e.g. the confirmed
``OccupancySnapshot``) as the authoritative input to the Task 16 rule
engine — the fact that DRIVES a domain event. The vertical-slice
persistence boundary commits fact + event + audit + outbox ATOMICALLY:

    temporal_facts      — canonical business fact (this module)
    operational_events  — domain event (Task 6.6)
    audit_events        — audit identity/context (Task 6.12)
    outbox_events       — Task 7 transactional outbox

PostgreSQL is the source of truth: the fact and the event it produced
can never diverge — they commit or roll back together (the outbox is
only transported AFTER the commit, never published to Redis first).

The fact's scope (tenant/venue/session/camera/configuration version) is
typed columns (governance Section 7); the genuinely variable fact
payload (the canonical Task 15 fact contract) is JSONB. ``fact_id`` is
the canonical, deterministic fact identity (content-derived per Task 7
idempotency) — replaying the same timeline reproduces the same fact row.
No ORM relationships are declared — this is an append-only fact log
(the schema is the deliverable).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TemporalFactModel(Base):
    __tablename__ = "temporal_facts"

    __table_args__ = (
        CheckConstraint(
            "length(btrim(fact_type)) > 0",
            name="ck_temporal_facts_fact_type_not_empty",
        ),
        CheckConstraint(
            "length(btrim(fsm_kind)) > 0",
            name="ck_temporal_facts_fsm_kind_not_empty",
        ),
        CheckConstraint(
            "length(btrim(schema_version)) > 0",
            name="ck_temporal_facts_schema_version_not_empty",
        ),
        # Timestamp semantics: ingestion can never precede the fact's own
        # instant (distinct, never collapsed).
        CheckConstraint(
            "created_at >= event_time",
            name="ck_temporal_facts_ingestion_not_before_event",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_temporal_facts_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_temporal_facts_camera_tenant",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_temporal_facts_session_tenant",
        ),
        # Query patterns (governance Section 9): tenant-scoped time
        # ranges, type-filtered time ranges, correlation lookups.
        Index("ix_temporal_facts_event_time", "event_time"),
        Index("ix_temporal_facts_tenant_time", "tenant_id", text("event_time DESC")),
        Index("ix_temporal_facts_type_time", "fact_type", text("event_time DESC")),
        Index("ix_temporal_facts_venue_id", "venue_id"),
        Index("ix_temporal_facts_session_id", "session_id"),
        Index("ix_temporal_facts_camera_id", "camera_id"),
        Index("ix_temporal_facts_config_version_id", "configuration_version_id"),
    )

    fact_id: Mapped[uuid.UUID] = mapped_column(
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
    # The controlled vocabulary of canonical facts ("occupancy_snapshot" …).
    fact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # The Task 15 FSM family that produced the fact ("occupancy", …).
    fsm_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # The fact's own instant — the hypertable partition candidate and the
    # temporal boundary the fact describes (never processing time).
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Provenance hop to the temporal transition that produced this fact.
    source_transition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    fsm_version: Mapped[str] = mapped_column(String(50), nullable=False)
    policy_revision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # The canonical Task 15 fact payload (the generic fact contract).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<TemporalFactModel({self.fact_id}) {self.fact_type!r} "
            f"@{self.event_time.isoformat() if self.event_time else None}>"
        )
