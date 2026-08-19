"""Create the AI domain persistence schema (Task 6.9).

Persists the Task 4 intelligence contracts (contracts/intelligence/
models.py) as DERIVED data (ADR-002: deterministic core, LLM-last) —
never as operational truth:

  findings           — a conclusion supported by evidence (the Finding
                       contract: finding identity, evidence_package_id
                       source link, finding_type, description,
                       confidence, event_time) plus review workflow
                       state (status), tenant, venue, model/provider
                       metadata, and version information.
  recommendations    — an evidence-grounded proposed action (the
                       Recommendation contract: recommendation identity,
                       optional opportunity link, description,
                       priority) plus review workflow state (status),
                       tenant, venue, model/provider metadata, version.
  recommendation_findings — M2M: which findings support a
                       recommendation (Recommendation.finding_ids).

Design decisions (each maps to a governance policy):

  - AI outputs are DERIVED records. They reference their source context
    (evidence packages, opportunities) via real composite FKs; they can
    NEVER modify authoritative operational data — no operational table
    is FK-targeted or cascaded from here, and no write-back path exists
    (ADR-002, governance Section 1 rule 4). Arbitrary LLM conversations
    are never stored as business truth — only structured findings and
    recommendations, with JSONB `metadata` reserved for genuinely
    variable model context (governance Section 7).
  - Source/evidence linkage: `findings.evidence_package_id` is a real
    composite FK (evidence_package_id, tenant_id) -> evidence_packages
    with ON DELETE RESTRICT — evidence cited by a derived finding is
    never silently destroyed. A composite FK with a denormalized
    tenant_id cannot use SET NULL (it would null tenant_id, violating
    NOT NULL); RESTRICT is the correct orphan-prevention semantics —
    retention tooling must unlink findings before purging packages.
    NOTE: the Finding contract declares evidence_package_id required,
    but the column is nullable in the schema so ungrounded or unlinked
    findings can exist; grounded findings set it, and the FK enforces
    the tenancy + delete invariants when they do.
  - Review workflow: both tables carry an explicit `status` enum
    (findings: proposed/accepted/rejected/archived; recommendations:
    pending/accepted/rejected/implemented/archived) and an `updated_at`
    timestamp for transitions. Status is DB-level workflow state
    (like config_status, task 6.5) — not part of the wire contracts.
    The DB constrains the value SET (enum) and that updated_at never
    precedes created_at; transition LEGALITY (which state may follow
    which) is the application's responsibility — a state machine in the
    database would be over-engineering.
  - Version information: `schema_version` (contract version) with a
    server default, per the two-axis versioning policy (Section 15).
  - Model/provider metadata: `model_name` + `model_version` (nullable)
    record which AI model derived the output, where required.
  - Tenant ownership is DIRECT: `tenant_id NOT NULL` + composite FKs
    (venue, evidence_package, opportunity) — the established pattern.
    Cross-tenant references rejected by composite FKs and RLS.
  - Relational, NOT hypertables: AI domain is low-volume, stateful,
    review-oriented (governance Section 3.7 — not a time-series
    candidate, Section 11.3).
  - RLS + grants ship in the same migration (Section 10.4 rule 5).

Revision ID: 011_ai_domain_storage
Revises: 010_analytics_storage
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "011_ai_domain_storage"
down_revision: str | None = "010_analytics_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON findings TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON recommendations TO hotelops_app;",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON recommendation_findings TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON findings FROM hotelops_app;",
    "REVOKE ALL ON recommendations FROM hotelops_app;",
    "REVOKE ALL ON recommendation_findings FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)


def upgrade() -> None:
    """Create the AI domain tables, then enable RLS on them."""

    # --- FINDINGS (derived, evidence-grounded conclusions) ---
    op.create_table(
        "findings",
        sa.Column("finding_id", sa.UUID(), nullable=False),
        # Contract version — two-axis versioning (governance Section 15).
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        # What class of conclusion this is (contract Finding.finding_type).
        sa.Column("finding_type", sa.String(100), nullable=False),
        sa.Column("description", sa.String(4096), nullable=False),
        # Optional model confidence 0..1 (contract Finding.confidence).
        sa.Column("confidence", sa.Double(), nullable=True),
        # The finding's effective/observed time (contract Finding.event_time).
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        # Review workflow state (DB-level, not on the wire contract).
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "accepted",
                "rejected",
                "archived",
                name="finding_status",
            ),
            nullable=False,
            server_default="proposed",
        ),
        # Source/evidence linkage (nullable until the finding is grounded).
        sa.Column("evidence_package_id", sa.UUID(), nullable=True),
        # Model/provider metadata (derived-data provenance).
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("model_version", sa.String(50), nullable=True),
        # Genuinely variable model context only (JSONB policy).
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Last status transition (mutations are legal — workflow state).
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("finding_id"),
        # Composite FK target for recommendation_findings.
        sa.UniqueConstraint("finding_id", "tenant_id", name="uq_findings_finding_tenant"),
        sa.CheckConstraint(
            "length(btrim(finding_type)) > 0",
            name="ck_findings_finding_type_not_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_findings_description_not_empty",
        ),
        # Contract confidence is bounded [0, 1].
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_findings_confidence_range",
        ),
        # A transitioned finding must be more recently updated than created.
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_findings_updated_not_before_created",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_findings_venue_tenant",
        ),
        # Evidence linkage — RESTRICT: evidence cited by a derived finding
        # is never silently destroyed (composite FK cannot SET NULL — it
        # would null tenant_id; see migration header note).
        sa.ForeignKeyConstraint(
            ["evidence_package_id", "tenant_id"],
            ["evidence_packages.package_id", "evidence_packages.tenant_id"],
            ondelete="RESTRICT",
            name="fk_findings_evidence_package_tenant",
        ),
    )
    # Query patterns (governance Section 9): tenant/venue review queues,
    # status-filtered review UI, evidence-first provenance lookups,
    # time-range review.
    op.create_index("ix_findings_tenant_id", "findings", ["tenant_id"])
    op.create_index("ix_findings_venue_id", "findings", ["venue_id"])
    op.create_index("ix_findings_status", "findings", ["status"])
    op.create_index("ix_findings_event_time", "findings", [sa.text("event_time DESC")])
    op.create_index("ix_findings_evidence_package_id", "findings", ["evidence_package_id"])

    # --- RECOMMENDATIONS (evidence-grounded proposed actions) ---
    op.create_table(
        "recommendations",
        sa.Column("recommendation_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("description", sa.String(4096), nullable=False),
        # Contract Recommendation.priority (high/medium/low).
        sa.Column(
            "priority",
            sa.Enum("high", "medium", "low", name="recommendation_priority"),
            nullable=False,
            server_default="medium",
        ),
        # Review workflow state (DB-level, not on the wire contract).
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "accepted",
                "rejected",
                "implemented",
                "archived",
                name="recommendation_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        # Optional business-opportunity linkage (contract field).
        sa.Column("opportunity_id", sa.UUID(), nullable=True),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("model_version", sa.String(50), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("recommendation_id"),
        # Composite FK target for recommendation_findings.
        sa.UniqueConstraint(
            "recommendation_id",
            "tenant_id",
            name="uq_recommendations_recommendation_tenant",
        ),
        sa.CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_recommendations_description_not_empty",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_recommendations_updated_not_before_created",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_recommendations_venue_tenant",
        ),
        # Opportunity linkage — RESTRICT: a recommendation citing an
        # opportunity blocks its deletion (composite FK cannot SET NULL).
        sa.ForeignKeyConstraint(
            ["opportunity_id", "tenant_id"],
            ["opportunities.opportunity_id", "opportunities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_recommendations_opportunity_tenant",
        ),
    )
    op.create_index("ix_recommendations_tenant_id", "recommendations", ["tenant_id"])
    op.create_index("ix_recommendations_venue_id", "recommendations", ["venue_id"])
    op.create_index("ix_recommendations_status", "recommendations", ["status"])
    op.create_index(
        "ix_recommendations_created_at", "recommendations", [sa.text("created_at DESC")]
    )

    # --- RECOMMENDATION <-> FINDINGS (M2M, membership_venues pattern) ---
    op.create_table(
        "recommendation_findings",
        sa.Column("recommendation_id", sa.UUID(), nullable=False),
        sa.Column("finding_id", sa.UUID(), nullable=False),
        # Denormalized tenant (FK-derived) so links are RLS-scoped and the
        # composite FKs reject cross-tenant links.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("recommendation_id", "finding_id"),
        sa.ForeignKeyConstraint(
            ["recommendation_id", "tenant_id"],
            ["recommendations.recommendation_id", "recommendations.tenant_id"],
            ondelete="CASCADE",
            name="fk_recommendation_findings_recommendation_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "tenant_id"],
            ["findings.finding_id", "findings.tenant_id"],
            ondelete="CASCADE",
            name="fk_recommendation_findings_finding_tenant",
        ),
    )
    # Finding-first lookups (which recommendations cite this finding?).
    op.create_index(
        "ix_recommendation_findings_finding_id", "recommendation_findings", ["finding_id"]
    )

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    for table in ("findings", "recommendations", "recommendation_findings"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO hotelops_app "
            f"USING (tenant_id = {_CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
        )


def downgrade() -> None:
    """Drop the AI domain RLS policies, tables, and enum types."""
    for table in ("recommendation_findings", "recommendations", "findings"):
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.drop_table("recommendation_findings")
    op.drop_table("recommendations")
    op.drop_table("findings")
    # Single-use enum types created by op.create_table; drop explicitly so
    # a downgrade fully reverses the upgrade (005/009 pattern).
    op.execute("DROP TYPE finding_status")
    op.execute("DROP TYPE recommendation_priority")
    op.execute("DROP TYPE recommendation_status")
