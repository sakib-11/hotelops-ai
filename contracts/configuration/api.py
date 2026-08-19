"""API contracts for the configuration domain (Task 10.15).

Request/response models for the configuration HTTP surface. These are
transport contracts — the authoritative domain models live in
contracts.configuration.models. Never expose endpoints that mutate
published versions or bypass the state machine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from contracts.common import ConfigurationId, ConfigurationVersionId, VenueId
from contracts.configuration import (
    CameraProfileModel,
    ConfigurationStatus,
    EntranceModel,
    ExclusionROIModel,
    PrivacyROIModel,
    QueueAreaModel,
    ServiceAreaModel,
    TableModel,
    ValidationResultModel,
    ZoneModel,
)


class DraftCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    venue_id: VenueId
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class DraftCloneRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_version_id: ConfigurationVersionId


class DraftUpdateRequest(BaseModel):
    """Full-snapshot replacement of a DRAFT version's entities.

    All lists are optional; omitted lists are left unchanged. Provided
    lists replace the previous content entirely.
    """

    model_config = {"extra": "forbid"}

    cameras: list[CameraProfileModel] | None = None
    zones: list[ZoneModel] | None = None
    tables: list[TableModel] | None = None
    entrances: list[EntranceModel] | None = None
    queue_areas: list[QueueAreaModel] | None = None
    service_areas: list[ServiceAreaModel] | None = None
    privacy_rois: list[PrivacyROIModel] | None = None
    exclusion_rois: list[ExclusionROIModel] | None = None


class ConfigurationVersionResponse(BaseModel):
    """A configuration version snapshot (domain contract passthrough)."""

    model_config = {"extra": "forbid"}

    configuration_version_id: ConfigurationVersionId
    configuration_id: ConfigurationId
    venue_id: VenueId
    version: int
    status: ConfigurationStatus
    cameras: list[CameraProfileModel]
    zones: list[ZoneModel]
    tables: list[TableModel]
    entrances: list[EntranceModel]
    queue_areas: list[QueueAreaModel]
    service_areas: list[ServiceAreaModel]
    privacy_rois: list[PrivacyROIModel]
    exclusion_rois: list[ExclusionROIModel]
    validation_result: ValidationResultModel | None = None
    validated_at: datetime | None = None
    validated_by: str | None = None
    published_at: datetime | None = None
    published_by: str | None = None
    replaced_version_id: ConfigurationVersionId | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_version(cls, version: Any) -> ConfigurationVersionResponse:
        data = version.model_dump(mode="json")
        return cls.model_validate(data)


class ValidationRunResponse(BaseModel):
    model_config = {"extra": "forbid"}

    configuration_version_id: ConfigurationVersionId
    status: ConfigurationStatus
    valid: bool
    result: ValidationResultModel


class PublishResponse(BaseModel):
    model_config = {"extra": "forbid"}

    configuration_version_id: ConfigurationVersionId
    previous_published_version_id: ConfigurationVersionId | None
    published_at: datetime


class SessionConfigurationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    session_id: Any
    configuration_version_id: ConfigurationVersionId
    version: int
    pinned_at_creation: bool = True

    @classmethod
    def from_version(cls, session_id: Any, version: Any) -> SessionConfigurationResponse:
        return cls(
            session_id=session_id,
            configuration_version_id=version.configuration_version_id,
            version=version.version,
        )


__all__ = [
    "ConfigurationVersionResponse",
    "DraftCloneRequest",
    "DraftCreateRequest",
    "DraftUpdateRequest",
    "PublishResponse",
    "SessionConfigurationResponse",
    "ValidationRunResponse",
]
