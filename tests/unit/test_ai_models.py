"""Tests for Task 6.9 — AI domain persistence ORM models.

Tests schema correctness — finding identity/evidence linkage, the
recommendation contract fields (priority, opportunity link), review
workflow status columns, versioning, composite FKs, and the M2M link
table — without requiring a live database. Status transitions and
migration behavior are exercised by the integration tests against a
real TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, Table, UniqueConstraint

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.ai import (
    FindingModel,
    RecommendationModel,
)


class TestFindingsSchema:
    """One row per evidence-grounded finding — the Finding contract."""

    def _table(self) -> Table:
        return Base.metadata.tables["findings"]

    def test_table_exists(self) -> None:
        assert "findings" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert FindingModel.__tablename__ == "findings"

    def test_finding_identity_and_contract_fields(self) -> None:
        """finding_id PK; finding_type/description required; confidence
        optional; event_time explicit (the Finding contract)."""
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["finding_id"]
        assert not table.columns["finding_type"].nullable
        assert not table.columns["description"].nullable
        assert table.columns["confidence"].nullable
        assert not table.columns["event_time"].nullable
        assert table.columns["event_time"].type.timezone is True

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable

    def test_status_is_workflow_enum(self) -> None:
        """Findings carry DB-level review workflow state, not a free string."""
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"proposed", "accepted", "rejected", "archived"}
        assert not col.nullable

    def test_version_and_model_metadata(self) -> None:
        table = self._table()
        assert not table.columns["schema_version"].nullable
        assert table.columns["model_name"].nullable
        assert table.columns["model_version"].nullable

    def test_evidence_package_link_is_composite_fk(self) -> None:
        """The evidence linkage is a real composite FK (package + tenant)."""
        table = self._table()
        fks = table.foreign_key_constraints
        evidence_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements]
            == ["evidence_packages", "evidence_packages"]
        ]
        assert len(evidence_fks) == 1
        assert [c.name for c in evidence_fks[0].columns] == [
            "evidence_package_id",
            "tenant_id",
        ]
        # RESTRICT — evidence cited by a derived finding is never silently
        # destroyed (a composite FK with tenant_id cannot SET NULL).
        assert evidence_fks[0].ondelete == "RESTRICT"

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

    def test_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        for name in (
            "ck_findings_finding_type_not_empty",
            "ck_findings_description_not_empty",
            "ck_findings_confidence_range",
            "ck_findings_updated_not_before_created",
        ):
            assert name in checks, f"Missing check: {name}"
        assert "confidence <= 1" in checks["ck_findings_confidence_range"]

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_findings_tenant_id",
            "ix_findings_venue_id",
            "ix_findings_status",
            "ix_findings_event_time",
            "ix_findings_evidence_package_id",
        ):
            assert name in idx_names, f"Missing index: {name}"

    def test_finding_tenant_unique_target(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["finding_id", "tenant_id"] for uq in uqs)


class TestRecommendationsSchema:
    """One row per evidence-grounded proposed action — the Recommendation
    contract."""

    def _table(self) -> Table:
        return Base.metadata.tables["recommendations"]

    def test_table_exists(self) -> None:
        assert "recommendations" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert RecommendationModel.__tablename__ == "recommendations"

    def test_recommendation_identity_and_contract_fields(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["recommendation_id"]
        assert not table.columns["description"].nullable
        assert table.columns["opportunity_id"].nullable

    def test_priority_is_contract_enum(self) -> None:
        """Priority matches contracts/intelligence/models.py Priority."""
        table = self._table()
        col = table.columns["priority"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {"high", "medium", "low"}
        assert not col.nullable

    def test_status_is_workflow_enum(self) -> None:
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert set(col.type.enums) == {
            "pending",
            "accepted",
            "rejected",
            "implemented",
            "archived",
        }
        assert not col.nullable

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable

    def test_version_and_model_metadata(self) -> None:
        table = self._table()
        assert not table.columns["schema_version"].nullable
        assert table.columns["model_name"].nullable
        assert table.columns["model_version"].nullable

    def test_opportunity_link_is_composite_fk(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        opp_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["opportunities", "opportunities"]
        ]
        assert len(opp_fks) == 1
        assert [c.name for c in opp_fks[0].columns] == ["opportunity_id", "tenant_id"]
        # RESTRICT — a recommendation citing an opportunity blocks deletion.
        assert opp_fks[0].ondelete == "RESTRICT"

    def test_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_recommendations_description_not_empty" in checks
        assert "ck_recommendations_updated_not_before_created" in checks

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_recommendations_tenant_id",
            "ix_recommendations_venue_id",
            "ix_recommendations_status",
            "ix_recommendations_created_at",
        ):
            assert name in idx_names, f"Missing index: {name}"

    def test_recommendation_tenant_unique_target(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any(
            [col.name for col in uq.columns] == ["recommendation_id", "tenant_id"] for uq in uqs
        )


class TestRecommendationFindingsLink:
    """M2M: which findings support a recommendation (contract finding_ids)."""

    def test_link_table(self) -> None:
        table = Base.metadata.tables["recommendation_findings"]
        assert [c.name for c in table.primary_key.columns] == [
            "recommendation_id",
            "finding_id",
        ]
        assert not table.columns["tenant_id"].nullable

    def test_composite_fks(self) -> None:
        table = Base.metadata.tables["recommendation_findings"]
        fks = table.foreign_key_constraints
        rec_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["recommendations", "recommendations"]
        ]
        finding_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["findings", "findings"]
        ]
        assert len(rec_fks) == 1
        assert [c.name for c in rec_fks[0].columns] == ["recommendation_id", "tenant_id"]
        assert rec_fks[0].ondelete == "CASCADE"
        assert len(finding_fks) == 1
        assert [c.name for c in finding_fks[0].columns] == ["finding_id", "tenant_id"]
        assert finding_fks[0].ondelete == "CASCADE"

    def test_finding_first_index(self) -> None:
        table = Base.metadata.tables["recommendation_findings"]
        idx_names = {idx.name for idx in table.indexes}
        assert "ix_recommendation_findings_finding_id" in idx_names
