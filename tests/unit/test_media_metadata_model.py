"""Unit tests for the Media Metadata database model and constraints (Task 9.6).

Validates:
- MediaAssetModel ORM mapping and schema attributes
- Composite tenant/venue foreign keys and uniqueness constraints
- Checksum SHA-256 regex constraint
- Non-negative file size constraint
- Atomic event link pair constraint
- Lifecycle states and category enum completeness
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.app.infrastructure.database.models.media import (
    _MEDIA_CATEGORIES,
    _MEDIA_LIFECYCLE_STATES,
    MediaAssetModel,
)


class TestMediaAssetModelMapping:
    """Tests verifying SQLAlchemy ORM mapping for MediaAssetModel."""

    def test_table_name_and_primary_key(self) -> None:
        assert MediaAssetModel.__tablename__ == "media_assets"
        pk_cols = [c.name for c in MediaAssetModel.__table__.primary_key]
        assert pk_cols == ["media_id"]

    def test_media_asset_model_instantiation(self) -> None:
        media_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        venue_id = uuid.uuid4()
        now = datetime.now(UTC)

        model = MediaAssetModel(
            media_id=media_id,
            tenant_id=tenant_id,
            venue_id=venue_id,
            category="recordings",
            object_key=f"tenants/{tenant_id}/venues/{venue_id}/recordings/2026/08/10/{media_id}.mp4",
            storage_uri=f"s3://hotelops-development/tenants/{tenant_id}/venues/{venue_id}/recordings/2026/08/10/{media_id}.mp4",
            storage_bucket="hotelops-development",
            content_type="video/mp4",
            size_bytes=10485760,
            checksum_sha256="69808d9ea5dc4fdb2d7d59e7cd5601073bf9355c597d8b94257e32cd3c8dce7f",
            original_filename="front_entrance_clip.mp4",
            lifecycle_state="initiated",
            retention_class="cctv_30_days",
            created_at=now,
            updated_at=now,
        )

        assert model.media_id == media_id
        assert model.tenant_id == tenant_id
        assert model.venue_id == venue_id
        assert model.category == "recordings"
        assert model.size_bytes == 10485760
        assert model.lifecycle_state == "initiated"
        assert "MediaAssetModel" in repr(model)

    def test_categories_match_specification(self) -> None:
        expected = {"recordings", "evidence", "reports", "analytics", "temporary"}
        assert set(_MEDIA_CATEGORIES) == expected

    def test_lifecycle_states_match_specification(self) -> None:
        expected = {
            "initiated",
            "uploading",
            "uploaded",
            "validating",
            "available",
            "failed",
            "expired",
            "deletion_pending",
            "deleted",
        }
        assert set(_MEDIA_LIFECYCLE_STATES) == expected


class TestMediaAssetTableConstraints:
    """Tests verifying database table constraints, indexes, and FK definitions."""

    def test_unique_constraints(self) -> None:
        constraints = {
            c.name: [col.name for col in c.columns]
            for c in MediaAssetModel.__table__.constraints
            if hasattr(c, "columns") and c.name and c.name.startswith("uq_")
        }
        assert "uq_media_assets_media_tenant" in constraints
        assert constraints["uq_media_assets_media_tenant"] == ["media_id", "tenant_id"]
        assert "uq_media_assets_object_key" in constraints
        assert constraints["uq_media_assets_object_key"] == ["object_key"]

    def test_check_constraints(self) -> None:
        check_names = {
            c.name for c in MediaAssetModel.__table__.constraints if hasattr(c, "sqltext")
        }
        assert "ck_media_assets_size_non_negative" in check_names
        assert "ck_media_assets_checksum_sha256" in check_names
        assert "ck_media_assets_event_pair" in check_names
        assert "ck_media_assets_key_not_empty" in check_names
        assert "ck_media_assets_uri_not_empty" in check_names

    def test_foreign_key_constraints(self) -> None:
        fk_targets = {
            fk.name: [elem.target_fullname for elem in fk.elements]
            for fk in MediaAssetModel.__table__.foreign_key_constraints
        }
        assert "fk_media_assets_venue_tenant" in fk_targets
        assert fk_targets["fk_media_assets_venue_tenant"] == [
            "venues.venue_id",
            "venues.tenant_id",
        ]
        assert "fk_media_assets_camera_tenant" in fk_targets
        assert fk_targets["fk_media_assets_camera_tenant"] == [
            "cameras.camera_id",
            "cameras.tenant_id",
        ]
        assert "fk_media_assets_session_tenant" in fk_targets
        assert fk_targets["fk_media_assets_session_tenant"] == [
            "video_sessions.session_id",
            "video_sessions.tenant_id",
        ]
        assert "fk_media_assets_event" in fk_targets
        assert fk_targets["fk_media_assets_event"] == [
            "operational_events.event_time",
            "operational_events.event_id",
        ]

    def test_indexes_defined(self) -> None:
        index_names = {idx.name for idx in MediaAssetModel.__table__.indexes}
        assert "ix_media_assets_tenant_id" in index_names
        assert "ix_media_assets_venue_id" in index_names
        assert "ix_media_assets_tenant_category_state" in index_names
        assert "ix_media_assets_created_at" in index_names
