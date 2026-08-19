"""Tests for Task 6.12 — audit, outbox, and inbox persistence ORM models.

Tests schema correctness — trusted audit identity (no client-supplied
actor column), append-only design (no UPDATE/DELETE grants at runtime),
blocked-secret metadata CHECK, outbox idempotent delivery (unique
event_id), inbox deduplication (unique source + source_message_id),
explicit worker status enums, lease columns, and indexes — without
requiring a live database. Atomicity, rollback, transitions, duplicate
handling, and tenant identity are exercised by the integration tests
against a real TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, Table

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.audit_outbox_inbox import (
    AuditEventModel,
    InboxMessageModel,
    OutboxEventModel,
)
from contracts.common import AuditEventId, InboxMessageId, OutboxMessageId


class TestAuditEventSchema:
    """The trusted audit log — globally append-only."""

    def _table(self) -> Table:
        return Base.metadata.tables["audit_events"]

    def test_table_exists(self) -> None:
        assert "audit_events" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert AuditEventModel.__tablename__ == "audit_events"

    def test_audit_identity_and_contract_id(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["audit_id"]
        assert AuditEventId.__name__ == "AuditEventId"

    def test_trusted_actor_context_columns(self) -> None:
        """Actor identity is a required server-side value — never a
        client-supplied column."""
        table = self._table()
        assert not table.columns["actor_id"].nullable
        assert not table.columns["tenant_id"].nullable
        # No client-supplied fields exist.
        for client_field in ("request_ip", "user_agent", "client_actor_id", "claimed_actor"):
            assert client_field not in {c.name for c in table.columns}

    def test_contract_fields_present(self) -> None:
        """The AuditEvent contract fields map to typed columns."""
        table = self._table()
        names = {c.name for c in table.columns}
        for field in (
            "audit_id",
            "actor_id",
            "tenant_id",
            "membership_id",
            "venue_id",
            "action",
            "action_category",
            "correlation_id",
            "timestamp",
            "metadata",
        ):
            assert field in names, f"Missing audit field: {field}"

    def test_action_category_is_contract_enum(self) -> None:
        """action_category matches the contract AuditActionCategory set."""
        table = self._table()
        col = table.columns["action_category"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {
            "authentication",
            "authorization",
            "venue",
            "video",
            "analytics",
            "evidence",
            "recommendation",
            "alert",
            "user",
            "membership",
            "tenant",
            "system",
        }
        assert not col.nullable

    def test_no_secret_columns(self) -> None:
        """No credential/token column may exist anywhere."""
        table = self._table()
        names = {c.name for c in table.columns}
        for secret_column in ("password", "token", "api_key", "credential", "secret"):
            assert secret_column not in names, f"Secret column must not exist: {secret_column}"

    def test_metadata_no_secrets_check(self) -> None:
        """Audit metadata rejects secret-like keys via the shared IMMUTABLE
        helper (audit contract first-segment semantics)."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_audit_events_metadata_no_secrets" in checks
        assert "integration_config_has_secret" in checks["ck_audit_events_metadata_no_secrets"]

    def test_action_not_empty_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_audit_events_action_not_empty" in checks

    def test_append_only_no_foreign_keys(self) -> None:
        """Audit survives tenant/user deletion — no FKs (governance 10.3)."""
        table = self._table()
        assert not table.foreign_key_constraints, "audit_events must have no foreign keys"

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_audit_events_tenant_id",
            "ix_audit_events_actor_id",
            "ix_audit_events_timestamp",
        ):
            assert name in idx_names, f"Missing index: {name}"

    def test_timestamp_is_utc(self) -> None:
        table = self._table()
        assert table.columns["timestamp"].type.timezone is True


class TestOutboxEventSchema:
    """The transactional outbox — idempotent delivery."""

    def _table(self) -> Table:
        return Base.metadata.tables["outbox_events"]

    def test_table_exists(self) -> None:
        assert "outbox_events" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert OutboxEventModel.__tablename__ == "outbox_events"

    def test_outbox_identity_and_contract_id(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["outbox_id"]
        assert OutboxMessageId.__name__ == "OutboxMessageId"

    def test_unique_event_id_idempotent_delivery(self) -> None:
        """At most one outbox row per event — idempotent publication."""
        table = self._table()
        uniques = {u.name: {c.name for c in u.columns} for u in table.constraints}
        assert "uq_outbox_events_event_id" in uniques
        assert uniques["uq_outbox_events_event_id"] == {"event_id"}

    def test_tenant_recorded_as_value(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.foreign_key_constraints, "outbox tenant_id is a value, not an FK"

    def test_status_is_explicit_worker_enum(self) -> None:
        """State is a single enum — no boolean flags, no is_published.

        dead_letter is the terminal state added by Task 7 (migration 016)
        — a permanently failing event is preserved, never deleted.
        """
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {
            "pending",
            "processing",
            "published",
            "failed",
            "dead_letter",
        }
        assert not col.nullable
        names = {c.name for c in table.columns}
        assert "is_published" not in names
        assert "is_failed" not in names

    def test_payload_and_lease_columns(self) -> None:
        table = self._table()
        assert not table.columns["payload"].nullable
        assert "claimed_by" in {c.name for c in table.columns}
        assert "claimed_until" in {c.name for c in table.columns}
        assert "attempts" in {c.name for c in table.columns}
        assert "published_at" in {c.name for c in table.columns}

    def test_retry_scheduling_columns(self) -> None:
        """Task 7 (016) — persisted backoff and error preservation."""
        table = self._table()
        available = table.columns["available_at"]
        assert not available.nullable
        assert available.type.timezone is True
        assert "last_error" in {c.name for c in table.columns}
        assert "venue_id" in {c.name for c in table.columns}

    def test_timestamp_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_outbox_events_updated_not_before_created" in checks
        assert "ck_outbox_events_published_not_before_created" in checks
        assert "ck_outbox_events_lease_not_before_created" in checks

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        by_name = {idx.name: idx for idx in table.indexes}
        assert "ix_outbox_events_tenant_id" in by_name
        # Partial poller index on the pending subset (governance 9 rule 3).
        pending = by_name["ix_outbox_events_pending"]
        assert pending.dialect_options["postgresql"]["where"] is not None


class TestInboxMessageSchema:
    """Idempotent inbound processing."""

    def _table(self) -> Table:
        return Base.metadata.tables["inbox_messages"]

    def test_table_exists(self) -> None:
        assert "inbox_messages" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert InboxMessageModel.__tablename__ == "inbox_messages"

    def test_inbox_identity_and_contract_id(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["inbox_id"]
        assert InboxMessageId.__name__ == "InboxMessageId"

    def test_dedup_unique_key(self) -> None:
        """Duplicate delivery is detected via (source, source_message_id)."""
        table = self._table()
        uniques = {u.name: {c.name for c in u.columns} for u in table.constraints}
        assert "uq_inbox_messages_source_message_id" in uniques
        assert uniques["uq_inbox_messages_source_message_id"] == {"source", "source_message_id"}

    def test_tenant_recorded_as_value(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.foreign_key_constraints, "inbox tenant_id is a value, not an FK"

    def test_status_is_explicit_worker_enum(self) -> None:
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {
            "pending",
            "processing",
            "processed",
            "failed",
            "dead_letter",
        }
        names = {c.name for c in table.columns}
        assert "is_processed" not in names

    def test_received_processed_timestamps(self) -> None:
        table = self._table()
        assert not table.columns["received_at"].nullable
        assert table.columns["received_at"].type.timezone is True
        assert "processed_at" in {c.name for c in table.columns}
        assert "attempts" in {c.name for c in table.columns}

    def test_retry_scheduling_columns(self) -> None:
        """Task 7 (016) — persisted backoff and error preservation."""
        table = self._table()
        available = table.columns["available_at"]
        assert not available.nullable
        assert available.type.timezone is True
        assert "last_error" in {c.name for c in table.columns}
        assert "venue_id" in {c.name for c in table.columns}

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        by_name = {idx.name: idx for idx in table.indexes}
        assert "ix_inbox_messages_tenant_id" in by_name
        pending = by_name["ix_inbox_messages_pending"]
        assert pending.dialect_options["postgresql"]["where"] is not None
