"""SQLAlchemy ORM model for the operational event storage layer (Task 6.6).

Persists the Task 4 `EventEnvelope` (contracts/events/envelope.py) as the
authoritative source of truth. Event transport (Redis Streams/MQTT) is
NOT persistence — this table is. No competing event structures are
invented: DetectionObservation/TrackObservation travel inside the
envelope `payload` JSONB (genuinely variable payload data).

Event time is explicit and never substituted by created_at (governance
Section 6):

    event_time      — when the real-world event occurred (envelope)
    produced_at     — when the producer created the envelope
    ingestion_time  — when HotelOps received it (server now(); the
                      row-creation timestamp for this append-only log)
    processing_time — when processing occurred (nullable, set later)

The table is a TimescaleDB hypertable partitioned on `event_time`
(governance Section 11 — the primary hypertable candidate). No retention
or compression policies are configured here: retention is
client-configurable per the privacy baseline. No ORM relationships are
declared — this is an append-only, query-by-range event log (the schema
is the deliverable).
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
    PrimaryKeyConstraint,
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


class OperationalEventModel(Base):
    __tablename__ = "operational_events"

    __table_args__ = (
        # TimescaleDB requires the partitioning column inside any unique
        # constraint — the canonical composite PK (event_time, event_id).
        PrimaryKeyConstraint("event_time", "event_id"),
        CheckConstraint(
            "length(btrim(event_type)) > 0",
            name="ck_operational_events_event_type_not_empty",
        ),
        CheckConstraint(
            "length(btrim(source)) > 0",
            name="ck_operational_events_source_not_empty",
        ),
        CheckConstraint(
            "length(btrim(schema_version)) > 0",
            name="ck_operational_events_schema_version_not_empty",
        ),
        # Event time semantics: ingestion/processing can never precede the
        # real-world event — timestamps are distinct, never collapsed.
        CheckConstraint(
            "ingestion_time >= event_time",
            name="ck_operational_events_ingestion_not_before_event",
        ),
        CheckConstraint(
            "processing_time IS NULL OR processing_time >= event_time",
            name="ck_operational_events_processing_not_before_event",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_operational_events_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_operational_events_camera_tenant",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_operational_events_session_tenant",
        ),
        # Query patterns (governance Section 9): tenant-scoped time ranges,
        # type-filtered time ranges, venue and session correlation lookups.
        # NOTE (Task 6.13 review): no single-column index on event_time —
        # global time-range lookups are served by the hypertable PK
        # (event_time, event_id), whose leftmost column is event_time (and
        # the partition column). The PK is the partitioning index.
        Index("ix_operational_events_tenant_time", "tenant_id", text("event_time DESC")),
        Index("ix_operational_events_type_time", "event_type", text("event_time DESC")),
        Index("ix_operational_events_venue_id", "venue_id"),
        Index("ix_operational_events_session_id", "session_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid.uuid4,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SCHEMA_VERSION,
        server_default=SCHEMA_VERSION,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    produced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    processing_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    # Genuinely variable envelope payload (generic PayloadT) only.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<OperationalEventModel({self.event_id}) {self.event_type!r} "
            f"event_time={self.event_time.isoformat() if self.event_time else None}>"
        )
