"""Tests for Task 10 — configuration domain ORM models.

Verifies schema correctness — version-owned entity tables, composite
tenant FKs, lifecycle check constraints, session pinning column, and
cross-tenant reference protection — without requiring a live database.
Uses SQLAlchemy metadata inspection (same convention as test_config_models.py).
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.configuration import (
    CameraProfileEntityModel,
    ConfigurationModel,
    ConfigurationVersionModel,
    EntranceEntityModel,
    ExclusionROIEntityModel,
    PrivacyROIEntityModel,
    QueueAreaEntityModel,
    ServiceAreaEntityModel,
    TableEntityModel,
    ZoneEntityModel,
)

_VERSION_TABLES = {
    "config_camera_profiles",
    "config_zones",
    "config_tables",
    "config_entrances",
    "config_queue_areas",
    "config_service_areas",
    "config_privacy_rois",
    "config_exclusion_rois",
}

_ENTITY_MODELS = [
    CameraProfileEntityModel,
    ZoneEntityModel,
    TableEntityModel,
    EntranceEntityModel,
    QueueAreaEntityModel,
    ServiceAreaEntityModel,
    PrivacyROIEntityModel,
    ExclusionROIEntityModel,
]


class TestTableExistence:
    def test_configuration_tables_registered(self) -> None:
        for name in (
            "configurations",
            "configuration_versions",
            *_VERSION_TABLES,
        ):
            assert name in Base.metadata.tables, f"Missing table: {name}"

    def test_model_table_names(self) -> None:
        assert ConfigurationModel.__tablename__ == "configurations"
        assert ConfigurationVersionModel.__tablename__ == "configuration_versions"
        assert CameraProfileEntityModel.__tablename__ == "config_camera_profiles"
        assert ZoneEntityModel.__tablename__ == "config_zones"


class TestConfigurationSchema:
    def _table(self) -> Table:
        return Base.metadata.tables["configurations"]

    def test_one_config_per_tenant_venue(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["venue_id", "tenant_id"] for uq in uqs)

    def test_tenant_columns_not_nullable(self) -> None:
        table = self._table()
        for col in ("venue_id", "tenant_id"):
            assert not table.columns[col].nullable

    def test_venue_tenant_composite_fk(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        venue_fk = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["venue_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["venues", "venues"]
        ]
        assert len(venue_fk) == 1
        assert venue_fk[0].ondelete == "CASCADE"

    def test_current_published_pointer_is_nullable(self) -> None:
        table = self._table()
        assert table.columns["current_published_version_id"].nullable


class TestConfigurationVersionSchema:
    def _table(self) -> Table:
        return Base.metadata.tables["configuration_versions"]

    def test_lifecycle_status_check_constraint(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_config_versions_status" in checks
        assert "published" in checks["ck_config_versions_status"]
        assert "validating" in checks["ck_config_versions_status"]

    def test_published_complete_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_config_versions_published_complete" in checks
        assert "published_at" in checks["ck_config_versions_published_complete"]

    def test_monotonic_version_uniqueness(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any(
            [col.name for col in uq.columns] == ["configuration_id", "version"] for uq in uqs
        )

    def test_version_positive_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_config_versions_version_positive" in checks

    def test_self_replace_forbidden(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_config_versions_no_self_replace" in checks

    def test_configuration_composite_fk(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        cfg_fk = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["configuration_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["configurations", "configurations"]
        ]
        assert len(cfg_fk) == 1
        assert cfg_fk[0].ondelete == "CASCADE"


class TestEntityTables:
    """Every version-owned entity table shares the ownership shape."""

    def test_entity_tables_have_direct_tenant_ownership(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            for col in ("configuration_version_id", "venue_id", "tenant_id", "profile_id"):
                assert not table.columns[col].nullable, f"{name}.{col} must be NOT NULL"

    def test_entity_tables_reference_same_version_tenant(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            fks = table.foreign_key_constraints
            version_fk = [
                fk
                for fk in fks
                if [c.name for c in fk.columns] == ["configuration_version_id", "tenant_id"]
                and [e.column.table.name for e in fk.elements]
                == ["configuration_versions", "configuration_versions"]
            ]
            assert len(version_fk) == 1, f"{name} missing same-version ownership FK"
            assert version_fk[0].ondelete == "CASCADE"

    def test_entity_tables_have_venue_tenant_fk(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            fks = table.foreign_key_constraints
            venue_fk = [
                fk
                for fk in fks
                if [c.name for c in fk.columns] == ["venue_id", "tenant_id"]
                and [e.column.table.name for e in fk.elements] == ["venues", "venues"]
            ]
            assert len(venue_fk) == 1, f"{name} missing venue composite FK"

    def test_entity_profile_id_unique_per_version(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
            assert any(
                [col.name for col in uq.columns] == ["configuration_version_id", "profile_id"]
                for uq in uqs
            ), f"{name} missing version+profile uniqueness"

    def test_entity_geometry_stored_as_jsonb(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            assert "JSONB" in str(table.columns["geometry"].type), f"{name}.geometry type"
        # Camera physical placement is optional (nullable); all other
        # entity geometries are required.
        cam = Base.metadata.tables["config_camera_profiles"]
        assert cam.columns["geometry"].nullable
        for name in _VERSION_TABLES - {"config_camera_profiles"}:
            assert not Base.metadata.tables[name].columns["geometry"].nullable, (
                f"{name}.geometry must be NOT NULL"
            )

    def test_entity_coordinate_and_type_checks(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            checks = {
                c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
            }
            assert f"ck_{name}_coordinate_space" in checks
            assert f"ck_{name}_geometry_type" in checks
            assert f"ck_{name}_profile_id_not_empty" in checks

    def test_entity_spatial_indexes(self) -> None:
        for name in _VERSION_TABLES:
            table = Base.metadata.tables[name]
            # GIST indexes are created in the migration (expression
            # indexes cannot be declared in __table_args__); verify the
            # plain column indexes are declared on the model.
            idx_names = {idx.name for idx in table.indexes}
            assert f"ix_{name}_tenant_id" in idx_names
            assert f"ix_{name}_venue_id" in idx_names
            assert f"ix_{name}_version_id" in idx_names


class TestCameraProfileTable:
    def _table(self) -> Table:
        return Base.metadata.tables["config_camera_profiles"]

    def test_camera_composite_fk(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        cam_fk = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["camera_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["cameras", "cameras"]
        ]
        assert len(cam_fk) == 1
        assert cam_fk[0].ondelete == "CASCADE"

    def test_resolution_checks(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_config_camera_profiles_resolution" in checks
        assert "ck_config_camera_profiles_fps_positive" in checks


class TestSessionPinning:
    def test_video_sessions_has_configuration_version_column(self) -> None:
        table = Base.metadata.tables["video_sessions"]
        assert "configuration_version_id" in table.columns

    def test_session_pin_fk_targets_version_tenant(self) -> None:
        table = Base.metadata.tables["video_sessions"]
        fks = table.foreign_key_constraints
        pin_fk = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["configuration_version_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements]
            == ["configuration_versions", "configuration_versions"]
        ]
        assert len(pin_fk) == 1
        assert pin_fk[0].ondelete == "RESTRICT"

    def test_session_pin_index(self) -> None:
        table = Base.metadata.tables["video_sessions"]
        idx_names = {idx.name for idx in table.indexes}
        assert "ix_video_sessions_config_version_id" in idx_names


class TestModelConstruction:
    def test_configuration_model_construction(self) -> None:
        cfg = ConfigurationModel(
            configuration_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Main Venue",
        )
        assert cfg.name == "Main Venue"

    def test_version_model_default_status(self) -> None:
        v = ConfigurationVersionModel(
            configuration_version_id=uuid.uuid4(),
            configuration_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            version=1,
            status="draft",
        )
        assert v.status == "draft"

    def test_version_model_published_requires_metadata(self) -> None:
        from datetime import UTC, datetime

        v = ConfigurationVersionModel(
            configuration_version_id=uuid.uuid4(),
            configuration_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            version=1,
            status="published",
            validated_at=datetime.now(UTC),
            validated_by="u1",
            published_at=datetime.now(UTC),
            published_by="u1",
        )
        assert v.status == "published"

    def test_zone_entity_model_construction(self) -> None:
        z = ZoneEntityModel(
            entity_id=uuid.uuid4(),
            configuration_version_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            profile_id="z1",
            name="Lobby",
            zone_type="lobby",
            geometry={"geometry_type": "polygon"},
            coordinate_space="venue_local",
            geometry_type="polygon",
        )
        assert z.profile_id == "z1"
