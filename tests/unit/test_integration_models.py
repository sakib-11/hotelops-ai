"""Tests for Task 6.11 — integration persistence ORM model.

Tests schema correctness — integration identity, provider/type,
explicit status enum (never boolean flags), the secure secrets posture
(secret_ref as a reference, secret terms blocked in config metadata),
duplicate-provider partial unique index, composite FKs, and indexes —
without requiring a live database. Status transitions, RLS, and
migration behavior are exercised by the integration tests against a
real TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, Table

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.integrations import IntegrationModel
from contracts.common import IntegrationId


class TestIntegrationsSchema:
    """One row per external integration."""

    def _table(self) -> Table:
        return Base.metadata.tables["integrations"]

    def test_table_exists(self) -> None:
        assert "integrations" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert IntegrationModel.__tablename__ == "integrations"

    def test_integration_identity_and_contract_id(self) -> None:
        """Integration identity is the canonical IntegrationId NewType."""
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["integration_id"]
        assert IntegrationId.__name__ == "IntegrationId"

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable

    def test_provider_type_is_enum(self) -> None:
        """Provider category is an enum of the adapter families."""
        table = self._table()
        col = table.columns["provider_type"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"pos", "pms", "staffing", "storage"}
        assert not col.nullable
        assert not table.columns["provider_name"].nullable

    def test_status_is_explicit_lifecycle_enum_not_booleans(self) -> None:
        """State is a single enum — no is_active/is_disabled flags."""
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"pending", "active", "disabled", "error"}
        assert not col.nullable
        names = {c.name for c in table.columns}
        assert "is_active" not in names
        assert "is_disabled" not in names
        assert "is_error" not in names

    def test_secrets_never_in_relational_columns(self) -> None:
        """No column may hold a credential value — only a secret_ref
        reference and non-sensitive config metadata."""
        table = self._table()
        names = {c.name for c in table.columns}
        for secret_column in ("api_key", "password", "token", "credential", "secret"):
            assert secret_column not in names, f"Secret column must not exist: {secret_column}"
        # secret_ref exists but is a REFERENCE (nullable, non-empty CHECK).
        assert "secret_ref" in names
        assert table.columns["secret_ref"].nullable

    def test_metadata_no_secrets_check(self) -> None:
        """Config metadata rejects secret-like keys via the IMMUTABLE helper
        (audit contract first-segment semantics)."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_integrations_metadata_no_secrets" in checks
        assert "integration_config_has_secret" in checks["ck_integrations_metadata_no_secrets"]

    def test_external_identifier_and_secret_ref_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_integrations_secret_ref_not_empty" in checks
        assert "ck_integrations_external_identifier_not_empty" in checks
        assert "ck_integrations_provider_name_not_empty" in checks
        assert "ck_integrations_updated_not_before_created" in checks

    def test_venue_tenant_composite_fk(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        venue_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["venue_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["venues", "venues"]
        ]
        assert len(venue_fks) == 1
        assert venue_fks[0].ondelete == "CASCADE"

    def test_duplicate_provider_partial_unique_index(self) -> None:
        """At most one ACTIVE integration per (tenant_id, provider_name)."""
        table = self._table()
        matches = [
            idx
            for idx in table.indexes
            if idx.name == "uq_integrations_active_provider" and idx.unique
        ]
        assert len(matches) == 1
        assert [c.name for c in matches[0].columns] == ["tenant_id", "provider_name"]
        assert matches[0].dialect_options["postgresql"]["where"] is not None

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_integrations_tenant_id",
            "ix_integrations_venue_id",
            "ix_integrations_status",
            "ix_integrations_provider_type",
            "ix_integrations_provider_name",
        ):
            assert name in idx_names, f"Missing index: {name}"

    def test_timestamps(self) -> None:
        table = self._table()
        assert not table.columns["created_at"].nullable
        assert table.columns["created_at"].type.timezone is True
        assert table.columns["updated_at"].nullable
