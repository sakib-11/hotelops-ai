"""Concrete S3-compatible Object Storage Adapter.

Implements StoragePort using boto3 against MinIO CE (development/CI)
and AWS S3 / S3-compatible providers (production).

This module is the ONLY place provider SDKs (boto3/botocore) are
imported. Application and domain services depend solely on the
provider-independent StoragePort protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from backend.app.infrastructure.storage.exceptions import (
    ObjectNotFoundError,
    StorageError,
    StorageIntegrityError,
    StorageOperationError,
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

if TYPE_CHECKING:
    from backend.app.infrastructure.config import Settings

logger = logging.getLogger(__name__)


def _normalize_s3_error(
    exc: Exception,
    object_key: str | None = None,
    bucket: str | None = None,
) -> StorageError:
    """Translate boto3/botocore exceptions into provider-independent StorageErrors."""
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
        return StorageUnavailableError(
            f"Object storage endpoint unreachable: {exc}",
            cause=exc,
        )

    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)

        if error_code in ("NoSuchBucket", "BucketNotFound"):
            return StorageUnavailableError(
                f"Configured storage bucket '{bucket or 'unknown'}' does not exist",
                cause=exc,
            )

        if error_code in ("NoSuchUpload", "InvalidPart", "InvalidPartOrder", "EntityTooSmall"):
            # Multipart-lifecycle errors — deterministic, retryable by
            # the caller (re-initiate or abort), never transient.
            return StorageOperationError(
                f"Multipart upload operation failed [{error_code}]: {exc}",
                cause=exc,
            )

        if status_code == 404 or error_code in ("404", "NoSuchKey", "NotFound"):
            return ObjectNotFoundError(object_key or "unknown", cause=exc)

        if error_code == "AccessDenied":
            return StorageOperationError(
                f"Access denied for storage operation on bucket '{bucket or 'unknown'}'",
                cause=exc,
            )

        return StorageOperationError(
            f"S3 operation failed [{error_code}]: {exc}",
            cause=exc,
        )

    if isinstance(exc, BotoCoreError):
        return StorageUnavailableError(
            f"S3 client error: {exc}",
            cause=exc,
        )

    if isinstance(exc, StorageError):
        return exc

    return StorageOperationError(f"Unexpected storage error: {exc}", cause=exc)


class _HashingFileLike:
    """Synchronous file-like bridge over an async byte stream.

    boto3's ``upload_fileobj`` requires a sync ``read()`` object, but the
    StoragePort delivers async iterators. This wrapper pulls chunks from
    the async iterator on the owning event loop (via
    ``run_coroutine_threadsafe``) while computing the SHA-256 digest as
    bytes flow — so checksums are verified without ever buffering the
    whole object in memory (CCTV recordings are large).
    """

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream
        self._loop = asyncio.get_running_loop()
        self._hasher = hashlib.sha256()
        self._buffer = bytearray()
        self._done = False

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes, pulling from the async iterator."""
        if size is None or size < 0:
            size = 1024 * 1024
        while len(self._buffer) < size and not self._done:
            try:
                next_chunk: Coroutine[Any, Any, bytes] = cast(
                    Coroutine[Any, Any, bytes], self._stream.__anext__()
                )
                chunk = asyncio.run_coroutine_threadsafe(next_chunk, self._loop).result()
            except StopAsyncIteration:
                self._done = True
                break
            self._hasher.update(chunk)
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result


class S3StorageAdapter:
    """Concrete S3 storage adapter satisfying StoragePort."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bucket = settings.object_storage_bucket
        self._endpoint_url = (
            settings.object_storage_endpoint if settings.object_storage_endpoint else None
        )
        self._region = settings.object_storage_region
        self._client: Any | None = None
        self._is_initialized = False

    def _create_boto_client(self) -> Any:
        """Create a configured boto3 S3 client."""
        boto_config = Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._settings.object_storage_access_key,
            aws_secret_access_key=self._settings.object_storage_secret_key,
            region_name=self._region,
            use_ssl=self._settings.object_storage_use_ssl,
            config=boto_config,
        )

    async def initialize(self) -> None:
        """Initialize the S3 client and verify bucket availability."""
        try:
            if self._client is None:
                self._client = await asyncio.to_thread(self._create_boto_client)
            await self._head_bucket()
            self._is_initialized = True
            logger.info(
                "S3StorageAdapter initialized successfully (bucket=%s, endpoint=%s)",
                self._bucket,
                self._endpoint_url,
            )
        except Exception as exc:
            norm_err = _normalize_s3_error(exc, bucket=self._bucket)
            logger.error("Failed to initialize S3StorageAdapter: %s", norm_err)
            raise norm_err from exc

    async def _head_bucket(self) -> None:
        """Execute head_bucket check against S3."""
        if self._client is None:
            msg = "S3 client not initialized"
            raise StorageUnavailableError(msg)
        await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)

    def _require_client(self) -> Any:
        if self._client is None:
            msg = "S3 client not initialized"
            raise StorageUnavailableError(msg)
        return self._client

    async def check_connectivity(self) -> bool:
        """Check if storage backend and bucket are reachable."""
        try:
            if self._client is None:
                self._client = await asyncio.to_thread(self._create_boto_client)
            await self._head_bucket()
            return True
        except Exception as exc:
            logger.warning(
                "Storage connectivity check failed for bucket '%s': %s",
                self._bucket,
                exc,
            )
            return False

    async def close(self) -> None:
        """Close S3 client session."""
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as exc:
                logger.debug("Error closing S3 client: %s", exc)
            finally:
                self._client = None
                self._is_initialized = False

    # ---------------------------------------------------------------------
    # Basic object operations
    # ---------------------------------------------------------------------

    async def object_exists(self, object_key: str) -> bool:
        """Check if an object exists in the S3 bucket."""
        client = self._require_client()
        try:
            await asyncio.to_thread(
                client.head_object,
                Bucket=self._bucket,
                Key=object_key,
            )
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchBucket", "BucketNotFound"):
                raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status_code == 404 or error_code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc

    async def get_object_metadata(self, object_key: str) -> ObjectMetadata | None:
        """Retrieve metadata for an object from S3."""
        client = self._require_client()
        try:
            resp = await asyncio.to_thread(
                client.head_object,
                Bucket=self._bucket,
                Key=object_key,
            )
            custom_metadata = resp.get("Metadata", {})
            checksum_sha256 = (
                resp.get("ChecksumSHA256")
                or custom_metadata.get("checksum-sha256")
                or custom_metadata.get("checksum_sha256")
            )
            return ObjectMetadata(
                object_key=object_key,
                size_bytes=resp.get("ContentLength", 0),
                content_type=resp.get("ContentType", "application/octet-stream"),
                etag=resp.get("ETag", "").strip('"'),
                last_modified=resp.get("LastModified") or datetime.now(UTC),
                checksum_sha256=checksum_sha256,
                custom_metadata=custom_metadata,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchBucket", "BucketNotFound"):
                raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status_code == 404 or error_code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc

    async def delete_object(self, object_key: str) -> bool:
        """Idempotently delete an object from S3."""
        client = self._require_client()
        try:
            await asyncio.to_thread(
                client.delete_object,
                Bucket=self._bucket,
                Key=object_key,
            )
            return True
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc

    async def delete_objects_batch(self, object_keys: list[str]) -> list[str]:
        """Delete multiple objects in a single batch S3 call."""
        if not object_keys:
            return []
        client = self._require_client()
        try:
            delete_payload = {"Objects": [{"Key": k} for k in object_keys], "Quiet": True}
            resp = await asyncio.to_thread(
                client.delete_objects,
                Bucket=self._bucket,
                Delete=delete_payload,
            )
            deleted = [d["Key"] for d in resp.get("Deleted", [])]
            return deleted or object_keys
        except Exception as exc:
            raise _normalize_s3_error(exc, bucket=self._bucket) from exc

    async def list_objects(self, prefix: str, *, max_keys: int = 1000) -> list[str]:
        """List object keys under a prefix (bounded, for reconciliation)."""
        client = self._require_client()
        keys: list[str] = []
        paginator_config = {"PageSize": min(max_keys, 1000)}
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in await asyncio.to_thread(
                paginator.paginate,
                Bucket=self._bucket,
                Prefix=prefix,
                PaginationConfig=paginator_config,
            ):
                for entry in page.get("Contents", []):
                    keys.append(entry["Key"])
                    if len(keys) >= max_keys:
                        return keys
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchBucket", "BucketNotFound"):
                raise _normalize_s3_error(exc, bucket=self._bucket) from exc
            raise _normalize_s3_error(exc, object_key=prefix, bucket=self._bucket) from exc
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=prefix, bucket=self._bucket) from exc
        return keys

    # ---------------------------------------------------------------------
    # Presigned URLs (private bucket — access is always server-mediated)
    # ---------------------------------------------------------------------

    async def generate_presigned_upload_url(
        self,
        request: UploadInitiationRequest,
    ) -> PresignedUploadResult:
        """Presign a short-lived PUT URL for direct client upload."""
        client = self._require_client()
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": request.object_key,
            "ContentType": request.content_type,
        }
        if request.checksum_sha256:
            params["ChecksumSHA256"] = request.checksum_sha256
        try:
            url = await asyncio.to_thread(
                client.generate_presigned_url,
                "put_object",
                Params=params,
                ExpiresIn=request.expires_in_seconds,
            )
        except Exception as exc:
            raise _normalize_s3_error(
                exc, object_key=request.object_key, bucket=self._bucket
            ) from exc

        expires_at = datetime.now(UTC) + timedelta(seconds=request.expires_in_seconds)
        return PresignedUploadResult(
            upload_url=url,
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
        """Presign a short-lived GET URL for server-authorized download."""
        client = self._require_client()
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_key,
        }
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        try:
            url = await asyncio.to_thread(
                client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=expires_in_seconds,
            )
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc

        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        return PresignedDownloadResult(
            download_url=url,
            object_key=object_key,
            expires_in_seconds=expires_in_seconds,
            expires_at=expires_at,
        )

    # ---------------------------------------------------------------------
    # Streaming operations (backend-mediated transfers)
    # ---------------------------------------------------------------------

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
        """Stream bytes directly to storage, verifying size and checksum.

        SHA-256 is computed while bytes flow (bounded memory). On a
        checksum mismatch the partially uploaded object is deleted so a
        corrupted object can never remain AVAILABLE.
        """
        client = self._require_client()
        hashing_stream = _HashingFileLike(stream)
        extra_args: dict[str, Any] = {
            "ContentType": content_type,
        }
        if custom_metadata:
            extra_args["Metadata"] = custom_metadata

        try:
            await asyncio.to_thread(
                client.upload_fileobj,
                hashing_stream,
                self._bucket,
                object_key,
                ExtraArgs=extra_args,
            )
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc

        meta = await self.get_object_metadata(object_key)
        if meta is None:
            msg = f"Object '{object_key}' disappeared immediately after upload"
            raise StorageIntegrityError(msg)

        computed_sha256 = hashing_stream.hexdigest
        if meta.size_bytes != size_bytes:
            await self.delete_object(object_key)
            msg = (
                f"Size mismatch for '{object_key}': expected {size_bytes} bytes, "
                f"storage reports {meta.size_bytes}"
            )
            raise StorageIntegrityError(msg)

        if checksum_sha256 is not None and computed_sha256 != checksum_sha256.lower():
            await self.delete_object(object_key)
            raise StorageIntegrityError(
                f"Checksum mismatch for '{object_key}': expected {checksum_sha256}, "
                f"computed {computed_sha256}",
                expected_checksum=checksum_sha256,
                actual_checksum=computed_sha256,
            )

        # Persist the authoritative checksum on the object as metadata so
        # later head-object checks can cross-verify without re-reading.
        if custom_metadata is not None and "checksum-sha256" not in custom_metadata:
            try:
                merged_meta = dict(custom_metadata)
                merged_meta["checksum-sha256"] = computed_sha256
                await asyncio.to_thread(
                    client.copy_object,
                    Bucket=self._bucket,
                    Key=object_key,
                    CopySource={"Bucket": self._bucket, "Key": object_key},
                    Metadata=merged_meta,
                    MetadataDirective="REPLACE",
                    ContentType=content_type,
                )
            except Exception:
                logger.debug("Failed to attach checksum metadata to '%s'", object_key)

        return ObjectMetadata(
            object_key=object_key,
            size_bytes=meta.size_bytes,
            content_type=meta.content_type,
            etag=meta.etag,
            last_modified=meta.last_modified,
            checksum_sha256=computed_sha256,
            custom_metadata=meta.custom_metadata,
        )

    def get_object_stream(
        self,
        object_key: str,
        *,
        byte_range: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream bytes from storage in bounded chunks."""
        client = self._require_client()

        async def _generator() -> AsyncIterator[bytes]:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": object_key}
            if byte_range:
                kwargs["Range"] = byte_range
            try:
                resp = await asyncio.to_thread(client.get_object, **kwargs)
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                if status_code == 404 or error_code in ("404", "NoSuchKey", "NotFound"):
                    raise ObjectNotFoundError(object_key, cause=exc) from exc
                raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
            except Exception as exc:
                raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
            try:
                body = resp["Body"]
                while True:
                    chunk = await asyncio.to_thread(body.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(body.close)

        return _generator()

    # ---------------------------------------------------------------------
    # Multipart uploads (large CCTV recordings)
    # ---------------------------------------------------------------------

    async def initiate_multipart_upload(
        self,
        object_key: str,
        content_type: str,
        *,
        custom_metadata: dict[str, str] | None = None,
    ) -> MultipartInitiationResult:
        """Initiate a multipart upload session for large video files."""
        client = self._require_client()
        try:
            resp = await asyncio.to_thread(
                client.create_multipart_upload,
                Bucket=self._bucket,
                Key=object_key,
                ContentType=content_type,
                Metadata=custom_metadata or {},
            )
        except Exception as exc:
            raise _normalize_s3_error(exc, object_key=object_key, bucket=self._bucket) from exc
        return MultipartInitiationResult(
            upload_id=resp["UploadId"],
            object_key=object_key,
        )

    async def generate_presigned_part_upload_url(
        self,
        request: MultipartPartUploadRequest,
    ) -> MultipartPartUploadResult:
        """Presign a short-lived PUT URL for one multipart part."""
        client = self._require_client()
        try:
            url = await asyncio.to_thread(
                client.generate_presigned_url,
                "upload_part",
                Params={
                    "Bucket": self._bucket,
                    "Key": request.object_key,
                    "UploadId": request.upload_id,
                    "PartNumber": request.part_number,
                },
                ExpiresIn=request.expires_in_seconds,
            )
        except Exception as exc:
            raise _normalize_s3_error(
                exc,
                object_key=request.object_key,
                bucket=self._bucket,
            ) from exc

        expires_at = datetime.now(UTC) + timedelta(seconds=request.expires_in_seconds)
        return MultipartPartUploadResult(
            part_number=request.part_number,
            upload_url=url,
            expires_in_seconds=request.expires_in_seconds,
            expires_at=expires_at,
        )

    async def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> ObjectMetadata:
        """Complete and assemble a multipart upload session.

        The part manifest is cross-checked against the provider's own
        uploaded-part list before completion (client ETags are never
        trusted blindly). Completion is idempotent: repeating a
        completed upload returns the assembled object's metadata.
        """
        client = self._require_client()

        # 1. Cross-check the manifest against the provider's uploaded parts.
        try:
            listed = await asyncio.to_thread(
                client.list_parts,
                Bucket=self._bucket,
                Key=request.object_key,
                UploadId=request.upload_id,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchUpload",):
                # Idempotent path — the upload was already completed
                # (or aborted). Return the final object metadata when it
                # exists; otherwise surface the provider error.
                meta = await self.get_object_metadata(request.object_key)
                if meta is not None:
                    return meta
            raise _normalize_s3_error(
                exc,
                object_key=request.object_key,
                bucket=self._bucket,
            ) from exc
        except Exception as exc:
            raise _normalize_s3_error(
                exc,
                object_key=request.object_key,
                bucket=self._bucket,
            ) from exc

        provider_parts = {p["PartNumber"]: p["ETag"] for p in listed.get("Parts", [])}
        missing = [p.part_number for p in request.parts if p.part_number not in provider_parts]
        if missing:
            msg = (
                f"Multipart upload '{request.upload_id}' is missing parts "
                f"{missing} — refusing to complete"
            )
            raise StorageIntegrityError(msg)

        ordered = sorted(request.parts, key=lambda p: p.part_number)
        for part in ordered:
            if provider_parts.get(part.part_number) != part.etag:
                msg = f"ETag mismatch for part {part.part_number} of upload '{request.upload_id}'"
                raise StorageIntegrityError(msg)

        try:
            resp = await asyncio.to_thread(
                client.complete_multipart_upload,
                Bucket=self._bucket,
                Key=request.object_key,
                UploadId=request.upload_id,
                MultipartUpload={
                    "Parts": [{"PartNumber": p.part_number, "ETag": p.etag} for p in ordered]
                },
            )
        except Exception as exc:
            raise _normalize_s3_error(
                exc,
                object_key=request.object_key,
                bucket=self._bucket,
            ) from exc

        meta = await self.get_object_metadata(request.object_key)
        if meta is None:
            meta = ObjectMetadata(
                object_key=request.object_key,
                size_bytes=int(resp.get("ContentLength", 0) or 0),
                content_type=resp.get("ContentType", "application/octet-stream"),
                etag=(resp.get("ETag") or "").strip('"'),
                last_modified=datetime.now(UTC),
            )
        return meta

    async def abort_multipart_upload(
        self,
        upload_id: str,
        object_key: str,
    ) -> bool:
        """Abort an active multipart upload and discard uploaded parts."""
        client = self._require_client()
        try:
            await asyncio.to_thread(
                client.abort_multipart_upload,
                Bucket=self._bucket,
                Key=object_key,
                UploadId=upload_id,
            )
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            # Aborting an already-aborted/completed upload is a no-op.
            if error_code == "NoSuchUpload":
                return True
            raise _normalize_s3_error(
                exc,
                object_key=object_key,
                bucket=self._bucket,
            ) from exc
        except Exception as exc:
            raise _normalize_s3_error(
                exc,
                object_key=object_key,
                bucket=self._bucket,
            ) from exc
