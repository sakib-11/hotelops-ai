"""SQLAlchemy ORM models for the alert & approval persistence layer (Task 6.10).

Persists the Task 4 operational contracts (contracts/operations/
models.py) — `Alert`, `ApprovalRequest`, `ApprovalStatus` — with
EXPLICIT state transitions and no uncontrolled boolean combinations:

  AlertModel          — the Alert contract: alert identity, alert_type,
                        severity, title, description, event_time,
                        polymorphic source_ref (finding / recommendation)
                        — plus direct tenant/venue ownership, an explicit
                        `alert_status` enum lifecycle, and timestamps.
  ApprovalRequestModel — the ApprovalRequest contract: request identity,
                        recommendation subject, requested_by actor,
                        explicit `approval_status` enum, requested_at /
                        resolved_at / reason.
  ApprovalDecisionModel — APPEND-ONLY decision history: actor, decision,
                        reason, decided_at. One terminal decision per
                        request (partial unique index — duplicate guard).

Transition LEGALITY is enforced by database triggers (migration 012) —
a CHECK cannot compare OLD/NEW row values. No ORM relationships are
declared — this is a review/dashboard store (the schema is the
deliverable), consistent with the other domain models.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION

# Enum values — mirror contracts/operations/models.py (Severity,
# ApprovalStatus) plus DB-level lifecycle states.
_ALERT_SEVERITIES = ("critical", "high", "medium", "low", "info")
_ALERT_STATUSES = ("raised", "acknowledged", "resolved", "expired")
_APPROVAL_STATUSES = ("pending", "approved", "rejected", "cancelled")
_APPROVAL_DECISIONS = ("approved", "rejected", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AlertModel(Base):
    __tablename__ = "alerts"

    __table_args__ = (
        CheckConstraint(
            "length(btrim(alert_type)) > 0",
            name="ck_alerts_alert_type_not_empty",
        ),
        CheckConstraint("length(btrim(title)) > 0", name="ck_alerts_title_not_empty"),
        CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_alerts_description_not_empty",
        ),
        # At most one source ref — never both a finding and a recommendation.
        CheckConstraint(
            "finding_id IS NULL OR recommendation_id IS NULL",
            name="ck_alerts_source_single",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_alerts_updated_not_before_created",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_alerts_venue_tenant",
        ),
        ForeignKeyConstraint(
            ["finding_id", "tenant_id"],
            ["findings.finding_id", "findings.tenant_id"],
            ondelete="CASCADE",
            name="fk_alerts_finding_tenant",
        ),
        ForeignKeyConstraint(
            ["recommendation_id", "tenant_id"],
            ["recommendations.recommendation_id", "recommendations.tenant_id"],
            ondelete="CASCADE",
            name="fk_alerts_recommendation_tenant",
        ),
        # Query patterns: alert-center dashboard (status filter), time-range
        # review, source-provenance lookups.
        Index("ix_alerts_tenant_id", "tenant_id"),
        Index("ix_alerts_venue_id", "venue_id"),
        Index("ix_alerts_status", "status"),
        Index("ix_alerts_event_time", text("event_time DESC")),
        Index("ix_alerts_finding_id", "finding_id"),
        Index("ix_alerts_recommendation_id", "recommendation_id"),
    )

    alert_id: Mapped[uuid.UUID] = mapped_column(
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
    alert_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(
        Enum(*_ALERT_SEVERITIES, name="alert_severity"),
        nullable=False,
        default="info",
        server_default="info",
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(4096), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Polymorphic source_ref (FindingId | RecommendationId | None) as two
    # real composite FKs — at most one set (CHECK).
    finding_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Explicit lifecycle state — never boolean flags.
    status: Mapped[str] = mapped_column(
        Enum(*_ALERT_STATUSES, name="alert_status"),
        nullable=False,
        default="raised",
        server_default="raised",
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    # Last status transition (set by the transition trigger).
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AlertModel({self.alert_id}) {self.alert_type!r} "
            f"severity={self.severity!r} status={self.status!r}>"
        )


class ApprovalRequestModel(Base):
    __tablename__ = "approval_requests"

    __table_args__ = (
        # Composite FK target for approval_decisions.
        UniqueConstraint("request_id", "tenant_id", name="uq_approval_requests_request_tenant"),
        CheckConstraint(
            "reason IS NULL OR length(btrim(reason)) > 0",
            name="ck_approval_requests_reason_not_empty",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= requested_at",
            name="ck_approval_requests_resolved_after_requested",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_approval_requests_updated_not_before_created",
        ),
        ForeignKeyConstraint(
            ["recommendation_id", "tenant_id"],
            ["recommendations.recommendation_id", "recommendations.tenant_id"],
            ondelete="CASCADE",
            name="fk_approval_requests_recommendation_tenant",
        ),
        ForeignKeyConstraint(
            ["requested_by"],
            ["users.user_id"],
            ondelete="RESTRICT",
            name="fk_approval_requests_requested_by",
        ),
        Index("ix_approval_requests_tenant_id", "tenant_id"),
        Index("ix_approval_requests_status", "status"),
        Index("ix_approval_requests_recommendation_id", "recommendation_id"),
        Index("ix_approval_requests_requested_at", text("requested_at DESC")),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
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
    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_APPROVAL_STATUSES, name="approval_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ApprovalRequestModel({self.request_id}) status={self.status!r} "
            f"recommendation={self.recommendation_id}>"
        )


class ApprovalDecisionModel(Base):
    __tablename__ = "approval_decisions"

    __table_args__ = (
        # Duplicate-approval guard: at most one terminal decision per
        # request (partial unique index — mirrors migration 012).
        Index(
            "uq_approval_decisions_terminal",
            "request_id",
            unique=True,
            postgresql_where=text("decision IN ('approved', 'rejected', 'cancelled')"),
        ),
        CheckConstraint(
            "reason IS NULL OR length(btrim(reason)) > 0",
            name="ck_approval_decisions_reason_not_empty",
        ),
        ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            ["approval_requests.request_id", "approval_requests.tenant_id"],
            ondelete="CASCADE",
            name="fk_approval_decisions_request_tenant",
        ),
        ForeignKeyConstraint(
            ["actor_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
            name="fk_approval_decisions_actor",
        ),
        Index("ix_approval_decisions_request_id", "request_id"),
        Index("ix_approval_decisions_actor_id", "actor_id"),
        Index("ix_approval_decisions_decided_at", text("decided_at DESC")),
    )

    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(
        Enum(*_APPROVAL_DECISIONS, name="approval_decision"),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<ApprovalDecisionModel({self.decision_id}) request={self.request_id} "
            f"decision={self.decision!r}>"
        )
