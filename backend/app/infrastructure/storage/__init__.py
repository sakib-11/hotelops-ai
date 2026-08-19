"""Object storage infrastructure and abstraction layer."""

from backend.app.infrastructure.storage.client import StorageClient
from backend.app.infrastructure.storage.exceptions import (
    InvalidObjectKeyError,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    StorageConfigError,
    StorageError,
    StorageIntegrityError,
    StorageOperationError,
    StorageUnavailableError,
)
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.infrastructure.storage.key_builder import (
    build_analytics_key,
    build_evidence_key,
    build_object_key,
    build_recording_key,
    build_report_key,
    build_temporary_key,
    normalize_extension,
    parse_object_key,
)
from backend.app.infrastructure.storage.protocol import StoragePort
from backend.app.infrastructure.storage.s3_adapter import S3StorageAdapter
from backend.app.infrastructure.storage.types import (
    MultipartCompleteRequest,
    MultipartInitiationResult,
    MultipartPartInfo,
    MultipartPartUploadRequest,
    MultipartPartUploadResult,
    ObjectCategory,
    ObjectMetadata,
    ObjectReference,
    PresignedDownloadResult,
    PresignedUploadResult,
    StorageKeyComponents,
    UploadInitiationRequest,
)

__all__ = [
    "FakeStorageAdapter",
    "InvalidObjectKeyError",
    "MultipartCompleteRequest",
    "MultipartInitiationResult",
    "MultipartPartInfo",
    "MultipartPartUploadRequest",
    "MultipartPartUploadResult",
    "ObjectAlreadyExistsError",
    "ObjectCategory",
    "ObjectMetadata",
    "ObjectNotFoundError",
    "ObjectReference",
    "PresignedDownloadResult",
    "PresignedUploadResult",
    "S3StorageAdapter",
    "StorageClient",
    "StorageConfigError",
    "StorageError",
    "StorageIntegrityError",
    "StorageKeyComponents",
    "StorageOperationError",
    "StoragePort",
    "StorageUnavailableError",
    "UploadInitiationRequest",
    "build_analytics_key",
    "build_evidence_key",
    "build_object_key",
    "build_recording_key",
    "build_report_key",
    "build_temporary_key",
    "normalize_extension",
    "parse_object_key",
]
