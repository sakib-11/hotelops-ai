"""Unit tests for S3StorageAdapter and storage configuration (Task 9.4).

Validates:
- Settings validation for object storage parameters
- S3StorageAdapter initialization and boto3 client configuration
- Protocol conformance (StoragePort)
- Object existence and metadata extraction via mocked S3 responses
- Idempotent single and batch object deletion
- Comprehensive error normalization from boto3/botocore exceptions
- ReadinessService integration with StoragePort
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
)

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.storage import (
    MultipartCompleteRequest,
    MultipartPartInfo,
    MultipartPartUploadRequest,
    ObjectNotFoundError,
    S3StorageAdapter,
    StorageError,
    StorageIntegrityError,
    StorageOperationError,
    StoragePort,
    StorageUnavailableError,
    UploadInitiationRequest,
)


@pytest.fixture
def test_settings() -> Settings:
    """Create test Settings instance."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
        OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
        OBJECT_STORAGE_ACCESS_KEY="test-access-key",
        OBJECT_STORAGE_SECRET_KEY="test-secret-key",
        OBJECT_STORAGE_REGION="us-east-1",
        OBJECT_STORAGE_USE_SSL=False,
    )


class TestStorageSettingsValidation:
    """Tests for object storage configuration validation."""

    def test_valid_bucket_name(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="test",
            OBJECT_STORAGE_BUCKET="valid-bucket-name-123",
        )
        assert settings.object_storage_bucket == "valid-bucket-name-123"

    def test_bucket_name_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="between 3 and 63"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                app_env="test",
                OBJECT_STORAGE_BUCKET="ab",
            )

    def test_bucket_name_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="between 3 and 63"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                app_env="test",
                OBJECT_STORAGE_BUCKET="a" * 64,
            )


class TestS3StorageAdapter:
    """Tests for S3StorageAdapter using mocked boto3 client."""

    def test_implements_storage_port_protocol(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        assert isinstance(adapter, StoragePort)

    @pytest.mark.asyncio
    async def test_initialize_and_head_bucket_success(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        adapter._client = mock_client

        await adapter.initialize()
        assert adapter._is_initialized is True
        mock_client.head_bucket.assert_called_once_with(Bucket="hotelops-test-bucket")

    @pytest.mark.asyncio
    async def test_check_connectivity_success(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        adapter._client = mock_client

        assert await adapter.check_connectivity() is True

    @pytest.mark.asyncio
    async def test_check_connectivity_failure_returns_false(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = EndpointConnectionError(endpoint_url="http://bad")
        adapter._client = mock_client

        assert await adapter.check_connectivity() is False

    @pytest.mark.asyncio
    async def test_object_exists_returns_true_when_head_succeeds(
        self, test_settings: Settings
    ) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ContentLength": 1024}
        adapter._client = mock_client

        exists = await adapter.object_exists("tenants/t1/venues/v1/evidence/2026/08/10/a1.jpg")
        assert exists is True
        mock_client.head_object.assert_called_once_with(
            Bucket="hotelops-test-bucket",
            Key="tenants/t1/venues/v1/evidence/2026/08/10/a1.jpg",
        )

    @pytest.mark.asyncio
    async def test_object_exists_returns_false_on_404(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        err_response = {
            "Error": {"Code": "404", "Message": "Not Found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_client.head_object.side_effect = ClientError(err_response, "HeadObject")
        adapter._client = mock_client

        exists = await adapter.object_exists("tenants/t1/venues/v1/evidence/2026/08/10/missing.jpg")
        assert exists is False

    @pytest.mark.asyncio
    async def test_get_object_metadata_success(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        now = datetime.now(UTC)
        mock_client.head_object.return_value = {
            "ContentLength": 2048,
            "ContentType": "image/jpeg",
            "ETag": '"abc123etag"',
            "LastModified": now,
            "Metadata": {
                "checksum-sha256": "69808d9ea5dc4fdb2d7d59e7cd5601073bf9355c597d8b94257e32cd3c8dce7f",
                "custom-tag": "camera-01",
            },
        }
        adapter._client = mock_client

        key = "tenants/t1/venues/v1/evidence/2026/08/10/a2.jpg"
        meta = await adapter.get_object_metadata(key)

        assert meta is not None
        assert meta.object_key == key
        assert meta.size_bytes == 2048
        assert meta.content_type == "image/jpeg"
        assert meta.etag == "abc123etag"
        assert meta.last_modified == now
        assert (
            meta.checksum_sha256
            == "69808d9ea5dc4fdb2d7d59e7cd5601073bf9355c597d8b94257e32cd3c8dce7f"
        )
        assert meta.custom_metadata["custom-tag"] == "camera-01"

    @pytest.mark.asyncio
    async def test_get_object_metadata_returns_none_on_404(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        err_response = {
            "Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_client.head_object.side_effect = ClientError(err_response, "HeadObject")
        adapter._client = mock_client

        meta = await adapter.get_object_metadata(
            "tenants/t1/venues/v1/evidence/2026/08/10/nonexistent.jpg"
        )
        assert meta is None

    @pytest.mark.asyncio
    async def test_delete_object_idempotent(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.delete_object.return_value = {}
        adapter._client = mock_client

        key = "tenants/t1/venues/v1/evidence/2026/08/10/a3.jpg"
        result = await adapter.delete_object(key)
        assert result is True
        mock_client.delete_object.assert_called_once_with(
            Bucket="hotelops-test-bucket",
            Key=key,
        )

    @pytest.mark.asyncio
    async def test_delete_objects_batch(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        keys = [
            "tenants/t1/venues/v1/evidence/2026/08/10/k1.jpg",
            "tenants/t1/venues/v1/evidence/2026/08/10/k2.jpg",
        ]
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": keys[0]}, {"Key": keys[1]}],
        }
        adapter._client = mock_client

        deleted = await adapter.delete_objects_batch(keys)
        assert deleted == keys
        mock_client.delete_objects.assert_called_once_with(
            Bucket="hotelops-test-bucket",
            Delete={"Objects": [{"Key": keys[0]}, {"Key": keys[1]}], "Quiet": True},
        )


class TestS3ErrorNormalization:
    """Tests asserting all boto3/botocore errors translate to normalized StorageErrors."""

    @pytest.mark.asyncio
    async def test_endpoint_connection_error_translates_to_unavailable(
        self, test_settings: Settings
    ) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.head_object.side_effect = EndpointConnectionError(
            endpoint_url="http://bad:9000"
        )
        adapter._client = mock_client

        with pytest.raises(StorageUnavailableError, match="endpoint unreachable"):
            await adapter.object_exists("some-key")

    @pytest.mark.asyncio
    async def test_connect_timeout_translates_to_unavailable(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.head_object.side_effect = ConnectTimeoutError(
            endpoint_url="http://timeout:9000"
        )
        adapter._client = mock_client

        with pytest.raises(StorageUnavailableError, match="endpoint unreachable"):
            await adapter.object_exists("some-key")

    @pytest.mark.asyncio
    async def test_nosuchbucket_translates_to_unavailable(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        err_response = {
            "Error": {"Code": "NoSuchBucket", "Message": "Bucket does not exist"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_client.head_object.side_effect = ClientError(err_response, "HeadObject")
        adapter._client = mock_client

        with pytest.raises(StorageUnavailableError, match="does not exist"):
            await adapter.object_exists("some-key")

    @pytest.mark.asyncio
    async def test_access_denied_translates_to_operation_error(
        self, test_settings: Settings
    ) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        err_response = {
            "Error": {"Code": "AccessDenied", "Message": "Access Denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }
        mock_client.delete_object.side_effect = ClientError(err_response, "DeleteObject")
        adapter._client = mock_client

        with pytest.raises(StorageOperationError, match="Access denied"):
            await adapter.delete_object("some-key")

    @pytest.mark.asyncio
    async def test_general_botocore_error_translates_to_storage_error(
        self, test_settings: Settings
    ) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.delete_object.side_effect = BotoCoreError()
        adapter._client = mock_client

        with pytest.raises(StorageError):
            await adapter.delete_object("some-key")


class TestS3PresignedUrls:
    """Tests for presigned upload/download URL generation (Task 9.4/9.11)."""

    @pytest.mark.asyncio
    async def test_generate_presigned_upload_url(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://presigned.example/upload?X=1"
        adapter._client = mock_client

        req = UploadInitiationRequest(
            object_key="tenants/t1/venues/v1/recordings/2026/08/10/r1.mp4",
            content_type="video/mp4",
            expires_in_seconds=600,
            checksum_sha256="abc" * 21 + "a",
        )
        result = await adapter.generate_presigned_upload_url(req)

        assert result.upload_url == "https://presigned.example/upload?X=1"
        assert result.http_method == "PUT"
        assert result.expires_in_seconds == 600
        mock_client.generate_presigned_url.assert_called_once_with(
            "put_object",
            Params={
                "Bucket": "hotelops-test-bucket",
                "Key": req.object_key,
                "ContentType": "video/mp4",
                "ChecksumSHA256": req.checksum_sha256,
            },
            ExpiresIn=600,
        )

    @pytest.mark.asyncio
    async def test_generate_presigned_download_url(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://presigned.example/get"
        adapter._client = mock_client

        result = await adapter.generate_presigned_download_url(
            "tenants/t1/venues/v1/evidence/2026/08/10/e1.jpg",
            expires_in_seconds=300,
            response_content_disposition="inline",
        )

        assert result.expires_in_seconds == 300
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={
                "Bucket": "hotelops-test-bucket",
                "Key": "tenants/t1/venues/v1/evidence/2026/08/10/e1.jpg",
                "ResponseContentDisposition": "inline",
            },
            ExpiresIn=300,
        )


class TestS3Streaming:
    """Tests for streaming put/get (Task 9.4)."""

    @staticmethod
    def _consuming_upload(fileobj, bucket: str, key: str, ExtraArgs=None) -> None:
        """A real upload_fileobj that drains the file-like bridge."""
        while fileobj.read(1024 * 1024):
            pass

    @pytest.mark.asyncio
    async def test_put_object_stream_success(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.upload_fileobj.side_effect = self._consuming_upload
        adapter._client = mock_client

        payload = b"hotelops video bytes"
        sha256 = hashlib.sha256(payload).hexdigest()
        mock_client.head_object.return_value = {
            "ContentLength": len(payload),
            "ContentType": "video/mp4",
            "ETag": '"abc"',
            "LastModified": datetime.now(UTC),
            "Metadata": {},
        }

        async def gen():
            yield payload[:8]
            yield payload[8:]

        meta = await adapter.put_object_stream(
            "key1",
            gen(),
            content_type="video/mp4",
            size_bytes=len(payload),
            checksum_sha256=sha256,
        )
        assert meta.size_bytes == len(payload)
        assert meta.checksum_sha256 == sha256
        mock_client.upload_fileobj.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_object_stream_checksum_mismatch_deletes_object(
        self, test_settings: Settings
    ) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.upload_fileobj.side_effect = self._consuming_upload
        adapter._client = mock_client

        payload = b"corrupted payload"
        wrong_sha = "0" * 64
        mock_client.head_object.return_value = {
            "ContentLength": len(payload),
            "ContentType": "video/mp4",
            "ETag": '"abc"',
            "LastModified": datetime.now(UTC),
            "Metadata": {},
        }

        async def gen():
            yield payload

        with pytest.raises(StorageIntegrityError, match="Checksum mismatch"):
            await adapter.put_object_stream(
                "key2",
                gen(),
                content_type="video/mp4",
                size_bytes=len(payload),
                checksum_sha256=wrong_sha,
            )
        # The corrupted object is removed — never left AVAILABLE.
        mock_client.delete_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_object_stream_size_mismatch_deletes_object(
        self, test_settings: Settings
    ) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.upload_fileobj.side_effect = self._consuming_upload
        adapter._client = mock_client
        payload = b"payload"
        mock_client.head_object.return_value = {
            "ContentLength": 9999,  # storage disagrees with what we sent
            "ContentType": "video/mp4",
            "ETag": '"abc"',
            "LastModified": datetime.now(UTC),
            "Metadata": {},
        }

        async def gen():
            yield payload

        with pytest.raises(StorageIntegrityError, match="Size mismatch"):
            await adapter.put_object_stream(
                "key3", gen(), content_type="video/mp4", size_bytes=len(payload)
            )
        mock_client.delete_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_object_stream_chunks(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        body = MagicMock()
        body.read.side_effect = [b"chunk1", b"chunk2", b""]
        mock_client.get_object.return_value = {"Body": body}
        adapter._client = mock_client

        chunks: list[bytes] = []
        async for chunk in adapter.get_object_stream("key4", byte_range="bytes=0-99"):
            chunks.append(chunk)

        assert chunks == [b"chunk1", b"chunk2"]
        mock_client.get_object.assert_called_once_with(
            Bucket="hotelops-test-bucket", Key="key4", Range="bytes=0-99"
        )

    @pytest.mark.asyncio
    async def test_get_object_stream_raises_not_found(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        err_response = {
            "Error": {"Code": "NoSuchKey", "Message": "Missing"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_client.get_object.side_effect = ClientError(err_response, "GetObject")
        adapter._client = mock_client

        with pytest.raises(ObjectNotFoundError):
            async for _ in adapter.get_object_stream("missing-key"):
                pass


class TestS3Multipart:
    """Tests for multipart upload lifecycle (Task 9.8)."""

    @pytest.mark.asyncio
    async def test_initiate_multipart_upload(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.create_multipart_upload.return_value = {"UploadId": "upload-123"}
        adapter._client = mock_client

        result = await adapter.initiate_multipart_upload("key5", "video/mp4")
        assert result.upload_id == "upload-123"
        assert result.object_key == "key5"
        mock_client.create_multipart_upload.assert_called_once_with(
            Bucket="hotelops-test-bucket", Key="key5", ContentType="video/mp4", Metadata={}
        )

    @pytest.mark.asyncio
    async def test_generate_presigned_part_upload_url(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://presigned.example/part"
        adapter._client = mock_client

        result = await adapter.generate_presigned_part_upload_url(
            MultipartPartUploadRequest(
                upload_id="upload-123", object_key="key6", part_number=4, expires_in_seconds=900
            )
        )
        assert result.part_number == 4
        mock_client.generate_presigned_url.assert_called_once_with(
            "upload_part",
            Params={
                "Bucket": "hotelops-test-bucket",
                "Key": "key6",
                "UploadId": "upload-123",
                "PartNumber": 4,
            },
            ExpiresIn=900,
        )

    def _complete_request(self) -> MultipartCompleteRequest:
        return MultipartCompleteRequest(
            upload_id="upload-123",
            object_key="key7",
            parts=[
                MultipartPartInfo(part_number=1, etag="etag-1"),
                MultipartPartInfo(part_number=2, etag="etag-2"),
            ],
        )

    @pytest.mark.asyncio
    async def test_complete_multipart_upload_success(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        adapter._client = mock_client
        mock_client.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 1, "ETag": "etag-1"},
                {"PartNumber": 2, "ETag": "etag-2"},
            ]
        }
        mock_client.complete_multipart_upload.return_value = {"ETag": '"final"'}
        mock_client.head_object.return_value = {
            "ContentLength": 2048,
            "ContentType": "video/mp4",
            "ETag": '"final"',
            "LastModified": datetime.now(UTC),
            "Metadata": {},
        }

        meta = await adapter.complete_multipart_upload(self._complete_request())
        assert meta.size_bytes == 2048
        assert meta.etag == "final"
        mock_client.complete_multipart_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_multipart_rejects_missing_part(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        adapter._client = mock_client
        # Part 2 was never uploaded to the provider.
        mock_client.list_parts.return_value = {"Parts": [{"PartNumber": 1, "ETag": "etag-1"}]}

        with pytest.raises(StorageIntegrityError, match="missing parts"):
            await adapter.complete_multipart_upload(self._complete_request())
        mock_client.complete_multipart_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_complete_multipart_rejects_etag_mismatch(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        adapter._client = mock_client
        mock_client.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 1, "ETag": "etag-WRONG"},
                {"PartNumber": 2, "ETag": "etag-2"},
            ]
        }

        with pytest.raises(StorageIntegrityError, match="ETag mismatch"):
            await adapter.complete_multipart_upload(self._complete_request())

    @pytest.mark.asyncio
    async def test_complete_multipart_idempotent_replay(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        adapter._client = mock_client
        # The upload was already completed: list_parts reports NoSuchUpload.
        err_response = {
            "Error": {"Code": "NoSuchUpload", "Message": "The specified upload does not exist."},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_client.list_parts.side_effect = ClientError(err_response, "ListParts")
        mock_client.head_object.return_value = {
            "ContentLength": 2048,
            "ContentType": "video/mp4",
            "ETag": '"final"',
            "LastModified": datetime.now(UTC),
            "Metadata": {},
        }

        meta = await adapter.complete_multipart_upload(self._complete_request())
        assert meta.size_bytes == 2048
        mock_client.complete_multipart_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_multipart_upload(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        mock_client.abort_multipart_upload.return_value = {}
        adapter._client = mock_client

        assert await adapter.abort_multipart_upload("upload-123", "key8") is True
        mock_client.abort_multipart_upload.assert_called_once_with(
            Bucket="hotelops-test-bucket", Key="key8", UploadId="upload-123"
        )

    @pytest.mark.asyncio
    async def test_abort_multipart_no_such_upload_is_noop(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        err_response = {
            "Error": {"Code": "NoSuchUpload", "Message": "Already aborted"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_client.abort_multipart_upload.side_effect = ClientError(
            err_response, "AbortMultipartUpload"
        )
        adapter._client = mock_client

        assert await adapter.abort_multipart_upload("upload-123", "key8") is True


class TestS3ListObjects:
    """Tests for bounded prefix listing (Task 9.13 reconciliation)."""

    @pytest.mark.asyncio
    async def test_list_objects_returns_keys(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "tenants/t1/venues/v1/temporary/2026/08/10/a.bin"}]}
        ]
        mock_client.get_paginator.return_value = paginator
        adapter._client = mock_client

        keys = await adapter.list_objects("tenants/t1/venues/v1/temporary/", max_keys=100)
        assert keys == ["tenants/t1/venues/v1/temporary/2026/08/10/a.bin"]
        mock_client.get_paginator.assert_called_once_with("list_objects_v2")

    @pytest.mark.asyncio
    async def test_list_objects_empty(self, test_settings: Settings) -> None:
        adapter = S3StorageAdapter(test_settings)
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]
        mock_client.get_paginator.return_value = paginator
        adapter._client = mock_client

        assert await adapter.list_objects("tenants/t1/venues/v1/recordings/") == []
