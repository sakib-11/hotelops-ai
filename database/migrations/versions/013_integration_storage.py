"""Create the integration persistence schema (Task 6.11).

Persists configuration for external systems (POS/PMS/staffing/storage
adapters — see docs/product/integration-scope.md) with strong tenant
ownership and a SECURE secrets posture:

  integrations — one row per external integration: integration
                 identity, tenant ownership, provider/type, explicit
                 status lifecycle, configuration metadata, external
                 identifiers, timestamps.

Secrets design (security architecture, task 5.1):
  - NO secrets are stored in relational columns. The security
    architecture has no secrets-management platform (Settings load
    from environment); this task does NOT invent one and does NOT
    create a second encryption system.
  - `secret_ref` is a REFERENCE to where the credential lives (e.g. an
    environment-variable name or external secret-store key) — never
    the credential value itself. The application resolves the actual
    secret from the existing Settings/environment at runtime.
  - Configuration metadata (JSONB) carries only non-sensitive adapter
    settings; a CHECK (via an IMMUTABLE helper function) rejects any
    key whose FIRST underscore/dot-separated segment matches the audit
    contract's blocked secret terms (password, token, secret, key,
    credential, authorization) — the exact same vocabulary and
    first-segment semantics as contracts/audit/models.py (so
    'secret_key' is blocked while 'api_key' is allowed).
  - `external_identifier` records the external system's own id (e.g.
    a POS store id) — a business attribute, not a credential.
  - Scope note: the metadata CHECK is a TOP-LEVEL-KEY tripwire only
    (defense in depth) — nested objects are not recursed, matching the
    audit contract's first-segment semantics; the application layer is
    the real guard for non-sensitive metadata.
  - The status trigger fires on UPDATE OF status only (the 012 pattern):
    rows may be INSERTed directly in any state (e.g. restoring an
    integration as active) — the trigger governs transitions, not
    creation.

State lifecycle (governance 3.10: "config + execution status"):
  - `integration_status` enum with EXPLICIT transitions enforced by a
    BEFORE UPDATE trigger (the 012 pattern): pending -> active /
    disabled / error; active -> disabled / error; error -> active /
    disabled; disabled -> active. Illegal transitions RAISE and roll
    back (tested). No boolean status flags.
  - Duplicate provider constraint: at most one ACTIVE integration per
    (tenant_id, provider_name) — a partial unique index (the 007
    pattern) so a tenant cannot configure the same provider twice in
    an active state; disabled/pending rows do not block re-creation.

Design decisions (each maps to a governance policy):
  - Tenant ownership: DIRECT — tenant_id NOT NULL + composite FK
    (venue_id, tenant_id) -> venues. RLS + grants in the same
    migration (Section 10.4 rule 5).
  - Timestamps: created_at (server now(), UTC) + updated_at for status
    transitions (trigger-set).
  - Relational, NOT hypertables (governance 3.10: config is
    current-state; execution records deferred until volume demands).
  - Integration execution records are NOT created — governance 3.10
    defers them ("only if volume demands").

Revision ID: 013_integration_storage
Revises: 012_alert_approval_storage
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "013_integration_storage"
down_revision: str | None = "012_alert_approval_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: asyncpg does not support multiple SQL commands in a single
# prepared statement. Each statement below must be executed separately.
_SQL_GRANT_TABLES = [
    "GRANT SELECT, INSERT, UPDATE, DELETE ON integrations TO hotelops_app;",
]

_SQL_REVOKE_TABLES = [
    "REVOKE ALL ON integrations FROM hotelops_app;",
]

_CURRENT_TENANT = (
    "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), "
    "'00000000-0000-0000-0000-000000000000')::uuid"
)

# JSONB cannot be inspected by ?| with first-segment semantics (exact-key
# match only), so a small IMMUTABLE helper implements the audit contract's
# blocked-term check; the CHECK constraint calls it.
#
# NOTE: keep this SQL identical to METADATA_NO_SECRETS_FUNCTION_SQL in
# backend/app/infrastructure/database/models/integrations.py (the mirror is
# used by Base.metadata.create_all() test fixtures, which must create the
# function first because the CHECK references it).
_SQL_METADATA_NO_SECRETS_FUNCTION = """
CREATE OR REPLACE FUNCTION integration_config_has_secret(jsonb_value jsonb)
RETURNS boolean AS $$
DECLARE
    k text;
    first_segment text;
BEGIN
    -- jsonb_object_keys only applies to objects; non-object metadata (or
    -- NULL) has no keys to inspect, so it cannot carry a secret key.
    IF jsonb_value IS NULL OR jsonb_typeof(jsonb_value) <> 'object' THEN
        RETURN false;
    END IF;
    FOR k IN SELECT * FROM jsonb_object_keys(jsonb_value)
    LOOP
        first_segment := lower(
            split_part(split_part(k, '-', 1), '_', 1)
        );
        first_segment := split_part(first_segment, '.', 1);
        IF first_segment IN (
            'password', 'token', 'secret', 'key', 'credential', 'authorization'
        ) THEN
            RETURN true;
        END IF;
    END LOOP;
    RETURN false;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""

# Explicit integration lifecycle (governance 3.10). A CHECK cannot compare
# OLD/NEW row values, so transition legality is enforced by a BEFORE UPDATE
# trigger; illegal transitions RAISE and the UPDATE rolls back.
_SQL_INTEGRATION_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION check_integration_status_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status = 'pending' AND NEW.status IN ('active', 'disabled', 'error'))
       OR (OLD.status = 'active' AND NEW.status IN ('disabled', 'error'))
       OR (OLD.status = 'error' AND NEW.status IN ('active', 'disabled'))
       OR (OLD.status = 'disabled' AND NEW.status IN ('active')) THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal integration status transition: % -> %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;
"""

_SQL_INTEGRATION_TRANSITION_TRIGGER = """
CREATE TRIGGER trg_integrations_status_transition
BEFORE UPDATE OF status ON integrations
FOR EACH ROW EXECUTE FUNCTION check_integration_status_transition();
"""


def upgrade() -> None:
    """Create the integrations table, status trigger, and RLS policies."""

    op.execute(_SQL_METADATA_NO_SECRETS_FUNCTION)

    op.create_table(
        "integrations",
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        # Provider category — the adapter contract family.
        sa.Column(
            "provider_type",
            sa.Enum("pos", "pms", "staffing", "storage", name="integration_provider"),
            nullable=False,
        ),
        # The specific system/vendor (business attribute, e.g. 'lightspeed').
        sa.Column("provider_name", sa.String(100), nullable=False),
        # Explicit lifecycle state — never boolean flags.
        sa.Column(
            "status",
            sa.Enum("pending", "active", "disabled", "error", name="integration_status"),
            nullable=False,
            server_default="pending",
        ),
        # Non-sensitive adapter configuration ONLY (secret terms blocked).
        sa.Column("config_metadata", JSONB(), nullable=True),
        # REFERENCE to the credential location — NEVER the credential value.
        sa.Column("secret_ref", sa.String(255), nullable=True),
        # The external system's own identifier (business attribute).
        sa.Column("external_identifier", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Last status transition (set by the transition trigger).
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("integration_id"),
        sa.CheckConstraint(
            "length(btrim(provider_name)) > 0",
            name="ck_integrations_provider_name_not_empty",
        ),
        sa.CheckConstraint(
            "secret_ref IS NULL OR length(btrim(secret_ref)) > 0",
            name="ck_integrations_secret_ref_not_empty",
        ),
        sa.CheckConstraint(
            "external_identifier IS NULL OR length(btrim(external_identifier)) > 0",
            name="ck_integrations_external_identifier_not_empty",
        ),
        sa.CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_integrations_updated_not_before_created",
        ),
        # Secrets never ride in configuration metadata — the audit contract's
        # blocked terms applied at the first key segment (contracts/audit/
        # models.py) via the IMMUTABLE helper above.
        sa.CheckConstraint(
            "config_metadata IS NULL OR NOT integration_config_has_secret(config_metadata)",
            name="ck_integrations_metadata_no_secrets",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_integrations_venue_tenant",
        ),
    )
    # Query patterns (governance Section 9): tenant/venue-scoped config
    # listing, status-filtered administration UI, provider lookups.
    op.create_index("ix_integrations_tenant_id", "integrations", ["tenant_id"])
    op.create_index("ix_integrations_venue_id", "integrations", ["venue_id"])
    op.create_index("ix_integrations_status", "integrations", ["status"])
    op.create_index("ix_integrations_provider_type", "integrations", ["provider_type"])
    op.create_index("ix_integrations_provider_name", "integrations", ["provider_name"])
    # Duplicate provider constraint: at most one ACTIVE integration per
    # (tenant_id, provider_name) — the 007 partial-unique pattern.
    op.create_index(
        "uq_integrations_active_provider",
        "integrations",
        ["tenant_id", "provider_name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.execute(_SQL_INTEGRATION_TRANSITION_FUNCTION)
    op.execute(_SQL_INTEGRATION_TRANSITION_TRIGGER)

    # --- RLS + grants (same migration, governance Section 10.4 rule 5) ---
    for stmt in _SQL_GRANT_TABLES:
        op.execute(stmt)

    op.execute("ALTER TABLE integrations ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE integrations FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY integrations_all ON integrations FOR ALL TO hotelops_app "
        f"USING (tenant_id = {_CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {_CURRENT_TENANT});"
    )


def downgrade() -> None:
    """Drop the integrations RLS policy, trigger, table, and enum types."""
    op.execute("DROP POLICY IF EXISTS integrations_all ON integrations;")
    op.execute("ALTER TABLE integrations DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE integrations NO FORCE ROW LEVEL SECURITY;")

    for stmt in _SQL_REVOKE_TABLES:
        op.execute(stmt)

    op.execute("DROP TRIGGER IF EXISTS trg_integrations_status_transition ON integrations")
    op.execute("DROP FUNCTION IF EXISTS check_integration_status_transition()")

    op.drop_table("integrations")
    # The table's CHECK references the helper — drop the function AFTER the
    # table (dependency order) so the downgrade fully reverses the upgrade.
    op.execute("DROP FUNCTION IF EXISTS integration_config_has_secret(jsonb)")
    # Single-use enum types created by op.create_table; drop explicitly so
    # a downgrade fully reverses the upgrade (005/009/011/012 pattern).
    op.execute("DROP TYPE integration_provider")
    op.execute("DROP TYPE integration_status")
