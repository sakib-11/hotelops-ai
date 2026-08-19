"""SQLAlchemy ORM models for the AI domain persistence layer (Task 6.9).

Persists the Task 4 intelligence contracts (contracts/intelligence/
models.py) as DERIVED data (ADR-002: deterministic core, LLM-last) —
never as operational truth:

  FindingModel       — the Finding contract: finding identity,
                       evidence_package_id source link, finding_type,
                       description, confidence, event_time — plus
                       review workflow state (status), tenant, venue,
                       model/provider metadata, version info.
  RecommendationModel — the Recommendation contract: recommendation
                       identity, optional opportunity link, description,
                       priority — plus review workflow state (status),
                       tenant, venue, model/provider metadata, version.
  recommendation_findings — M2M: which findings support a recommendation
                       (Recommendation.finding_ids).

AI outputs are derived records that reference their source context
(evidence packages, opportunities) via real composite FKs and can never
modify authoritative operational data — no write-back path exists.
Arbitrary LLM conversations are never stored as business truth. No ORM
relationships are declared — this is a review-store (the schema is the
deliverable), consistent with the event/analytics models.
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
    Enum,
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

# Enum values — mirror contracts/intelligence/models.py (Priority) plus
# DB-level review workflow states (config_status pattern, task 6.5).
_FINDING_STATUSES = ("proposed", "accepted", "rejected", "archived")
_RECOMMENDATION_STATUSES = ("pending", "accepted", "rejected", "implemented", "archived")
_PRIORITIES = ("high", "medium", "low")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FindingModel(Base):
    __tablename__ = "findings"

    __table_args__ = (
        # Composite FK target for recommendation_findings.
        UniqueConstraint("finding_id", "tenant_id", name="uq_findings_finding_tenant"),
        CheckConstraint(
            "length(btrim(finding_type)) > 0",
            name="ck_findings_finding_type_not_empty",
        ),
        CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_findings_description_not_empty",
        ),
        # Contract confidence is bounded [0, 1].
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_findings_confidence_range",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_findings_updated_not_before_created",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_findings_venue_tenant",
        ),
        # Evidence linkage — RESTRICT: evidence cited by a derived finding is
        # never silently destroyed. A composite FK with a denormalized
        # tenant_id cannot use SET NULL (it would null tenant_id, violating
        # NOT NULL) — RESTRICT is the correct orphan-prevention semantics;
        # retention tooling must unlink findings before purging packages.
        ForeignKeyConstraint(
            ["evidence_package_id", "tenant_id"],
            ["evidence_packages.package_id", "evidence_packages.tenant_id"],
            ondelete="RESTRICT",
            name="fk_findings_evidence_package_tenant",
        ),
        # Query patterns: tenant/venue review queues, status-filtered review
        # UI, evidence-first provenance lookups, time-range review.
        Index("ix_findings_tenant_id", "tenant_id"),
        Index("ix_findings_venue_id", "venue_id"),
        Index("ix_findings_status", "status"),
        Index("ix_findings_event_time", text("event_time DESC")),
        Index("ix_findings_evidence_package_id", "evidence_package_id"),
    )

    finding_id: Mapped[uuid.UUID] = mapped_column(
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
    finding_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(4096), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_FINDING_STATUSES, name="finding_status"),
        nullable=False,
        default="proposed",
        server_default="proposed",
    )  # Source/evidence linkage (nullable until the finding is grounded).
    evidence_package_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Model/provider metadata (derived-data provenance).
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Genuinely variable model context only (JSONB policy).
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    # Last status transition (mutations are legal — workflow state).
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<FindingModel({self.finding_id}) {self.finding_type!r} status={self.status!r}>"


class RecommendationModel(Base):
    __tablename__ = "recommendations"

    __table_args__ = (
        # Composite FK target for recommendation_findings.
        UniqueConstraint(
            "recommendation_id",
            "tenant_id",
            name="uq_recommendations_recommendation_tenant",
        ),
        CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_recommendations_description_not_empty",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_recommendations_updated_not_before_created",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_recommendations_venue_tenant",
        ),
        # Opportunity linkage — RESTRICT: a recommendation citing an
        # opportunity blocks its deletion (composite FK cannot SET NULL).
        ForeignKeyConstraint(
            ["opportunity_id", "tenant_id"],
            ["opportunities.opportunity_id", "opportunities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_recommendations_opportunity_tenant",
        ),
        Index("ix_recommendations_tenant_id", "tenant_id"),
        Index("ix_recommendations_venue_id", "venue_id"),
        Index("ix_recommendations_status", "status"),
        Index("ix_recommendations_created_at", text("created_at DESC")),
    )

    recommendation_id: Mapped[uuid.UUID] = mapped_column(
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
    description: Mapped[str] = mapped_column(String(4096), nullable=False)
    priority: Mapped[str] = mapped_column(
        Enum(*_PRIORITIES, name="recommendation_priority"),
        nullable=False,
        default="medium",
        server_default="medium",
    )
    status: Mapped[str] = mapped_column(
        Enum(*_RECOMMENDATION_STATUSES, name="recommendation_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<RecommendationModel({self.recommendation_id}) priority={self.priority!r} "
            f"status={self.status!r}>"
        )


# =============================================================================
# Recommendation <-> Findings (M2M, composite PK — membership_venues pattern)
# =============================================================================


recommendation_findings = Table(
    "recommendation_findings",
    Base.metadata,
    Column("recommendation_id", UUID(as_uuid=True), primary_key=True),
    Column("finding_id", UUID(as_uuid=True), primary_key=True),
    # Denormalized tenant (FK-derived) so links are RLS-scoped and the
    # composite FKs reject cross-tenant links (migration 003 pattern).
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    ForeignKeyConstraint(
        ["recommendation_id", "tenant_id"],
        ["recommendations.recommendation_id", "recommendations.tenant_id"],
        ondelete="CASCADE",
        name="fk_recommendation_findings_recommendation_tenant",
    ),
    ForeignKeyConstraint(
        ["finding_id", "tenant_id"],
        ["findings.finding_id", "findings.tenant_id"],
        ondelete="CASCADE",
        name="fk_recommendation_findings_finding_tenant",
    ),
    # Finding-first lookups (which recommendations cite this finding?).
    Index("ix_recommendation_findings_finding_id", "finding_id"),
    PrimaryKeyConstraint("recommendation_id", "finding_id"),
)
