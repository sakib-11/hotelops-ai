"""SQLAlchemy ORM models for the evidence persistence layer (Task 6.7).

Persists the Task 4 evidence contracts — `EvidenceRef`
(contracts/events/evidence.py) and `EvidencePackage`
(contracts/intelligence/models.py) — as the authoritative metadata
store (governance Section 3.5). Binary artifacts never live in
PostgreSQL: evidence metadata and provenance are stored here; artifact
bytes live in object storage and are referenced by key/URI.

  EvidenceRefModel      — one row per artifact reference: object key,
                          artifact type, content metadata, checksum,
                          capture/event relationship, tenant, venue,
                          timestamps.
  EvidencePackageModel  — bounded, reviewable evidence collections.
  package_evidence_refs — M2M association between packages and refs
                          (composite PK join table, membership_venues
                          pattern).

Tenant ownership is DIRECT and DB-enforced (composite FKs + RLS). The
`video_assets.evidence_ref` forward reference from migration 005 is
wired to evidence_refs by a real FK in migration 009. No relationships
are declared — this is a metadata/review store (the schema is the
deliverable), consistent with the event model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION

# Enum values — mirror contracts/events/evidence.py (EvidenceType).
_EVIDENCE_TYPES = ("frame", "image", "video_clip", "object_storage", "analytical_artifact")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EvidenceRefModel(Base):
    __tablename__ = "evidence_refs"

    __table_args__ = (
        # Composite FK target for package_evidence_refs and video_assets.
        UniqueConstraint("ref_id", "tenant_id", name="uq_evidence_refs_ref_tenant"),
        CheckConstraint(
            "length(btrim(ref_uri)) > 0",
            name="ck_evidence_refs_uri_not_empty",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_evidence_refs_size_non_negative",
        ),
        CheckConstraint(
            "checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_refs_checksum_sha256",
        ),
        # The event link is an atomic pair — never a half-populated FK.
        CheckConstraint(
            "(event_id IS NULL AND event_time IS NULL) "
            "OR (event_id IS NOT NULL AND event_time IS NOT NULL)",
            name="ck_evidence_refs_event_pair",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_refs_venue_tenant",
        ),
        # Hypertable PK target — cannot carry tenant_id (migration 009 note).
        ForeignKeyConstraint(
            ["event_time", "event_id"],
            ["operational_events.event_time", "operational_events.event_id"],
            ondelete="SET NULL",
            name="fk_evidence_refs_event",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_refs_session_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_refs_camera_tenant",
        ),
        # Query patterns (governance Section 9): tenant/venue-scoped review,
        # session/event provenance lookups, time-range review.
        Index("ix_evidence_refs_tenant_id", "tenant_id"),
        Index("ix_evidence_refs_venue_id", "venue_id"),
        Index("ix_evidence_refs_session_id", "session_id"),
        Index("ix_evidence_refs_event_id", "event_id"),
        Index("ix_evidence_refs_captured_at", "captured_at"),
    )

    ref_id: Mapped[uuid.UUID] = mapped_column(
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
    ref_type: Mapped[str] = mapped_column(
        Enum(*_EVIDENCE_TYPES, name="evidence_type"),
        nullable=False,
    )
    # Object-storage key / resolvable location (never the bytes).
    ref_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Content metadata — typed because it is displayed/filtered.
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Source event context (both columns or neither — CHECK). NOTE:
    # event_time here is the SOURCE event's timestamp (the FK pair with
    # event_id), never this artifact's own capture time (captured_at).
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Source video context.
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Genuinely variable artifact metadata only (JSONB policy).
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=None
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<EvidenceRefModel({self.ref_id}) {self.ref_type!r} uri={self.ref_uri!r}>"


class EvidencePackageModel(Base):
    __tablename__ = "evidence_packages"

    __table_args__ = (
        # Composite FK target for package_evidence_refs.
        UniqueConstraint("package_id", "tenant_id", name="uq_evidence_packages_package_tenant"),
        CheckConstraint(
            "description IS NULL OR length(btrim(description)) > 0",
            name="ck_evidence_packages_description_not_empty",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_packages_venue_tenant",
        ),
        Index("ix_evidence_packages_tenant_id", "tenant_id"),
        Index("ix_evidence_packages_venue_id", "venue_id"),
        Index("ix_evidence_packages_created_at", "created_at"),
    )

    package_id: Mapped[uuid.UUID] = mapped_column(
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
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<EvidencePackageModel({self.package_id}) venue={self.venue_id}>"


# =============================================================================
# Package <-> Ref association (M2M, composite PK — membership_venues pattern)
# =============================================================================


package_evidence_refs = Table(
    "package_evidence_refs",
    Base.metadata,
    Column("package_id", UUID(as_uuid=True), primary_key=True),
    Column("ref_id", UUID(as_uuid=True), primary_key=True),
    # Denormalized tenant (FK-derived) so links are RLS-scoped and the
    # composite FKs reject cross-tenant links (migration 003 pattern).
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    ForeignKeyConstraint(
        ["package_id", "tenant_id"],
        ["evidence_packages.package_id", "evidence_packages.tenant_id"],
        ondelete="CASCADE",
        name="fk_package_evidence_refs_package_tenant",
    ),
    ForeignKeyConstraint(
        ["ref_id", "tenant_id"],
        ["evidence_refs.ref_id", "evidence_refs.tenant_id"],
        ondelete="CASCADE",
        name="fk_package_evidence_refs_ref_tenant",
    ),
    # Ref -> packages lookups (which packages cite this artifact?).
    Index("ix_package_evidence_refs_ref_id", "ref_id"),
    PrimaryKeyConstraint("package_id", "ref_id"),
)
