"""Unit tests for the Task 7 idempotency_records ORM model (migration 016).

Schema-correctness tests without a live database: tenant-scoped
uniqueness unit, SHA-256 request-hash CHECK, explicit status enum,
worker lease columns, timestamps, and indexes.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, Table

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.idempotency import IdempotencyRecordModel


class TestIdempotencyRecordSchema:
    def _table(self) -> Table:
        return Base.metadata.tables["idempotency_records"]

    def test_table_exists_and_model_maps(self) -> None:
        assert "idempotency_records" in Base.metadata.tables
        assert IdempotencyRecordModel.__tablename__ == "idempotency_records"

    def test_primary_key(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["idempotency_id"]

    def test_tenant_scoped_uniqueness_unit(self) -> None:
        """(tenant_id, operation, idempotency_key) is the idempotency unit."""
        table = self._table()
        uniques = {u.name: {c.name for c in u.columns} for u in table.constraints}
        assert "uq_idempotency_records_tenant_operation_key" in uniques
        assert uniques["uq_idempotency_records_tenant_operation_key"] == {
            "tenant_id",
            "operation",
            "idempotency_key",
        }

    def test_tenant_recorded_as_value(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.foreign_key_constraints, "tenant_id is a value, not an FK"

    def test_request_hash_is_sha256_hex(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_idempotency_records_request_hash_sha256" in checks
        assert "length(request_hash) = 64" in checks["ck_idempotency_records_request_hash_sha256"]

    def test_status_is_explicit_enum(self) -> None:
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"in_progress", "completed"}
        assert not col.nullable

    def test_actor_and_venue_recorded_values(self) -> None:
        """actor_id/venue_id are recorded VALUES (never client-supplied)."""
        table = self._table()
        assert "actor_id" in {c.name for c in table.columns}
        assert "venue_id" in {c.name for c in table.columns}

    def test_result_and_lease_columns(self) -> None:
        table = self._table()
        names = {c.name for c in table.columns}
        for column in (
            "result",
            "claimed_by",
            "claimed_until",
            "created_at",
            "updated_at",
            "completed_at",
            "expires_at",
        ):
            assert column in names, f"Missing idempotency column: {column}"

    def test_timestamp_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_idempotency_records_updated_not_before_created" in checks
        assert "ck_idempotency_records_completed_not_before_created" in checks
        assert "ck_idempotency_records_lease_not_before_created" in checks

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in ("ix_idempotency_records_tenant_id", "ix_idempotency_records_expires_at"):
            assert name in idx_names, f"Missing index: {name}"
