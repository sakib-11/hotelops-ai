"""Contract hydration for configuration versions (Task 10).

Rebuilds the frozen ConfigurationVersionModel snapshot from the ORM
version row and its version-owned entity rows. Used by the service and
API layer whenever the full physical model is needed.

The hydration is deterministic: entity ordering follows the canonical
category order (cameras, zones, tables, entrances, queue_areas,
service_areas, privacy_rois, exclusion_rois) and within a category the
database ordering (insertion order) is preserved.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.configuration import (
    CameraProfileEntityModel,
    ConfigurationVersionModel,
    EntranceEntityModel,
    ExclusionROIEntityModel,
    PrivacyROIEntityModel,
    QueueAreaEntityModel,
    ServiceAreaEntityModel,
    TableEntityModel,
    ZoneEntityModel,
)
from contracts.common import (
    CameraId,
    ConfigurationId,
    ConfigurationVersionId,
    TenantId,
    VenueId,
)
from contracts.configuration import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationStatus,
    EntranceDirection,
    EntranceModel,
    ExclusionROIModel,
    PrivacyROIModel,
    QueueAreaModel,
    ServiceAreaModel,
    TableModel,
    ValidationResultModel,
    ZoneModel,
    ZoneType,
)
from contracts.configuration import (
    ConfigurationVersionModel as ContractVersion,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryScope, GeometryType


async def load_version_entities(
    session: AsyncSession,
    version: ConfigurationVersionModel,
) -> ContractVersion:
    """Load all version-owned entities and hydrate the contract snapshot."""
    version_id = version.configuration_version_id
    tenant_id = version.tenant_id

    cameras = (
        (
            await session.execute(
                select(CameraProfileEntityModel).where(
                    CameraProfileEntityModel.configuration_version_id == version_id,
                    CameraProfileEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    zones = (
        (
            await session.execute(
                select(ZoneEntityModel).where(
                    ZoneEntityModel.configuration_version_id == version_id,
                    ZoneEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    tables = (
        (
            await session.execute(
                select(TableEntityModel).where(
                    TableEntityModel.configuration_version_id == version_id,
                    TableEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    entrances = (
        (
            await session.execute(
                select(EntranceEntityModel).where(
                    EntranceEntityModel.configuration_version_id == version_id,
                    EntranceEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    queue_areas = (
        (
            await session.execute(
                select(QueueAreaEntityModel).where(
                    QueueAreaEntityModel.configuration_version_id == version_id,
                    QueueAreaEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    service_areas = (
        (
            await session.execute(
                select(ServiceAreaEntityModel).where(
                    ServiceAreaEntityModel.configuration_version_id == version_id,
                    ServiceAreaEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    privacy_rois = (
        (
            await session.execute(
                select(PrivacyROIEntityModel).where(
                    PrivacyROIEntityModel.configuration_version_id == version_id,
                    PrivacyROIEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    exclusion_rois = (
        (
            await session.execute(
                select(ExclusionROIEntityModel).where(
                    ExclusionROIEntityModel.configuration_version_id == version_id,
                    ExclusionROIEntityModel.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )

    return hydrate_contract(
        version,
        cameras=[_camera(c) for c in cameras],
        zones=[_zone(z) for z in zones],
        tables=[_table(t) for t in tables],
        entrances=[_entrance(e) for e in entrances],
        queue_areas=[_queue(q) for q in queue_areas],
        service_areas=[_service(s) for s in service_areas],
        privacy_rois=[_privacy(p) for p in privacy_rois],
        exclusion_rois=[_exclusion(x) for x in exclusion_rois],
    )


def hydrate_contract(
    row: ConfigurationVersionModel,
    *,
    cameras: list[CameraProfileModel] | None = None,
    zones: list[ZoneModel] | None = None,
    tables: list[TableModel] | None = None,
    entrances: list[EntranceModel] | None = None,
    queue_areas: list[QueueAreaModel] | None = None,
    service_areas: list[ServiceAreaModel] | None = None,
    privacy_rois: list[PrivacyROIModel] | None = None,
    exclusion_rois: list[ExclusionROIModel] | None = None,
) -> ContractVersion:
    """Build the frozen contract from the ORM row + entity lists."""
    return ContractVersion(
        configuration_version_id=ConfigurationVersionId(row.configuration_version_id),
        configuration_id=ConfigurationId(row.configuration_id),
        venue_id=VenueId(row.venue_id),
        tenant_id=TenantId(row.tenant_id),
        version=row.version,
        status=ConfigurationStatus(row.status),
        cameras=cameras or [],
        zones=zones or [],
        tables=tables or [],
        entrances=entrances or [],
        queue_areas=queue_areas or [],
        service_areas=service_areas or [],
        privacy_rois=privacy_rois or [],
        exclusion_rois=exclusion_rois or [],
        validation_result=(
            ValidationResultModel.model_validate(row.validation_result)
            if row.validation_result
            else None
        ),
        validated_at=row.validated_at,
        validated_by=row.validated_by,
        published_at=row.published_at,
        published_by=row.published_by,
        replaced_version_id=(
            ConfigurationVersionId(row.replaced_version_id)
            if row.replaced_version_id is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# =============================================================================
# Row -> contract entity converters
# =============================================================================


def _geometry(value: dict[str, Any] | None) -> GeometryModel | None:
    if value is None:
        return None
    return GeometryModel(
        geometry_id=str(value.get("geometry_id", "g")),
        geometry_type=GeometryType(value["geometry_type"]),
        coordinate_space=CoordinateSpace(value["coordinate_space"]),
        geometry_scope=GeometryScope(value["geometry_scope"]),
        coordinates=value["coordinates"],
        reference_camera_profile_id=value.get("reference_camera_profile_id"),
        reference_width=value.get("reference_width"),
        reference_height=value.get("reference_height"),
        metadata=value.get("metadata"),
    )


def _camera(row: CameraProfileEntityModel) -> CameraProfileModel:
    geom = _geometry(row.geometry)
    return CameraProfileModel(
        profile_id=row.profile_id,
        camera_id=CameraId(row.camera_id),
        camera_reference=row.camera_reference,
        mount_type=CameraMountType(row.mount_type),
        mount_height_meters=row.mount_height_meters,
        tilt_degrees=row.tilt_degrees,
        pan_degrees=row.pan_degrees,
        roll_degrees=row.roll_degrees,
        resolution_width=row.resolution_width,
        resolution_height=row.resolution_height,
        fps=row.fps,
        codec=row.codec,
        image_orientation=row.image_orientation,
        analysis_enabled=row.analysis_enabled,
        detection_zones=list(row.detection_zones or []),
        privacy_rois=list(row.privacy_rois or []),
        exclusion_rois=list(row.exclusion_rois or []),
        physical_placement=geom,
        metadata=row.metadata_,
    )


def _zone(row: ZoneEntityModel) -> ZoneModel:
    return ZoneModel(
        profile_id=row.profile_id,
        name=row.name,
        zone_type=ZoneType(row.zone_type),
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        labels=list(row.labels or []),
        contained_tables=list(row.contained_tables or []),
        contained_entrances=list(row.contained_entrances or []),
        contained_queue_areas=list(row.contained_queue_areas or []),
        contained_service_areas=list(row.contained_service_areas or []),
        metadata=row.metadata_,
    )


def _table(row: TableEntityModel) -> TableModel:
    return TableModel(
        profile_id=row.profile_id,
        name=row.name,
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        seat_count=row.seat_count,
        table_shape=row.table_shape,
        metadata=row.metadata_,
    )


def _entrance(row: EntranceEntityModel) -> EntranceModel:
    return EntranceModel(
        profile_id=row.profile_id,
        name=row.name,
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        direction=EntranceDirection(row.direction),
        zone_profile_id=row.zone_profile_id,
        camera_profiles=list(row.camera_profiles or []),
        metadata=row.metadata_,
    )


def _queue(row: QueueAreaEntityModel) -> QueueAreaModel:
    return QueueAreaModel(
        profile_id=row.profile_id,
        name=row.name,
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        queue_direction=list(row.queue_direction) if row.queue_direction else None,
        max_queue_length=row.max_queue_length,
        zone_profile_id=row.zone_profile_id,
        camera_profiles=list(row.camera_profiles or []),
        metadata=row.metadata_,
    )


def _service(row: ServiceAreaEntityModel) -> ServiceAreaModel:
    return ServiceAreaModel(
        profile_id=row.profile_id,
        name=row.name,
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        service_type=row.service_type,
        zone_profile_id=row.zone_profile_id,
        camera_profiles=list(row.camera_profiles or []),
        metadata=row.metadata_,
    )


def _privacy(row: PrivacyROIEntityModel) -> PrivacyROIModel:
    return PrivacyROIModel(
        profile_id=row.profile_id,
        name=row.name,
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        privacy_action=row.privacy_action,
        policy_reference=row.policy_reference,
        camera_profiles=list(row.camera_profiles or []),
        metadata=row.metadata_,
    )


def _exclusion(row: ExclusionROIEntityModel) -> ExclusionROIModel:
    return ExclusionROIModel(
        profile_id=row.profile_id,
        name=row.name,
        geometry=_geometry(row.geometry) or _missing_geometry(row),
        excluded_tasks=list(row.excluded_tasks or []),
        exclusion_reason=row.exclusion_reason,
        camera_profiles=list(row.camera_profiles or []),
        metadata=row.metadata_,
    )


def _missing_geometry(row: Any) -> GeometryModel:
    """Raise on corrupted data — the DB enforces geometry NOT NULL.

    A missing geometry signals data corruption, not an edge case:
    silently fabricating a degenerate polygon would mask it and produce
    a misleading contract snapshot.
    """
    msg = (
        f"Configuration entity '{getattr(row, 'profile_id', '?')}' "
        "has no stored geometry — database integrity violation"
    )
    raise ValueError(msg)


__all__ = [
    "hydrate_contract",
    "load_version_entities",
]
