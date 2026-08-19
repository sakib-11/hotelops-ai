"""Typed request and response models for the storage abstraction.

Provider-independent data structures representing storage operations,
object metadata, presigned URLs, and multipart upload state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ObjectCategory(StrEnum):
    """Controlled storage object categories."""

    RECORDINGS = "recordings"
    EVIDENCE = "evidence"
    REPORTS = "reports"
    ANALYTICS = "analytics"
    TEMPORARY = "temporary"


@dataclass(frozen=True)
class StorageKeyComponents:
    """Deconstructed components of a standard scoped storage object key."""

    tenant_id: UUID
    venue_id: UUID
    category: ObjectCategory
    year: int
    month: int
    day: int
    artifact_id: UUID
    extension: str


@dataclass(frozen=True)
class ObjectMetadata:
    """Provider-independent metadata for an object in storage."""

    object_key: str
    size_bytes: int
    content_type: str
    etag: str
    last_modified: datetime
    checksum_sha256: str | None = None
    custom_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObjectReference:
    """A logical pointer to a stored media object."""

    object_key: str
    uri: str
    content_type: str
    size_bytes: int | None = None
    checksum_sha256: str | None = None


@dataclass(frozen=True)
class UploadInitiationRequest:
    """Request parameters for initiating an upload."""

    object_key: str
    content_type: str
    expected_size_bytes: int | None = None
    checksum_sha256: str | None = None
    expires_in_seconds: int = 900
    custom_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PresignedUploadResult:
    """Result of generating a controlled presigned upload URL."""

    upload_url: str
    object_key: str
    http_method: str
    required_headers: dict[str, str]
    expires_in_seconds: int
    expires_at: datetime


@dataclass(frozen=True)
class PresignedDownloadResult:
    """Result of generating a controlled presigned download URL."""

    download_url: str
    object_key: str
    expires_in_seconds: int
    expires_at: datetime


@dataclass(frozen=True)
class MultipartInitiationResult:
    """Result of initiating a multipart upload session."""

    upload_id: str
    object_key: str


@dataclass(frozen=True)
class MultipartPartInfo:
    """Identification and checksum info for a completed multipart part."""

    part_number: int
    etag: str


@dataclass(frozen=True)
class MultipartCompleteRequest:
    """Parameters required to finalize a multipart upload."""

    upload_id: str
    object_key: str
    parts: list[MultipartPartInfo]


@dataclass(frozen=True)
class MultipartPartUploadRequest:
    """Request to presign one part of an active multipart upload."""

    upload_id: str
    object_key: str
    part_number: int
    expires_in_seconds: int = 900


@dataclass(frozen=True)
class MultipartPartUploadResult:
    """A presigned PUT URL for one multipart part."""

    part_number: int
    upload_url: str
    expires_in_seconds: int
    expires_at: datetime
