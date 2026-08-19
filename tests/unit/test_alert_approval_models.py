"""Tests for Task 6.10 — alert & approval persistence ORM models.

Tests schema correctness — the Alert contract fields, explicit state
enums (never boolean flags), polymorphic source refs as real composite
FKs, the ApprovalRequest contract (request/actor/subject/state/
timestamps), and the append-only ApprovalDecision history with the
duplicate-approval partial unique index — without requiring a live
database. State-transition legality and RLS are exercised by the
integration tests against a real TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, Table, UniqueConstraint

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.alerts_approvals import (
    AlertModel,
    ApprovalDecisionModel,
    ApprovalRequestModel,
)


class TestAlertsSchema:
    """One row per operational signal — the Alert contract."""

    def _table(self) -> Table:
        return Base.metadata.tables["alerts"]

    def test_table_exists(self) -> None:
        assert "alerts" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert AlertModel.__tablename__ == "alerts"

    def test_alert_identity_and_contract_fields(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["alert_id"]
        assert not table.columns["alert_type"].nullable
        assert not table.columns["title"].nullable
        assert not table.columns["description"].nullable
        assert not table.columns["event_time"].nullable
        assert table.columns["event_time"].type.timezone is True

    def test_severity_is_contract_enum(self) -> None:
        """Severity matches contracts/operations/models.py Severity."""
        table = self._table()
        col = table.columns["severity"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"critical", "high", "medium", "low", "info"}
        assert not col.nullable

    def test_status_is_explicit_lifecycle_enum_not_booleans(self) -> None:
        """State is a single enum — no is_approved/is_rejected/is_pending."""
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"raised", "acknowledged", "resolved", "expired"}
        assert not col.nullable
        # No boolean state columns exist.
        names = {c.name for c in table.columns}
        assert "is_approved" not in names
        assert "is_rejected" not in names
        assert "is_pending" not in names

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable

    def test_source_refs_are_real_composite_fks(self) -> None:
        """Polymorphic source_ref is two real composite FKs, at most one set."""
        table = self._table()
        fks = table.foreign_key_constraints
        finding_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["findings", "findings"]
        ]
        recommendation_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["recommendations", "recommendations"]
        ]
        assert len(finding_fks) == 1
        assert [c.name for c in finding_fks[0].columns] == ["finding_id", "tenant_id"]
        assert len(recommendation_fks) == 1
        assert [c.name for c in recommendation_fks[0].columns] == [
            "recommendation_id",
            "tenant_id",
        ]

    def test_source_single_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_alerts_source_single" in checks
        assert "recommendation_id IS NULL" in checks["ck_alerts_source_single"]

    def test_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        for name in (
            "ck_alerts_alert_type_not_empty",
            "ck_alerts_title_not_empty",
            "ck_alerts_description_not_empty",
            "ck_alerts_updated_not_before_created",
        ):
            assert name in checks, f"Missing check: {name}"

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

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_alerts_tenant_id",
            "ix_alerts_venue_id",
            "ix_alerts_status",
            "ix_alerts_event_time",
            "ix_alerts_finding_id",
            "ix_alerts_recommendation_id",
        ):
            assert name in idx_names, f"Missing index: {name}"


class TestApprovalRequestsSchema:
    """The ApprovalRequest contract — request/actor/subject/state/timestamps."""

    def _table(self) -> Table:
        return Base.metadata.tables["approval_requests"]

    def test_table_exists(self) -> None:
        assert "approval_requests" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert ApprovalRequestModel.__tablename__ == "approval_requests"

    def test_request_identity_and_contract_fields(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["request_id"]
        assert not table.columns["recommendation_id"].nullable  # subject
        assert not table.columns["requested_by"].nullable  # actor/context
        assert not table.columns["requested_at"].nullable
        assert table.columns["requested_at"].type.timezone is True
        assert table.columns["resolved_at"].nullable
        assert table.columns["reason"].nullable

    def test_status_is_explicit_contract_enum(self) -> None:
        """State is the contract ApprovalStatus enum — no boolean combos."""
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"pending", "approved", "rejected", "cancelled"}
        assert not col.nullable
        names = {c.name for c in table.columns}
        assert "is_approved" not in names
        assert "is_rejected" not in names

    def test_tenant_ownership_direct(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable

    def test_subject_is_composite_fk(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        rec_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["recommendations", "recommendations"]
        ]
        assert len(rec_fks) == 1
        assert [c.name for c in rec_fks[0].columns] == ["recommendation_id", "tenant_id"]
        assert rec_fks[0].ondelete == "CASCADE"

    def test_requested_by_fk_to_users(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        actor_fks = [fk for fk in fks if [e.column.table.name for e in fk.elements] == ["users"]]
        assert len(actor_fks) == 1
        assert [c.name for c in actor_fks[0].columns] == ["requested_by"]
        assert actor_fks[0].ondelete == "RESTRICT"

    def test_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        for name in (
            "ck_approval_requests_reason_not_empty",
            "ck_approval_requests_resolved_after_requested",
            "ck_approval_requests_updated_not_before_created",
        ):
            assert name in checks, f"Missing check: {name}"
        assert (
            "resolved_at >= requested_at" in checks["ck_approval_requests_resolved_after_requested"]
        )

    def test_request_tenant_unique_target(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["request_id", "tenant_id"] for uq in uqs)

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_approval_requests_tenant_id",
            "ix_approval_requests_status",
            "ix_approval_requests_recommendation_id",
            "ix_approval_requests_requested_at",
        ):
            assert name in idx_names, f"Missing index: {name}"


class TestApprovalDecisionsSchema:
    """Append-only decision history with a duplicate-approval guard."""

    def _table(self) -> Table:
        return Base.metadata.tables["approval_decisions"]

    def test_table_exists(self) -> None:
        assert "approval_decisions" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert ApprovalDecisionModel.__tablename__ == "approval_decisions"

    def test_decision_fields(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["decision_id"]
        assert not table.columns["request_id"].nullable
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["actor_id"].nullable
        assert not table.columns["decision"].nullable
        assert not table.columns["decided_at"].nullable
        col = table.columns["decision"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"approved", "rejected", "cancelled"}

    def test_request_composite_fk_cascade(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        req_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements]
            == ["approval_requests", "approval_requests"]
        ]
        assert len(req_fks) == 1
        assert [c.name for c in req_fks[0].columns] == ["request_id", "tenant_id"]
        assert req_fks[0].ondelete == "CASCADE"

    def test_duplicate_approval_guard_partial_unique_index(self) -> None:
        """At most one terminal decision per request — a partial unique index."""
        table = self._table()
        matches = [
            idx
            for idx in table.indexes
            if idx.name == "uq_approval_decisions_terminal" and idx.unique
        ]
        assert len(matches) == 1
        assert [c.name for c in matches[0].columns] == ["request_id"]
        assert matches[0].dialect_options["postgresql"]["where"] is not None

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_approval_decisions_request_id",
            "ix_approval_decisions_actor_id",
            "ix_approval_decisions_decided_at",
        ):
            assert name in idx_names, f"Missing index: {name}"
