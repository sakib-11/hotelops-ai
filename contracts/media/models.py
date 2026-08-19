"""Canonical media contract models for HotelOps AI (Task 9.7)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    EventId,
    MediaId,
    VenueId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)


class MediaCategory(StrEnum):
    """Controlled storage object categories."""

    RECORDINGS = "recordings"
    EVIDENCE = "evidence"
    REPORTS = "reports"
    ANALYTICS = "analytics"
    TEMPORARY = "temporary"


class MediaLifecycleState(StrEnum):
    """Lifecycle states of media stored in object storage."""

    INITIATED = "initiated"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    AVAILABLE = "available"
    FAILED = "failed"
    EXPIRED = "expired"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class MediaProvenance(BaseModel, frozen=True):
    """Provenance references linking media to its domain origin."""

    model_config = {"extra": "forbid"}

    camera_id: UUID | None = None
    session_id: VideoSessionId | None = None
    event_id: EventId | None = None
    event_time: datetime | None = None

    _validate_event_time = field_validator("event_time")(validate_utc)


def _validate_sha256(v: str | None) -> str | None:
    """Validate a SHA-256 checksum string (64 lowercase hex chars)."""
    if v is None:
        return v
    normalized = v.lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise ValueError("checksum_sha256 must be a 64-character lowercase hex SHA-256 digest")
    return normalized


class MediaUploadInitiateRequest(BaseModel, frozen=True):
    """Request payload to initiate a new media upload."""

    model_config = {"extra": "forbid"}

    venue_id: VenueId
    category: MediaCategory
    content_type: str = Field(min_length=3, max_length=128)
    expected_size_bytes: int = Field(default=0, ge=0)
    original_filename: str | None = Field(default=None, max_length=255)
    retention_class: str | None = Field(default=None, max_length=64)
    provenance: MediaProvenance | None = None
    custom_metadata: dict[str, Any] | None = None
    checksum_sha256: str | None = None

    _validate_checksum = field_validator("checksum_sha256")(_validate_sha256)


class MediaUploadInitiateResponse(BaseModel, frozen=True):
    """Authoritative response containing upload coordinates and capabilities."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    object_key: str
    storage_uri: str
    upload_url: str
    required_headers: dict[str, str] = Field(default_factory=dict)
    expires_in_seconds: int
    lifecycle_state: MediaLifecycleState
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


# =============================================================================
# Multipart / completion
# =============================================================================


class MediaPartInfo(BaseModel, frozen=True):
    """One completed multipart part (part number + provider ETag)."""

    model_config = {"extra": "forbid"}

    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)


class MediaCompleteRequest(BaseModel, frozen=True):
    """Request to finalize a media upload after bytes reached storage."""

    model_config = {"extra": "forbid"}

    upload_id: str | None = Field(default=None, max_length=255)
    parts: list[MediaPartInfo] | None = None
    checksum_sha256: str | None = None
    expected_size_bytes: int | None = Field(default=None, ge=0)

    _validate_checksum = field_validator("checksum_sha256")(_validate_sha256)

    @field_validator("parts")
    @classmethod
    def _validate_parts(cls, v: list[MediaPartInfo] | None) -> list[MediaPartInfo] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("parts must be non-empty when provided")
        numbers = [p.part_number for p in v]
        if len(numbers) != len(set(numbers)):
            raise ValueError("part numbers must be unique")
        return v


class MediaCompleteResponse(BaseModel, frozen=True):
    """Authoritative completion result."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    object_key: str
    lifecycle_state: MediaLifecycleState
    size_bytes: int
    checksum_sha256: str | None = None
    expires_at: datetime | None = None
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


class MediaMultipartInitiateResponse(BaseModel, frozen=True):
    """Result of registering a multipart upload session (large files)."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    object_key: str
    upload_id: str
    part_size_bytes: int
    max_parts: int
    expires_in_seconds: int
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


class MediaPartPresignRequest(BaseModel, frozen=True):
    """Parts to presign for an active multipart upload."""

    model_config = {"extra": "forbid"}

    part_numbers: list[int] = Field(min_length=1, max_length=10000)

    @field_validator("part_numbers")
    @classmethod
    def _validate_numbers(cls, v: list[int]) -> list[int]:
        if any(n < 1 or n > 10000 for n in v):
            raise ValueError("part numbers must be in the range 1..10000")
        if len(v) != len(set(v)):
            raise ValueError("part numbers must be unique")
        return v


class MediaPartPresignedUrl(BaseModel, frozen=True):
    """A presigned PUT URL for one multipart part."""

    model_config = {"extra": "forbid"}

    part_number: int
    upload_url: str
    expires_in_seconds: int
    expires_at: datetime


class MediaPartPresignResponse(BaseModel, frozen=True):
    """Presigned part upload coordinates."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    upload_id: str
    parts: list[MediaPartPresignedUrl]
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


# =============================================================================
# Verification / access / deletion / metadata
# =============================================================================


class MediaVerifyRequest(BaseModel, frozen=True):
    """Request to verify a completed upload before promotion."""

    model_config = {"extra": "forbid"}

    checksum_sha256: str | None = None

    _validate_checksum = field_validator("checksum_sha256")(_validate_sha256)


class MediaVerifyResponse(BaseModel, frozen=True):
    """Outcome of content + integrity verification."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    lifecycle_state: MediaLifecycleState
    checksum_sha256: str | None = None
    size_bytes: int
    validated_at: datetime | None = None
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


class MediaDownloadResponse(BaseModel, frozen=True):
    """A short-lived, server-authorized signed download URL."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    object_key: str
    download_url: str
    content_type: str
    original_filename: str | None = None
    expires_at: datetime
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


class MediaDeleteResponse(BaseModel, frozen=True):
    """Result of an idempotent deletion/abort request."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    lifecycle_state: MediaLifecycleState
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)


class MediaMetadataResponse(BaseModel, frozen=True):
    """Authoritative metadata record for a media asset."""

    model_config = {"extra": "forbid"}

    media_id: MediaId
    tenant_id: UUID
    venue_id: UUID
    category: MediaCategory
    object_key: str
    storage_uri: str
    storage_bucket: str
    content_type: str
    size_bytes: int
    checksum_sha256: str | None = None
    original_filename: str | None = None
    lifecycle_state: MediaLifecycleState
    retention_class: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    uploaded_at: datetime | None = None
    validated_at: datetime | None = None
    deleted_at: datetime | None = None
    schema_version: str = Field(default=SCHEMA_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
