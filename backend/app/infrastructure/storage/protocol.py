"""Storage Port Protocol interface.

Defines the provider-independent storage interface that application
and domain services depend upon. No vendor SDK types may appear here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from backend.app.infrastructure.storage.types import (
    MultipartCompleteRequest,
    MultipartInitiationResult,
    MultipartPartUploadRequest,
    MultipartPartUploadResult,
    ObjectMetadata,
    PresignedDownloadResult,
    PresignedUploadResult,
    UploadInitiationRequest,
)


@runtime_checkable
class StoragePort(Protocol):
    """Abstract storage port for binary media operations."""

    async def initialize(self) -> None:
        """Initialize underlying connection pools or clients."""
        ...

    async def check_connectivity(self) -> bool:
        """Check if the storage backend is reachable and healthy."""
        ...

    async def close(self) -> None:
        """Close active connections and cleanup resources."""
        ...

    async def get_object_metadata(self, object_key: str) -> ObjectMetadata | None:
        """Retrieve metadata for an object. Returns None if object does not exist."""
        ...

    async def object_exists(self, object_key: str) -> bool:
        """Check if an object exists in storage."""
        ...

    async def generate_presigned_upload_url(
        self,
        request: UploadInitiationRequest,
    ) -> PresignedUploadResult:
        """Generate a short-lived presigned URL for direct client upload."""
        ...

    async def generate_presigned_download_url(
        self,
        object_key: str,
        *,
        expires_in_seconds: int = 900,
        response_content_disposition: str | None = None,
    ) -> PresignedDownloadResult:
        """Generate a short-lived presigned URL for direct client download/streaming."""
        ...

    async def put_object_stream(
        self,
        object_key: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str,
        size_bytes: int,
        checksum_sha256: str | None = None,
        custom_metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        """Stream bytes directly to storage from the backend."""
        ...

    def get_object_stream(
        self,
        object_key: str,
        *,
        byte_range: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream bytes from storage."""
        ...

    async def delete_object(self, object_key: str) -> bool:
        """Idempotently delete an object from storage. Returns True if deleted or did not exist."""
        ...

    async def delete_objects_batch(self, object_keys: list[str]) -> list[str]:
        """Delete multiple objects in a batch. Returns the list of deleted keys."""
        ...

    async def initiate_multipart_upload(
        self,
        object_key: str,
        content_type: str,
        *,
        custom_metadata: dict[str, str] | None = None,
    ) -> MultipartInitiationResult:
        """Initiate a multipart upload session for large video files."""
        ...

    async def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> ObjectMetadata:
        """Complete and assemble a multipart upload session."""
        ...

    async def abort_multipart_upload(
        self,
        upload_id: str,
        object_key: str,
    ) -> bool:
        """Abort an active multipart upload and discard uploaded parts."""
        ...

    async def generate_presigned_part_upload_url(
        self,
        request: MultipartPartUploadRequest,
    ) -> MultipartPartUploadResult:
        """Presign a short-lived PUT URL for one part of a multipart upload."""
        ...

    async def list_objects(self, prefix: str, *, max_keys: int = 1000) -> list[str]:
        """List object keys under a prefix (bounded, for reconciliation)."""
        ...
