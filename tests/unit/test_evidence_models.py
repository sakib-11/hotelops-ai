"""Tests for Task 6.7 — evidence persistence ORM models.

Tests schema correctness — columns, typed artifact references, checksum
validation, composite FKs, the package/ref association, and the
video_assets.evidence_ref FK — without requiring a live database.
Migration-level behavior is exercised by the integration tests against a
real TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.evidence import (
    EvidencePackageModel,
    EvidenceRefModel,
)


class TestEvidenceRefsSchema:
    """One row per artifact reference — bytes live in object storage."""

    def _table(self) -> Table:
        return Base.metadata.tables["evidence_refs"]

    def test_table_exists(self) -> None:
        assert "evidence_refs" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert EvidenceRefModel.__tablename__ == "evidence_refs"

    def test_primary_key_is_ref_id(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["ref_id"]
        assert str(table.columns["ref_id"].type) == "UUID"

    def test_artifact_reference_columns(self) -> None:
        """Object key, artifact type, and content metadata are typed."""
        table = self._table()
        assert not table.columns["ref_uri"].nullable
        assert table.columns["ref_uri"].type.length == 2048
        assert not table.columns["ref_type"].nullable
        assert not table.columns["schema_version"].nullable
        assert table.columns["content_type"].nullable
        assert table.columns["size_bytes"].nullable
        assert table.columns["checksum"].nullable

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable

    def test_capture_event_relationship_columns(self) -> None:
        """The capture/event relationship is explicit: source event pair,
        video session, camera, and capture time."""
        table = self._table()
        assert table.columns["event_id"].nullable
        assert table.columns["event_time"].nullable
        assert table.columns["session_id"].nullable
        assert table.columns["camera_id"].nullable
        assert table.columns["captured_at"].nullable
        assert table.columns["captured_at"].type.timezone is True

    def test_timestamps_are_utc(self) -> None:
        table = self._table()
        assert table.columns["created_at"].type.timezone is True
        assert table.columns["event_time"].type.timezone is True

    def test_artifact_reference_check_constraints(self) -> None:
        """ref_uri is non-empty; checksum must be a sha256 hex digest."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_evidence_refs_uri_not_empty" in checks
        assert "ck_evidence_refs_checksum_sha256" in checks
        assert "ck_evidence_refs_size_non_negative" in checks
        assert "ck_evidence_refs_event_pair" in checks

    def test_event_pair_check(self) -> None:
        """The event FK columns must be both present or both absent."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert (
            "event_id IS NOT NULL AND event_time IS NOT NULL"
            in checks["ck_evidence_refs_event_pair"]
        )

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

    def test_event_composite_fk_to_hypertable(self) -> None:
        """The event link references the operational_events hypertable PK
        (event_time, event_id) — the only possible target on a hypertable."""
        table = self._table()
        fks = table.foreign_key_constraints
        event_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["event_time", "event_id"]
            and [e.column.table.name for e in fk.elements]
            == [
                "operational_events",
                "operational_events",
            ]
        ]
        assert len(event_fks) == 1
        assert event_fks[0].ondelete == "SET NULL"

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
        assert session_fks[0].ondelete == "CASCADE"
        assert len(camera_fks) == 1

    def test_ref_tenant_unique_target(self) -> None:
        """(ref_id, tenant_id) is the composite FK target for links/assets."""
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["ref_id", "tenant_id"] for uq in uqs)

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_evidence_refs_tenant_id",
            "ix_evidence_refs_venue_id",
            "ix_evidence_refs_session_id",
            "ix_evidence_refs_event_id",
            "ix_evidence_refs_captured_at",
        ):
            assert name in idx_names, f"Missing index: {name}"


class TestEvidencePackagesSchema:
    """Bounded evidence collections (the EvidencePackage contract)."""

    def _table(self) -> Table:
        return Base.metadata.tables["evidence_packages"]

    def test_table_exists(self) -> None:
        assert "evidence_packages" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert EvidencePackageModel.__tablename__ == "evidence_packages"

    def test_tenant_venue_ownership(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable
        assert table.columns["description"].nullable
        assert table.columns["created_at"].type.timezone is True

    def test_description_not_empty_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_evidence_packages_description_not_empty" in checks

    def test_package_tenant_unique_target(self) -> None:
        table = self._table()
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["package_id", "tenant_id"] for uq in uqs)


class TestPackageEvidenceRefsAssociation:
    """M2M join between packages and refs (membership_venues pattern)."""

    def _table(self) -> Table:
        return Base.metadata.tables["package_evidence_refs"]

    def test_table_exists(self) -> None:
        assert "package_evidence_refs" in Base.metadata.tables

    def test_composite_pk_and_tenant(self) -> None:
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["package_id", "ref_id"]
        assert not table.columns["tenant_id"].nullable

    def test_composite_fks_prevent_cross_tenant_links(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        package_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements]
            == ["evidence_packages", "evidence_packages"]
        ]
        ref_fks = [
            fk
            for fk in fks
            if [e.column.table.name for e in fk.elements] == ["evidence_refs", "evidence_refs"]
        ]
        assert len(package_fks) == 1
        assert package_fks[0].ondelete == "CASCADE"
        assert len(ref_fks) == 1
        assert ref_fks[0].ondelete == "CASCADE"


class TestVideoAssetEvidenceForwardRef:
    """video_assets.evidence_ref stays a bare-UUID forward reference.

    Wiring a real FK would create a dependency cycle (evidence_refs ->
    video_sessions -> video_assets -> evidence_refs) that SQLAlchemy cannot
    sort — documented in migration 009. Provenance to video context flows
    through evidence_refs.session_id / camera_id instead.
    """

    def test_evidence_ref_has_no_fk(self) -> None:
        table = Base.metadata.tables["video_assets"]
        evidence_fks = [
            fk
            for fk in table.foreign_key_constraints
            if [c.name for c in fk.columns] == ["evidence_ref", "tenant_id"]
        ]
        assert evidence_fks == [], "evidence_ref must stay a bare UUID (no FK cycle)"
        assert table.columns["evidence_ref"].nullable

    def test_session_tenant_unique_target_preserved(self) -> None:
        """The session unique target from migration 008 must remain."""
        table = Base.metadata.tables["video_sessions"]
        uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["session_id", "tenant_id"] for uq in uqs)
