"""Tests for Task 6.5 — operational configuration ORM models.

Tests schema correctness — constraints, foreign keys, unique-active
rules, version semantics, and cross-tenant protection — without
requiring a live database.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Enum,
    Table,
    UniqueConstraint,
)

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.config import (
    AnalysisConfigModel,
    CameraConfigModel,
)

# =============================================================================
# Table existence
# =============================================================================


class TestTableExistence:
    """Verify all config tables are registered with Base.metadata."""

    def test_camera_configs_table_exists(self) -> None:
        assert "camera_configs" in Base.metadata.tables

    def test_analysis_configs_table_exists(self) -> None:
        assert "analysis_configs" in Base.metadata.tables

    def test_config_models_have_expected_table_names(self) -> None:
        assert CameraConfigModel.__tablename__ == "camera_configs"
        assert AnalysisConfigModel.__tablename__ == "analysis_configs"


# =============================================================================
# CameraConfig schema
# =============================================================================


class TestCameraConfigSchema:
    """Typed per-camera configuration columns and constraints."""

    def _table(self) -> Table:
        return Base.metadata.tables["camera_configs"]

    def test_primary_key_is_config_id(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["config_id"]
        assert str(table.columns["config_id"].type) == "UUID"

    def test_tenant_scope_columns_not_nullable(self) -> None:
        table = self._table()
        for col in ("camera_id", "venue_id", "tenant_id"):
            assert not table.columns[col].nullable, f"{col} must be NOT NULL"

    def test_status_enum_effective_state(self) -> None:
        table = self._table()
        col = table.columns["status"]
        assert isinstance(col.type, Enum)
        assert list(col.type.enums) == ["draft", "active", "archived"]

    def test_version_not_nullable(self) -> None:
        table = self._table()
        assert not table.columns["version"].nullable

    def test_typed_configuration_columns(self) -> None:
        """Configuration is typed — no generic key-value structure."""
        table = self._table()
        assert not table.columns["analysis_enabled"].nullable
        assert str(table.columns["frame_rate"].type) == "NUMERIC(7, 3)"
        assert str(table.columns["detection_sensitivity"].type) == "NUMERIC(4, 3)"

    def test_parameters_jsonb_is_optional(self) -> None:
        table = self._table()
        assert table.columns["parameters"].nullable
        assert "JSONB" in str(table.columns["parameters"].type)

    def test_timestamps_are_timestamptz(self) -> None:
        table = self._table()
        for col in ("created_at", "updated_at"):
            assert table.columns[col].type.timezone is True
            assert not table.columns[col].nullable

    def test_version_uniqueness_per_camera(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        version_uq = [
            uq for uq in uqs if [col.name for col in uq.columns] == ["camera_id", "version"]
        ]
        assert len(version_uq) >= 1

    def test_config_tenant_composite_uniqueness(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        tenant_uq = [
            uq for uq in uqs if [col.name for col in uq.columns] == ["config_id", "tenant_id"]
        ]
        assert len(tenant_uq) >= 1

    def test_unique_active_configuration_rule(self) -> None:
        """Partial unique index: at most one active config per camera."""
        table = self._table()
        partial = [idx for idx in table.indexes if idx.name == "uq_camera_configs_active"]
        assert len(partial) == 1
        assert partial[0].unique is True
        assert [col.name for col in partial[0].columns] == ["camera_id"]
        assert str(partial[0].dialect_options["postgresql"]["where"]) == "status = 'active'"

    def test_invalid_value_check_constraints(self) -> None:
        """CHECK constraints reject invalid configuration values."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_camera_configs_version_positive" in checks
        assert "ck_camera_configs_frame_rate_positive" in checks
        assert "ck_camera_configs_width_positive" in checks
        assert "ck_camera_configs_height_positive" in checks
        assert "ck_camera_configs_sensitivity_range" in checks

    def test_cross_tenant_camera_reference_prevented(self) -> None:
        """Composite FK (camera_id, tenant_id) -> cameras prevents
        referencing a camera of another tenant."""
        table = self._table()
        fks = table.foreign_key_constraints
        camera_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["camera_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["cameras", "cameras"]
        ]
        assert len(camera_fks) >= 1
        assert camera_fks[0].ondelete == "CASCADE"

    def test_venue_tenant_reference_prevented(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        venue_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["venue_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["venues", "venues"]
        ]
        assert len(venue_fks) >= 1
        assert venue_fks[0].ondelete == "CASCADE"

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        assert "ix_camera_configs_tenant_id" in idx_names
        assert "ix_camera_configs_venue_id" in idx_names
        # Task 6.13 review: camera_id-only lookups are served by
        # uq_camera_configs_version (camera_id, version) — no separate
        # single-column camera_id index (redundant, removed in 015).
        assert "ix_camera_configs_camera_id" not in idx_names
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["camera_id", "version"] for uq in uqs)


# =============================================================================
# AnalysisConfig schema
# =============================================================================


class TestAnalysisConfigSchema:
    """Typed per-venue analysis profile and threshold columns."""

    def _table(self) -> Table:
        return Base.metadata.tables["analysis_configs"]

    def test_primary_key_is_config_id(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["config_id"]

    def test_tenant_scope_columns_not_nullable(self) -> None:
        table = self._table()
        for col in ("venue_id", "tenant_id"):
            assert not table.columns[col].nullable, f"{col} must be NOT NULL"

    def test_profile_name_not_nullable(self) -> None:
        table = self._table()
        assert not table.columns["name"].nullable
        assert table.columns["name"].type.length == 100

    def test_typed_threshold_columns(self) -> None:
        """Thresholds are typed columns, not key-value rows."""
        table = self._table()
        for col in (
            "confidence_threshold",
            "frame_rate",
            "occupancy_threshold",
            "dwell_time_seconds",
            "queue_length_threshold",
            "wait_time_seconds",
        ):
            assert col in table.columns, f"Missing typed threshold column: {col}"

    def test_version_uniqueness_per_profile(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        version_uq = [
            uq for uq in uqs if [col.name for col in uq.columns] == ["venue_id", "name", "version"]
        ]
        assert len(version_uq) >= 1

    def test_unique_active_configuration_rule(self) -> None:
        """Partial unique index: at most one active profile per (venue, name)."""
        table = self._table()
        partial = [idx for idx in table.indexes if idx.name == "uq_analysis_configs_active"]
        assert len(partial) == 1
        assert partial[0].unique is True
        assert [col.name for col in partial[0].columns] == ["venue_id", "name"]
        assert str(partial[0].dialect_options["postgresql"]["where"]) == "status = 'active'"
        # Task 6.13 review: venue_id-only lookups are served by
        # uq_analysis_configs_version (venue_id, name, version) — no separate
        # single-column venue_id index (redundant, removed in 015).
        idx_names = {idx.name for idx in table.indexes}
        assert "ix_analysis_configs_venue_id" not in idx_names
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any(
            [col.name for col in uq.columns] == ["venue_id", "name", "version"] for uq in uqs
        )

    def test_threshold_check_constraints(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        for name in (
            "ck_analysis_configs_version_positive",
            "ck_analysis_configs_name_not_empty",
            "ck_analysis_configs_confidence_range",
            "ck_analysis_configs_frame_rate_positive",
            "ck_analysis_configs_occupancy_range",
            "ck_analysis_configs_dwell_non_negative",
            "ck_analysis_configs_queue_non_negative",
            "ck_analysis_configs_wait_non_negative",
        ):
            assert name in checks, f"Missing CHECK constraint: {name}"

    def test_venue_tenant_reference_prevented(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        venue_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["venue_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["venues", "venues"]
        ]
        assert len(venue_fks) >= 1
        assert venue_fks[0].ondelete == "CASCADE"

    def test_timestamps_are_timestamptz(self) -> None:
        table = self._table()
        for col in ("created_at", "updated_at"):
            assert table.columns[col].type.timezone is True


# =============================================================================
# Model instantiation and defaults
# =============================================================================


class TestModelDefaults:
    """Verify models accept defaults and can be constructed."""

    def test_camera_config_creation(self) -> None:
        cfg = CameraConfigModel(
            config_id=uuid.uuid4(),
            camera_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            version=1,
            status="active",
            analysis_enabled=False,
        )
        assert cfg.status == "active"
        assert cfg.analysis_enabled is False

    def test_camera_config_typed_values(self) -> None:
        cfg = CameraConfigModel(
            config_id=uuid.uuid4(),
            camera_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            version=2,
            status="draft",
            frame_rate=25,
            width=1920,
            height=1080,
            detection_sensitivity=Decimal("0.8"),
        )
        assert cfg.version == 2
        assert cfg.frame_rate == 25
        assert cfg.detection_sensitivity == Decimal("0.8")

    def test_analysis_config_creation(self) -> None:
        cfg = AnalysisConfigModel(
            config_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="lobby",
            version=1,
            status="active",
            occupancy_threshold=80,
            dwell_time_seconds=300,
        )
        assert cfg.name == "lobby"
        assert cfg.occupancy_threshold == 80
