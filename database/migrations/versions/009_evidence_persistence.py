"""Create the evidence persistence schema (Task 6.7).

Persists the Task 4 evidence contracts — `EvidenceRef`
(contracts/events/evidence.py) and `EvidencePackage`
(contracts/intelligence/models.py) — as the authoritative metadata
store. Binary artifacts NEVER live in PostgreSQL: evidence metadata and
provenance are stored here; artifact bytes live in object storage
(MinIO/S3) and are referenced by key/URI (governance Section 1 rule 3).

  evidence_refs          — one row per evidence artifact reference:
                           object key, artifact type, content metadata,
                           checksum, capture/event relationship, tenant,
                           venue, timestamps.
  evidence_packages      — bounded, reviewable collections of evidence
                           (the EvidencePackage contract).
  package_evidence_refs  — M2M association between packages and refs
                           (an artifact can support several packages).

Design decisions (each maps to a governance policy):

  - Typed columns for everything queried/filtered/validated: `ref_type`
    enum, `content_type`, `size_bytes`, `checksum`. JSONB `metadata`
    exists only for genuinely variable artifact metadata (Section 7).
  - Tenant ownership is DIRECT and DB-enforced: every table carries
    `tenant_id NOT NULL` plus composite FKs (venue/ref/package,
    tenant_id) — the pattern established in migrations 003/005/008.
    Cross-tenant references are rejected by composite FKs and RLS.
  - Artifact reference validation: `ref_uri` is required and non-empty;
    `checksum`, when present, must be a 64-char lowercase hex sha256
    digest (CHECK constraint).
  - Traceability: `evidence_refs` carries the source event/video
    context — a nullable composite FK (event_time, event_id) ->
    operational_events (the hypertable's PK; the pair must be both
    present or both absent — CHECK) and nullable composite FKs
    (session_id, tenant_id) -> video_sessions and
    (camera_id, tenant_id) -> cameras. The event FK cannot carry
    tenant_id because the hypertable PK is (event_time, event_id);
    cross-tenant event links are prevented by RLS + repository scoping.
  - Orphan prevention: venue-owned rows and package/ref links cascade
    (ON DELETE CASCADE). The event FK is SET NULL so evidence survives
    operational event retention/pruning while traceability degrades
    gracefully.
  - video_assets.evidence_ref (migration 005) stays a bare-UUID forward
    reference — wiring it would create a table dependency cycle (see
    the upgrade() note); provenance to video context flows through
    evidence_refs.session_id / camera_id instead.
  - `video_assets.evidence_ref` (migration 005 forward reference) gains
    its real FK now that the evidence schema exists.
  - NOT a hypertable: evidence metadata is low-volume and referential
    (governance Sections 3.5 and 11.3 — evidence metadata is explicitly
    NOT a hypertable candidate).
  - RLS + grants ship in the same migration (Section 10.4 rule 5).

Revision ID: 009_evidence_persistence
Revises: 008_operational_events
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

# revision identifiers, used by Alembic.
revision: str = "009_evidence_persistence"
down_revision: str | None = "008_operational_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON evidence_refs TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON evidence_packages TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON package_evidence_refs TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON evidence_refs FROM hotelops_app;",
    "REVOKE ALL ON evidence_packages FROM hotelops_app;",
    "REVOKE ALL ON package_evidence_refs FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)

# The five EvidenceType values from contracts/events/evidence.py.
_EVIDENCE_TYPES = ("frame", "image", "video_clip", "object_storage", "analytical_artifact")


def upgrade() -> None:
    """Create the evidence tables (and the video_assets FK), then enable RLS."""

    # --- EVIDENCE REFS (artifact references; bytes live in object storage) ---
    op.create_table(
        "evidence_refs",
        sa.Column("ref_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column(
            "ref_type",
            ENUM(*_EVIDENCE_TYPES, name="evidence_type"),
            nullable=False,
        ),
        # Object-storage key / resolvable location (never the bytes).
        sa.Column("ref_uri", sa.String(2048), nullable=False),
        # Content metadata — typed because it is displayed/filtered.
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        # sha256 hex digest when the artifact is hashed (validation below).
        sa.Column("checksum", sa.String(128), nullable=True),
        # Source event context: composite FK to the operational_events
        # hypertable PK (event_time, event_id). Both columns or neither.
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_id", sa.UUID(), nullable=True),
        # Source video context.
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("camera_id", sa.UUID(), nullable=True),
        # Genuinely variable artifact metadata only (JSONB policy).
        sa.Column("metadata", JSONB(), nullable=True),
        # When the artifact itself was captured (may differ from event_time
        # for recorded evidence processed later).
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        # Composite FK target for package_evidence_refs and video_assets.
        sa.UniqueConstraint("ref_id", "tenant_id", name="uq_evidence_refs_ref_tenant"),
        sa.CheckConstraint(
            "length(btrim(ref_uri)) > 0",
            name="ck_evidence_refs_uri_not_empty",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_evidence_refs_size_non_negative",
        ),
        sa.CheckConstraint(
            "checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_refs_checksum_sha256",
        ),
        # The event link is an atomic pair — never a half-populated FK.
        sa.CheckConstraint(
            "(event_id IS NULL AND event_time IS NULL) "
            "OR (event_id IS NOT NULL AND event_time IS NOT NULL)",
            name="ck_evidence_refs_event_pair",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_refs_venue_tenant",
        ),
        # Hypertable PK target — cannot carry tenant_id (see header note).
        sa.ForeignKeyConstraint(
            ["event_time", "event_id"],
            ["operational_events.event_time", "operational_events.event_id"],
            ondelete="SET NULL",
            name="fk_evidence_refs_event",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["video_sessions.session_id", "video_sessions.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_refs_session_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.camera_id", "cameras.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_refs_camera_tenant",
        ),
    )
    # Query patterns (governance Section 9): tenant/venue-scoped evidence
    # review, session/event provenance lookups, time-range review.
    op.create_index("ix_evidence_refs_tenant_id", "evidence_refs", ["tenant_id"])
    op.create_index("ix_evidence_refs_venue_id", "evidence_refs", ["venue_id"])
    op.create_index("ix_evidence_refs_session_id", "evidence_refs", ["session_id"])
    op.create_index("ix_evidence_refs_event_id", "evidence_refs", ["event_id"])
    op.create_index("ix_evidence_refs_captured_at", "evidence_refs", ["captured_at"])

    # --- EVIDENCE PACKAGES (bounded, reviewable evidence collections) ---
    op.create_table(
        "evidence_packages",
        sa.Column("package_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("package_id"),
        # Composite FK target for package_evidence_refs.
        sa.UniqueConstraint("package_id", "tenant_id", name="uq_evidence_packages_package_tenant"),
        sa.CheckConstraint(
            "description IS NULL OR length(btrim(description)) > 0",
            name="ck_evidence_packages_description_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_evidence_packages_venue_tenant",
        ),
    )
    op.create_index("ix_evidence_packages_tenant_id", "evidence_packages", ["tenant_id"])
    op.create_index("ix_evidence_packages_venue_id", "evidence_packages", ["venue_id"])
    op.create_index("ix_evidence_packages_created_at", "evidence_packages", ["created_at"])

    # --- PACKAGE <-> REF ASSOCIATION (M2M; composite-PK join table) ---
    op.create_table(
        "package_evidence_refs",
        sa.Column("package_id", sa.UUID(), nullable=False),
        sa.Column("ref_id", sa.UUID(), nullable=False),
        # Denormalized tenant (FK-derived) so links are RLS-scoped and
        # composite FKs reject cross-tenant links — the established
        # membership_venues pattern (migration 003).
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("package_id", "ref_id"),
        sa.ForeignKeyConstraint(
            ["package_id", "tenant_id"],
            ["evidence_packages.package_id", "evidence_packages.tenant_id"],
            ondelete="CASCADE",
            name="fk_package_evidence_refs_package_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["ref_id", "tenant_id"],
            ["evidence_refs.ref_id", "evidence_refs.tenant_id"],
            ondelete="CASCADE",
            name="fk_package_evidence_refs_ref_tenant",
        ),
    )
    # Ref -> packages lookups (which packages cite this artifact?).
    op.create_index("ix_package_evidence_refs_ref_id", "package_evidence_refs", ["ref_id"])

    # NOTE: video_assets.evidence_ref (migration 005) deliberately stays a
    # bare-UUID forward reference — wiring a composite FK now would create a
    # dependency cycle (evidence_refs -> video_sessions -> video_assets ->
    # evidence_refs) that SQLAlchemy cannot sort. Provenance from evidence to
    # its video context flows through evidence_refs.session_id / camera_id /
    # event_id instead; the asset->evidence direction needs a deliberate
    # design decision (and is not required by Task 6.7).

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    for table in ("evidence_refs", "evidence_packages", "package_evidence_refs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
            f"USING (tenant_id = {_CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
        )


def downgrade() -> None:
    """Drop the evidence RLS policies, tables, FK, and the enum type."""
    for table in ("package_evidence_refs", "evidence_packages", "evidence_refs"):
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.drop_table("package_evidence_refs")
    op.drop_table("evidence_packages")
    op.drop_table("evidence_refs")
    op.execute("DROP TYPE evidence_type")
