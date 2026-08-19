"""In-memory fake storage adapter for testing.

THIS IS A TEST IMPLEMENTATION ONLY.
NEVER USE IN PRODUCTION ENVIRONMENTS.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import uuid4

from backend.app.infrastructure.storage.exceptions import (
    ObjectNotFoundError,
    StorageIntegrityError,
    StorageUnavailableError,
)
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


class FakeStorageAdapter:
    """In-memory storage adapter implementing StoragePort for testing."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, ObjectMetadata]] = {}
        self._multipart_uploads: dict[str, dict[int, bytes]] = {}
        self._is_initialized = False
        self._simulate_unavailable = False

    def simulate_unavailable(self, unavailable: bool = True) -> None:
        """Helper for tests to simulate storage outage."""
        self._simulate_unavailable = unavailable

    async def initialize(self) -> Self:
        if self._simulate_unavailable:
            raise StorageUnavailableError("Simulated storage failure during initialize")
        self._is_initialized = True
        return self

    async def check_connectivity(self) -> bool:
        if self._simulate_unavailable:
            return False
        return self._is_initialized

    async def close(self) -> None:
        self._is_initialized = False

    def _ensure_available(self) -> None:
        if self._simulate_unavailable:
            raise StorageUnavailableError("Storage service is currently unavailable")

    async def get_object_metadata(self, object_key: str) -> ObjectMetadata | None:
        self._ensure_available()
        item = self._objects.get(object_key)
        return item[1] if item is not None else None

    async def object_exists(self, object_key: str) -> bool:
        self._ensure_available()
        return object_key in self._objects

    async def generate_presigned_upload_url(
        self,
        request: UploadInitiationRequest,
    ) -> PresignedUploadResult:
        self._ensure_available()
        expires_at = datetime.now(UTC) + timedelta(seconds=request.expires_in_seconds)
        return PresignedUploadResult(
            upload_url=f"https://fake-storage.local/{request.object_key}?upload=true",
            object_key=request.object_key,
            http_method="PUT",
            required_headers={"Content-Type": request.content_type},
            expires_in_seconds=request.expires_in_seconds,
            expires_at=expires_at,
        )

    async def generate_presigned_download_url(
        self,
        object_key: str,
        *,
        expires_in_seconds: int = 900,
        response_content_disposition: str | None = None,
    ) -> PresignedDownloadResult:
        self._ensure_available()
        if object_key not in self._objects:
            raise ObjectNotFoundError(object_key)

        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        disp_param = (
            f"&disposition={response_content_disposition}" if response_content_disposition else ""
        )
        return PresignedDownloadResult(
            download_url=f"https://fake-storage.local/{object_key}?download=true{disp_param}",
            object_key=object_key,
            expires_in_seconds=expires_in_seconds,
            expires_at=expires_at,
        )

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
        self._ensure_available()

        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        async for chunk in stream:
            chunks.append(chunk)
            hasher.update(chunk)

        data = b"".join(chunks)

        if len(data) != size_bytes:
            raise StorageIntegrityError(
                f"Size mismatch: expected {size_bytes} bytes, received {len(data)} bytes"
            )

        computed_sha256 = hasher.hexdigest()
        if checksum_sha256 is not None and computed_sha256 != checksum_sha256.lower():
            raise StorageIntegrityError(
                f"Checksum mismatch: expected {checksum_sha256}, got {computed_sha256}",
                expected_checksum=checksum_sha256,
                actual_checksum=computed_sha256,
            )

        meta = ObjectMetadata(
            object_key=object_key,
            size_bytes=len(data),
            content_type=content_type,
            etag=f'"{hashlib.md5(data).hexdigest()}"',
            last_modified=datetime.now(UTC),
            checksum_sha256=computed_sha256,
            custom_metadata=custom_metadata or {},
        )

        self._objects[object_key] = (data, meta)
        return meta

    def get_object_stream(
        self,
        object_key: str,
        *,
        byte_range: str | None = None,
    ) -> AsyncIterator[bytes]:
        self._ensure_available()
        item = self._objects.get(object_key)
        if item is None:
            raise ObjectNotFoundError(object_key)

        data, _ = item

        async def _generator() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
            yield data

        return _generator()

    async def delete_object(self, object_key: str) -> bool:
        self._ensure_available()
        self._objects.pop(object_key, None)
        return True

    async def delete_objects_batch(self, object_keys: list[str]) -> list[str]:
        self._ensure_available()
        deleted: list[str] = []
        for key in object_keys:
            self._objects.pop(key, None)
            deleted.append(key)
        return deleted

    async def list_objects(self, prefix: str, *, max_keys: int = 1000) -> list[str]:
        self._ensure_available()
        return [key for key in self._objects if key.startswith(prefix)][:max_keys]

    async def initiate_multipart_upload(
        self,
        object_key: str,
        content_type: str,
        *,
        custom_metadata: dict[str, str] | None = None,
    ) -> MultipartInitiationResult:
        self._ensure_available()
        upload_id = str(uuid4())
        self._multipart_uploads[upload_id] = {}
        return MultipartInitiationResult(upload_id=upload_id, object_key=object_key)

    async def generate_presigned_part_upload_url(
        self,
        request: MultipartPartUploadRequest,
    ) -> MultipartPartUploadResult:
        self._ensure_available()
        if request.upload_id not in self._multipart_uploads:
            raise StorageIntegrityError(f"Multipart upload ID '{request.upload_id}' not found")
        expires_at = datetime.now(UTC) + timedelta(seconds=request.expires_in_seconds)
        return MultipartPartUploadResult(
            part_number=request.part_number,
            upload_url=(
                f"https://fake-storage.local/{request.object_key}?uploadId="
                f"{request.upload_id}&partNumber={request.part_number}"
            ),
            expires_in_seconds=request.expires_in_seconds,
            expires_at=expires_at,
        )

    async def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> ObjectMetadata:
        self._ensure_available()
        parts = self._multipart_uploads.get(request.upload_id)
        if parts is None:
            # Idempotent replay of an already-completed upload: return
            # the assembled object metadata when the object exists.
            item = self._objects.get(request.object_key)
            if item is not None:
                return item[1]
            raise StorageIntegrityError(f"Multipart upload ID '{request.upload_id}' not found")

        missing = [p.part_number for p in request.parts if p.part_number not in parts]
        if missing:
            msg = (
                f"Multipart upload '{request.upload_id}' is missing parts "
                f"{missing} — refusing to complete"
            )
            raise StorageIntegrityError(msg)

        sorted_parts = sorted(request.parts, key=lambda p: p.part_number)
        combined_bytes = b"".join(parts.get(p.part_number, b"") for p in sorted_parts)

        meta = ObjectMetadata(
            object_key=request.object_key,
            size_bytes=len(combined_bytes),
            content_type="application/octet-stream",
            etag=f'"{hashlib.md5(combined_bytes).hexdigest()}"',
            last_modified=datetime.now(UTC),
            checksum_sha256=hashlib.sha256(combined_bytes).hexdigest(),
        )
        self._objects[request.object_key] = (combined_bytes, meta)
        del self._multipart_uploads[request.upload_id]
        return meta

    async def abort_multipart_upload(
        self,
        upload_id: str,
        object_key: str,
    ) -> bool:
        self._ensure_available()
        self._multipart_uploads.pop(upload_id, None)
        return True
