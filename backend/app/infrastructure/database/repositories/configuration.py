"""Configuration domain repositories (Task 10).

Tenant/venue-scoped persistence for the versioned Camera/Venue
configuration. All access is constrained by the trusted server-side
ActorContext; every query filters by tenant and venue scope.

The version repository owns the state machine transitions: status
changes are guarded (from-state matched atomically in SQL), and
publication is a single transactional unit (row lock on the venue
configuration + version transition + current-version pointer update)
so concurrent publish/validation requests cannot race.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.auth.scope import require_tenant_venue_access
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
from backend.app.infrastructure.database.models.video import VideoSessionModel
from contracts.common import ConfigurationId, ConfigurationVersionId, TenantId, VenueId
from contracts.configuration import ConfigurationStatus
from contracts.configuration import ConfigurationVersionModel as ContractVersion
from contracts.identity import ActorContext


def _uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _has_venue_access(actor: ActorContext, venue_id: uuid.UUID) -> bool:
    """Empty venue_scope means ALL_VENUES (tenant-wide access)."""
    if not actor.venue_scope:
        return True
    return _uuid(venue_id) in {_uuid(v) for v in actor.venue_scope}


class ConfigurationRepository:
    """Tenant-scoped persistence for the logical configuration aggregate."""

    async def get_or_create(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        venue_id: VenueId,
        name: str,
    ) -> ConfigurationModel:
        """Return the venue's configuration or create it (idempotent)."""
        require_tenant_venue_access(
            actor=actor,
            resource_tenant_id=TenantId(actor.tenant_id),
            venue_id=venue_id,
        )
        stmt = select(ConfigurationModel).where(
            ConfigurationModel.venue_id == _uuid(venue_id),
            ConfigurationModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is not None:
            return record
        record = ConfigurationModel(
            venue_id=_uuid(venue_id),
            tenant_id=_uuid(actor.tenant_id),
            name=name,
        )
        session.add(record)
        await session.flush()
        return record

    async def get_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        configuration_id: ConfigurationId | uuid.UUID | str,
    ) -> ConfigurationModel | None:
        """Fetch the aggregate within the actor's tenant + venue scope."""
        stmt = select(ConfigurationModel).where(
            ConfigurationModel.configuration_id == _uuid(configuration_id),
            ConfigurationModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_by_venue(
        self,
        session: AsyncSession,
        actor: ActorContext,
        venue_id: VenueId | uuid.UUID,
    ) -> ConfigurationModel | None:
        stmt = select(ConfigurationModel).where(
            ConfigurationModel.venue_id == _uuid(venue_id),
            ConfigurationModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def set_current_published_version(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        configuration_id: ConfigurationId | uuid.UUID,
        configuration_version_id: ConfigurationVersionId | uuid.UUID,
    ) -> bool:
        """Atomically update the current-published pointer (publish commit)."""
        now = datetime.now(UTC)
        stmt = (
            update(ConfigurationModel)
            .where(
                ConfigurationModel.configuration_id == _uuid(configuration_id),
                ConfigurationModel.tenant_id == _uuid(actor.tenant_id),
            )
            .values(current_published_version_id=_uuid(configuration_version_id), updated_at=now)
            .returning(ConfigurationModel.configuration_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None


class ConfigurationVersionRepository:
    """Tenant-scoped, state-guarded version lifecycle persistence."""

    # =========================================================================
    # Reads
    # =========================================================================

    async def get_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        version_id: ConfigurationVersionId | uuid.UUID | str,
    ) -> ConfigurationVersionModel | None:
        stmt = select(ConfigurationVersionModel).where(
            ConfigurationVersionModel.configuration_version_id == _uuid(version_id),
            ConfigurationVersionModel.tenant_id == _uuid(actor.tenant_id),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_by_configuration(
        self,
        session: AsyncSession,
        actor: ActorContext,
        configuration_id: ConfigurationId | uuid.UUID,
    ) -> list[ConfigurationVersionModel]:
        stmt = (
            select(ConfigurationVersionModel)
            .where(
                ConfigurationVersionModel.configuration_id == _uuid(configuration_id),
                ConfigurationVersionModel.tenant_id == _uuid(actor.tenant_id),
            )
            .order_by(ConfigurationVersionModel.version.asc())
        )
        result = await session.execute(stmt)
        return [r for r in result.scalars().all() if _has_venue_access(actor, r.venue_id)]

    async def get_latest_version(
        self,
        session: AsyncSession,
        actor: ActorContext,
        configuration_id: ConfigurationId | uuid.UUID,
    ) -> ConfigurationVersionModel | None:
        stmt = (
            select(ConfigurationVersionModel)
            .where(
                ConfigurationVersionModel.configuration_id == _uuid(configuration_id),
                ConfigurationVersionModel.tenant_id == _uuid(actor.tenant_id),
            )
            .order_by(ConfigurationVersionModel.version.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_latest_published_version(
        self,
        session: AsyncSession,
        actor: ActorContext,
        configuration_id: ConfigurationId | uuid.UUID,
    ) -> ConfigurationVersionModel | None:
        """Highest-numbered PUBLISHED version of a configuration.

        Used by the publish monotonicity guard: publishing an older
        version after a newer one is already published would regress the
        venue's current-version pointer, so it must be rejected.
        """
        stmt = (
            select(ConfigurationVersionModel)
            .where(
                ConfigurationVersionModel.configuration_id == _uuid(configuration_id),
                ConfigurationVersionModel.tenant_id == _uuid(actor.tenant_id),
                ConfigurationVersionModel.status == "published",
            )
            .order_by(ConfigurationVersionModel.version.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_current_published_for_venue(
        self,
        session: AsyncSession,
        actor: ActorContext,
        venue_id: VenueId | uuid.UUID,
    ) -> ConfigurationVersionModel | None:
        """Resolve the current published version for a venue (session pinning)."""
        stmt = (
            select(ConfigurationVersionModel)
            .join(
                ConfigurationModel,
                (ConfigurationModel.configuration_id == ConfigurationVersionModel.configuration_id)
                & (ConfigurationModel.tenant_id == ConfigurationVersionModel.tenant_id),
            )
            .where(
                ConfigurationModel.venue_id == _uuid(venue_id),
                ConfigurationModel.tenant_id == _uuid(actor.tenant_id),
                ConfigurationModel.current_published_version_id
                == ConfigurationVersionModel.configuration_version_id,
                ConfigurationVersionModel.status == "published",
            )
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def get_published_for_session(
        self,
        session: AsyncSession,
        actor: ActorContext,
        session_id: uuid.UUID | str,
    ) -> ConfigurationVersionModel | None:
        """Resolve the EXACT pinned version for a video session.

        Never substitutes the latest published version — historical
        replay must resolve the pinned snapshot. Returns None when the
        session is unpinned or the pinned version is gone.
        """
        session_stmt = select(VideoSessionModel).where(
            VideoSessionModel.session_id == _uuid(session_id),
            VideoSessionModel.tenant_id == _uuid(actor.tenant_id),
        )
        session_result = await session.execute(session_stmt)
        session_row = session_result.scalar_one_or_none()
        if session_row is None or session_row.configuration_version_id is None:
            return None
        if not _has_venue_access(actor, session_row.venue_id):
            return None
        version_stmt = select(ConfigurationVersionModel).where(
            ConfigurationVersionModel.configuration_version_id
            == session_row.configuration_version_id,
            ConfigurationVersionModel.tenant_id == _uuid(actor.tenant_id),
            ConfigurationVersionModel.status == "published",
        )
        version_result = await session.execute(version_stmt)
        record = version_result.scalar_one_or_none()
        if record is None or not _has_venue_access(actor, record.venue_id):
            return None
        return record

    async def load_contract(
        self,
        session: AsyncSession,
        row: ConfigurationVersionModel,
    ) -> ContractVersion:
        """Hydrate the full contract snapshot (entities included).

        Production implementation loads the version-owned entity rows
        from the database; tests inject a fake.
        """
        from backend.app.domain.configuration.hydration import load_version_entities

        return await load_version_entities(session, row)

    # =========================================================================
    # Writes
    # =========================================================================

    async def create(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        configuration_id: ConfigurationId | uuid.UUID,
        venue_id: VenueId | uuid.UUID,
        version_number: int,
    ) -> ConfigurationVersionModel:
        require_tenant_venue_access(
            actor=actor,
            resource_tenant_id=TenantId(actor.tenant_id),
            venue_id=VenueId(venue_id),
        )
        record = ConfigurationVersionModel(
            configuration_id=_uuid(configuration_id),
            venue_id=_uuid(venue_id),
            tenant_id=_uuid(actor.tenant_id),
            version=version_number,
            status=ConfigurationStatus.DRAFT.value,
        )
        session.add(record)
        await session.flush()
        return record

    async def replace_entities(
        self,
        session: AsyncSession,
        version: ConfigurationVersionModel,
        contract: ContractVersion,
    ) -> None:
        """Replace the version-owned entities of a DRAFT version.

        All entity rows are removed and re-inserted from the contract
        snapshot. Only called for DRAFT versions (state guarded by the
        caller). Entity rows are version-owned and tenant-scoped.
        """
        version_id = version.configuration_version_id
        tenant_id = version.tenant_id
        venue_id = version.venue_id
        rows: dict[Any, list[dict[str, Any]]] = {}

        rows[CameraProfileEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": c.profile_id,
                "camera_id": _uuid(c.camera_id),
                "camera_reference": c.camera_reference,
                "mount_type": c.mount_type.value
                if hasattr(c.mount_type, "value")
                else str(c.mount_type),
                "mount_height_meters": c.mount_height_meters,
                "tilt_degrees": c.tilt_degrees,
                "pan_degrees": c.pan_degrees,
                "roll_degrees": c.roll_degrees,
                "resolution_width": c.resolution_width,
                "resolution_height": c.resolution_height,
                "fps": c.fps,
                "codec": c.codec,
                "image_orientation": c.image_orientation,
                "analysis_enabled": c.analysis_enabled,
                "detection_zones": list(c.detection_zones),
                "privacy_rois": list(c.privacy_rois),
                "exclusion_rois": list(c.exclusion_rois),
                "geometry": c.physical_placement.model_dump(mode="json")
                if c.physical_placement
                else None,
                "coordinate_space": (
                    c.physical_placement.coordinate_space.value
                    if c.physical_placement
                    else "venue_local"
                ),
                "geometry_type": (
                    c.physical_placement.geometry_type.value if c.physical_placement else "point"
                ),
                "metadata": dict(c.metadata) if c.metadata else None,
            }
            for c in contract.cameras
        ]
        rows[ZoneEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": z.profile_id,
                "name": z.name,
                "zone_type": z.zone_type.value
                if hasattr(z.zone_type, "value")
                else str(z.zone_type),
                "geometry": z.geometry.model_dump(mode="json"),
                "coordinate_space": z.geometry.coordinate_space.value,
                "geometry_type": z.geometry.geometry_type.value,
                "labels": list(z.labels),
                "contained_tables": list(z.contained_tables),
                "contained_entrances": list(z.contained_entrances),
                "contained_queue_areas": list(z.contained_queue_areas),
                "contained_service_areas": list(z.contained_service_areas),
                "metadata": dict(z.metadata) if z.metadata else None,
            }
            for z in contract.zones
        ]
        rows[TableEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": t.profile_id,
                "name": t.name,
                "geometry": t.geometry.model_dump(mode="json"),
                "coordinate_space": t.geometry.coordinate_space.value,
                "geometry_type": t.geometry.geometry_type.value,
                "seat_count": t.seat_count,
                "table_shape": t.table_shape,
                "metadata": dict(t.metadata) if t.metadata else None,
            }
            for t in contract.tables
        ]
        rows[EntranceEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": e.profile_id,
                "name": e.name,
                "geometry": e.geometry.model_dump(mode="json"),
                "coordinate_space": e.geometry.coordinate_space.value,
                "geometry_type": e.geometry.geometry_type.value,
                "direction": e.direction.value
                if hasattr(e.direction, "value")
                else str(e.direction),
                "zone_profile_id": e.zone_profile_id,
                "camera_profiles": list(e.camera_profiles),
                "metadata": dict(e.metadata) if e.metadata else None,
            }
            for e in contract.entrances
        ]
        rows[QueueAreaEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": q.profile_id,
                "name": q.name,
                "geometry": q.geometry.model_dump(mode="json"),
                "coordinate_space": q.geometry.coordinate_space.value,
                "geometry_type": q.geometry.geometry_type.value,
                "queue_direction": list(q.queue_direction) if q.queue_direction else None,
                "max_queue_length": q.max_queue_length,
                "zone_profile_id": q.zone_profile_id,
                "camera_profiles": list(q.camera_profiles),
                "metadata": dict(q.metadata) if q.metadata else None,
            }
            for q in contract.queue_areas
        ]
        rows[ServiceAreaEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": s.profile_id,
                "name": s.name,
                "geometry": s.geometry.model_dump(mode="json"),
                "coordinate_space": s.geometry.coordinate_space.value,
                "geometry_type": s.geometry.geometry_type.value,
                "service_type": s.service_type,
                "zone_profile_id": s.zone_profile_id,
                "camera_profiles": list(s.camera_profiles),
                "metadata": dict(s.metadata) if s.metadata else None,
            }
            for s in contract.service_areas
        ]
        rows[PrivacyROIEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": p.profile_id,
                "name": p.name,
                "geometry": p.geometry.model_dump(mode="json"),
                "coordinate_space": p.geometry.coordinate_space.value,
                "geometry_type": p.geometry.geometry_type.value,
                "privacy_action": p.privacy_action,
                "policy_reference": p.policy_reference,
                "camera_profiles": list(p.camera_profiles),
                "metadata": dict(p.metadata) if p.metadata else None,
            }
            for p in contract.privacy_rois
        ]
        rows[ExclusionROIEntityModel] = [
            {
                "configuration_version_id": version_id,
                "venue_id": venue_id,
                "tenant_id": tenant_id,
                "profile_id": x.profile_id,
                "name": x.name,
                "geometry": x.geometry.model_dump(mode="json"),
                "coordinate_space": x.geometry.coordinate_space.value,
                "geometry_type": x.geometry.geometry_type.value,
                "excluded_tasks": list(x.excluded_tasks),
                "exclusion_reason": x.exclusion_reason,
                "camera_profiles": list(x.camera_profiles),
                "metadata": dict(x.metadata) if x.metadata else None,
            }
            for x in contract.exclusion_rois
        ]

        # Delete existing entity rows for this version (DRAFT only).
        for table_model in rows:
            await session.execute(
                table_model.__table__.delete().where(
                    table_model.configuration_version_id == version_id,
                    table_model.tenant_id == tenant_id,
                )
            )
        # Insert new rows (bulk insert via table metadata).
        for table_model, table_rows in rows.items():
            if not table_rows:
                continue
            await session.execute(table_model.__table__.insert(), table_rows)

    async def update_status(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        version_id: ConfigurationVersionId | uuid.UUID,
        from_status: str,
        to_status: str,
        extra_updates: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically transition status when the current status matches."""
        values: dict[str, Any] = {
            "status": to_status,
            "updated_at": datetime.now(UTC),
        }
        if extra_updates:
            values.update(extra_updates)
        stmt = (
            update(ConfigurationVersionModel)
            .where(
                ConfigurationVersionModel.configuration_version_id == _uuid(version_id),
                ConfigurationVersionModel.tenant_id == _uuid(actor.tenant_id),
                ConfigurationVersionModel.status == from_status,
            )
            .values(**values)
            .returning(ConfigurationVersionModel.configuration_version_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def lock_venue_configuration(
        self,
        session: AsyncSession,
        actor: ActorContext,
        configuration_id: ConfigurationId | uuid.UUID,
    ) -> ConfigurationModel | None:
        """SELECT ... FOR UPDATE on the venue configuration row.

        Serializes concurrent publish/validation for the same venue —
        the project concurrency strategy for the current-version pointer.
        """
        stmt = (
            select(ConfigurationModel)
            .where(
                ConfigurationModel.configuration_id == _uuid(configuration_id),
                ConfigurationModel.tenant_id == _uuid(actor.tenant_id),
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["ConfigurationRepository", "ConfigurationVersionRepository"]
