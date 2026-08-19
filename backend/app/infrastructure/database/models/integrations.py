"""SQLAlchemy ORM model for the integration persistence layer (Task 6.11).

Persists configuration for external systems (POS/PMS/staffing/storage
adapters — docs/product/integration-scope.md) with strong tenant
ownership and a SECURE secrets posture:

  IntegrationModel — one row per external integration: identity,
                     tenant ownership, provider/type, explicit status
                     lifecycle, configuration metadata, external
                     identifiers, timestamps.

Secrets design (security architecture, task 5.1):
  - NO secrets are stored in relational columns. `secret_ref` is a
    REFERENCE to where the credential lives (e.g. an environment
    variable name or external secret-store key) — never the credential
    value itself. The application resolves the actual secret from the
    existing Settings/environment at runtime. This task does NOT invent
    a secrets-management platform or a second encryption system.
  - `config_metadata` (JSONB) carries only non-sensitive adapter
    settings; a DB CHECK rejects secret-like keys using the audit
    contract's blocked terms.

State lifecycle: `integration_status` enum with EXPLICIT transitions
enforced by a BEFORE UPDATE trigger (migration 013) — pending ->
active/disabled/error; active -> disabled/error; error ->
active/disabled; disabled -> active. Illegal transitions RAISE and
roll back. No boolean status flags. Duplicate provider constraint: at
most one ACTIVE integration per (tenant_id, provider_name) via a
partial unique index. No ORM relationships are declared — this is a
config/dashboard store (the schema is the deliverable).
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database.base import Base
from contracts.common import SCHEMA_VERSION

# Enum values — provider adapter families (integration-scope.md) and the
# explicit lifecycle states (governance 3.10).
_PROVIDER_TYPES = ("pos", "pms", "staffing", "storage")
_INTEGRATION_STATUSES = ("pending", "active", "disabled", "error")

# The IMMUTABLE helper behind ck_integrations_metadata_no_secrets. Created by
# migration 013 in production; test fixtures that build the schema via
# Base.metadata.create_all() must create it first (JSONB ?| only does exact
# key match, so first-segment blocked-term semantics need this function).
# Mirrors contracts/audit/models.py: 'secret_key' blocked, 'api_key' allowed.
#
# NOTE: keep this SQL identical to the copy in migration 013 (single-use
# helper; the migration is authoritative and must stay self-contained).
METADATA_NO_SECRETS_FUNCTION_SQL = """
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IntegrationModel(Base):
    __tablename__ = "integrations"

    __table_args__ = (
        CheckConstraint(
            "length(btrim(provider_name)) > 0",
            name="ck_integrations_provider_name_not_empty",
        ),
        CheckConstraint(
            "secret_ref IS NULL OR length(btrim(secret_ref)) > 0",
            name="ck_integrations_secret_ref_not_empty",
        ),
        CheckConstraint(
            "external_identifier IS NULL OR length(btrim(external_identifier)) > 0",
            name="ck_integrations_external_identifier_not_empty",
        ),
        CheckConstraint(
            "updated_at IS NULL OR updated_at >= created_at",
            name="ck_integrations_updated_not_before_created",
        ),
        # Secrets never ride in configuration metadata — the audit contract's
        # blocked terms applied at the first key segment via the IMMUTABLE
        # helper function defined in migration 013 (contracts/audit/models.py
        # semantics: 'secret_key' blocked, 'api_key' allowed).
        CheckConstraint(
            "config_metadata IS NULL OR NOT integration_config_has_secret(config_metadata)",
            name="ck_integrations_metadata_no_secrets",
        ),
        ForeignKeyConstraint(
            ["venue_id", "tenant_id"],
            ["venues.venue_id", "venues.tenant_id"],
            ondelete="CASCADE",
            name="fk_integrations_venue_tenant",
        ),
        # Query patterns: tenant/venue-scoped config listing, status-filtered
        # admin UI, provider lookups.
        Index("ix_integrations_tenant_id", "tenant_id"),
        Index("ix_integrations_venue_id", "venue_id"),
        Index("ix_integrations_status", "status"),
        Index("ix_integrations_provider_type", "provider_type"),
        Index("ix_integrations_provider_name", "provider_name"),
        # Duplicate provider constraint: at most one ACTIVE integration per
        # (tenant_id, provider_name) — partial unique index (007 pattern).
        Index(
            "uq_integrations_active_provider",
            "tenant_id",
            "provider_name",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(
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
    provider_type: Mapped[str] = mapped_column(
        Enum(*_PROVIDER_TYPES, name="integration_provider"),
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*_INTEGRATION_STATUSES, name="integration_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    # Non-sensitive adapter configuration ONLY (secret terms blocked by CHECK).
    config_metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "config_metadata", JSONB, nullable=True, default=None
    )
    # REFERENCE to the credential location — NEVER the credential value.
    secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
            f"<IntegrationModel({self.integration_id}) "
            f"provider={self.provider_name!r} status={self.status!r}>"
        )
