"""Tests for Task 6.8 — analytics persistence ORM models.

Tests schema correctness — metric identity/value columns, the hypertable
composite PK, window semantics, composite FKs, the relational
opportunities table, and the M2M link tables — without requiring a live
database. Hypertable conversion and aggregation behavior are exercised by
the integration tests against a real TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.analytics import (
    MetricModel,
    OpportunityModel,
)


class TestMetricsSchema:
    """One row per derived metric sample — the MetricValue contract."""

    def _table(self) -> Table:
        return Base.metadata.tables["metrics"]

    def test_table_exists(self) -> None:
        assert "metrics" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert MetricModel.__tablename__ == "metrics"

    def test_primary_key_is_composite_time_metric(self) -> None:
        """The hypertable PK includes the partition column (TimescaleDB)."""
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["event_time", "metric_id"]

    def test_metric_identity_and_value(self) -> None:
        """Metric identity is explicit: metric_name (business) + metric_id;
        value and unit are typed columns."""
        table = self._table()
        assert not table.columns["metric_name"].nullable
        assert table.columns["metric_name"].type.length == 100
        assert not table.columns["value"].nullable
        assert "DOUBLE" in str(table.columns["value"].type).upper()
        assert table.columns["unit"].nullable

    def test_event_time_is_explicit_utc(self) -> None:
        """The sample/effective time is explicit — never created_at."""
        table = self._table()
        assert not table.columns["event_time"].nullable
        assert table.columns["event_time"].type.timezone is True

    def test_aggregation_window_columns(self) -> None:
        table = self._table()
        assert table.columns["window_start"].nullable
        assert table.columns["window_end"].nullable
        assert table.columns["window_start"].type.timezone is True

    def test_ingestion_time_distinct_from_event_time(self) -> None:
        table = self._table()
        assert not table.columns["ingestion_time"].nullable
        assert table.columns["ingestion_time"].type.timezone is True

    def test_tenant_venue_and_dimensions(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable
        assert table.columns["session_id"].nullable
        assert table.columns["camera_id"].nullable

    def test_source_ref_forward_reference(self) -> None:
        """AnalysisJobId is a bare UUID forward reference (no jobs table)."""
        table = self._table()
        assert table.columns["source_ref"].nullable

    def test_metric_name_not_empty_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_metrics_metric_name_not_empty" in checks
        assert "ck_metrics_unit_not_empty" in checks

    def test_window_pair_check(self) -> None:
        """Aggregation window: both columns or neither, and ordered."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "window_end >= window_start" in checks["ck_metrics_window_ordered"]
        assert "ck_metrics_ingestion_not_before_event" in checks

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

    def test_session_and_camera_composite_fks(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        session_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["session_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["video_sessions", "video_sessions"]
        ]
        camera_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["camera_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["cameras", "cameras"]
        ]
        assert len(session_fks) == 1
        assert len(camera_fks) == 1

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_metrics_tenant_time",
            "ix_metrics_venue_time",
            "ix_metrics_name_time",
            "ix_metrics_session_id",
            "ix_metrics_camera_id",
        ):
            assert name in idx_names, f"Missing index: {name}"


class TestOpportunitiesSchema:
    """Relational opportunity records — the OpportunityCandidate contract."""

    def _table(self) -> Table:
        return Base.metadata.tables["opportunities"]

    def test_table_exists(self) -> None:
        assert "opportunities" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert OpportunityModel.__tablename__ == "opportunities"

    def test_single_column_pk(self) -> None:
        """Opportunities are relational (NOT a hypertable)."""
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["opportunity_id"]
        assert not table.columns["description"].nullable
        assert not table.columns["event_time"].nullable
        assert table.columns["event_time"].type.timezone is True

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable

    def test_description_not_empty_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_opportunities_description_not_empty" in checks

    def test_opportunity_tenant_unique_target(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any(
            [col.name for col in uq.columns] == ["opportunity_id", "tenant_id"] for uq in uqs
        )


class TestOpportunityLinks:
    """M2M links: metric samples (hypertable PK pair) and evidence refs."""

    def test_opportunity_metrics_table(self) -> None:
        table = Base.metadata.tables["opportunity_metrics"]
        assert [c.name for c in table.primary_key.columns] == [
            "opportunity_id",
            "event_time",
            "metric_id",
        ]
        assert not table.columns["tenant_id"].nullable
        # The metric side references the hypertable PK pair — the only
        # FK target possible on a hypertable.
        fks = table.foreign_key_constraints
        metric_fks = [
            fk for fk in fks if [e.column.table.name for e in fk.elements] == ["metrics", "metrics"]
        ]
        assert len(metric_fks) == 1
        assert [c.name for c in metric_fks[0].columns] == ["event_time", "metric_id"]
        assert metric_fks[0].ondelete == "CASCADE"

    def test_opportunity_evidence_refs_table(self) -> None:
        table = Base.metadata.tables["opportunity_evidence_refs"]
        assert [c.name for c in table.primary_key.columns] == ["opportunity_id", "ref_id"]
        assert not table.columns["tenant_id"].nullable
        fks = table.foreign_key_constraints
        ref_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["evidence_refs", "evidence_refs"]
        ]
        assert len(ref_fks) == 1
        assert ref_fks[0].ondelete == "CASCADE"
