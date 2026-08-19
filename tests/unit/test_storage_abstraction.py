"""Unit tests for the provider-independent storage abstraction (Task 9.3).

Validates:
- Typed models, object references, and metadata structures
- Deterministic object key generation and validation
- Path traversal rejection
- Storage error hierarchy
- Protocol conformance of FakeStorageAdapter
- In-memory upload, download, multipart, checksum verification, and deletion
- Provider independence (zero cloud/vendor SDK imports)
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.app.infrastructure.storage import (
    FakeStorageAdapter,
    InvalidObjectKeyError,
    MultipartCompleteRequest,
    MultipartPartInfo,
    ObjectCategory,
    ObjectNotFoundError,
    ObjectReference,
    StorageIntegrityError,
    StoragePort,
    StorageUnavailableError,
    UploadInitiationRequest,
    build_object_key,
    normalize_extension,
    parse_object_key,
)


class TestStorageKeyBuilder:
    """Tests for deterministic key generation and path validation."""

    def test_build_object_key_standard_format(self) -> None:
        tenant_id = UUID("c7a10f82-84b2-4d7a-b50a-bdfd189196b0")
        venue_id = UUID("4a87265a-063a-4a6c-9c70-7613768b4ad3")
        artifact_id = UUID("8f3b23c1-0731-419b-a3d2-d17e3f2824b2")
        capture_time = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

        key = build_object_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            category=ObjectCategory.EVIDENCE,
            artifact_id=artifact_id,
            extension="jpg",
            capture_time=capture_time,
        )

        expected = (
            "tenants/c7a10f82-84b2-4d7a-b50a-bdfd189196b0/"
            "venues/4a87265a-063a-4a6c-9c70-7613768b4ad3/"
            "evidence/2026/08/10/8f3b23c1-0731-419b-a3d2-d17e3f2824b2.jpg"
        )
        assert key == expected

    def test_parse_object_key_roundtrip(self) -> None:
        tenant_id = uuid4()
        venue_id = uuid4()
        artifact_id = uuid4()
        capture_time = datetime(2026, 5, 15, 8, 30, 0, tzinfo=UTC)

        key = build_object_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            category=ObjectCategory.RECORDINGS,
            artifact_id=artifact_id,
            extension=".mp4",
            capture_time=capture_time,
        )

        parsed = parse_object_key(key)
        assert parsed.tenant_id == tenant_id
        assert parsed.venue_id == venue_id
        assert parsed.category == ObjectCategory.RECORDINGS
        assert parsed.artifact_id == artifact_id
        assert parsed.year == 2026
        assert parsed.month == 5
        assert parsed.day == 15
        assert parsed.extension == "mp4"

    def test_normalize_extension_strips_dots_and_lowercases(self) -> None:
        assert normalize_extension(".JPG") == "jpg"
        assert normalize_extension("..mp4") == "mp4"
        assert normalize_extension("json_gz") == "json_gz"

    def test_normalize_extension_rejects_invalid_chars(self) -> None:
        with pytest.raises(InvalidObjectKeyError):
            normalize_extension("mp4;rm -rf /")

        with pytest.raises(InvalidObjectKeyError):
            normalize_extension("")

    def test_parse_object_key_rejects_path_traversal(self) -> None:
        with pytest.raises(InvalidObjectKeyError, match="illegal path traversal"):
            parse_object_key("tenants/../../etc/passwd")

        with pytest.raises(InvalidObjectKeyError, match="illegal path traversal"):
            parse_object_key("/tenants/123/venues/456/evidence/2026/01/01/789.jpg")

        with pytest.raises(InvalidObjectKeyError, match="illegal path traversal"):
            parse_object_key("tenants//venues/evidence/2026/01/01/789.jpg")

    def test_build_object_key_rejects_invalid_category(self) -> None:
        with pytest.raises(InvalidObjectKeyError, match="Unknown category"):
            build_object_key(
                tenant_id=uuid4(),
                venue_id=uuid4(),
                category="invalid_category",
                artifact_id=uuid4(),
                extension="mp4",
            )


class TestStorageTypedModels:
    """Tests for typed requests and object reference models."""

    def test_upload_initiation_request_defaults(self) -> None:
        req = UploadInitiationRequest(
            object_key="tenants/1/venues/2/evidence/2026/08/10/3.jpg",
            content_type="image/jpeg",
        )
        assert req.expires_in_seconds == 900
        assert req.expected_size_bytes is None
        assert req.checksum_sha256 is None

    def test_object_reference_representation(self) -> None:
        ref = ObjectReference(
            object_key="tenants/1/venues/2/recordings/2026/08/10/3.mp4",
            uri="s3://hotelops-development/tenants/1/venues/2/recordings/2026/08/10/3.mp4",
            content_type="video/mp4",
            size_bytes=10485760,
            checksum_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        assert ref.size_bytes == 10485760
        assert ref.content_type == "video/mp4"


class TestFakeStorageAdapter:
    """Tests verifying the StoragePort protocol and in-memory fake implementation."""

    @pytest.mark.asyncio
    async def test_protocol_conformance(self) -> None:
        adapter = FakeStorageAdapter()
        assert isinstance(adapter, StoragePort)

    @pytest.mark.asyncio
    async def test_lifecycle_and_connectivity(self) -> None:
        adapter = FakeStorageAdapter()
        assert await adapter.check_connectivity() is False

        await adapter.initialize()
        assert await adapter.check_connectivity() is True

        await adapter.close()
        assert await adapter.check_connectivity() is False

    @pytest.mark.asyncio
    async def test_put_and_get_stream_with_checksum(self) -> None:
        adapter = FakeStorageAdapter()
        await adapter.initialize()

        test_payload = b"HotelOps AI video keyframe binary data"
        expected_sha256 = "69808d9ea5dc4fdb2d7d59e7cd5601073bf9355c597d8b94257e32cd3c8dce7f"
        key = "tenants/t1/venues/v1/evidence/2026/08/10/a1.jpg"

        async def stream_generator() -> AsyncIterator[bytes]:
            yield test_payload[:10]
            yield test_payload[10:]

        meta = await adapter.put_object_stream(
            key,
            stream_generator(),
            content_type="image/jpeg",
            size_bytes=len(test_payload),
            checksum_sha256=expected_sha256,
        )

        assert meta.object_key == key
        assert meta.size_bytes == len(test_payload)
        assert meta.checksum_sha256 == expected_sha256
        assert await adapter.object_exists(key) is True

        # Read back
        chunks: list[bytes] = []
        async for chunk in adapter.get_object_stream(key):
            chunks.append(chunk)
        assert b"".join(chunks) == test_payload

    @pytest.mark.asyncio
    async def test_put_stream_detects_checksum_mismatch(self) -> None:
        adapter = FakeStorageAdapter()
        await adapter.initialize()

        test_payload = b"Corrupted video byte payload"
        wrong_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
        key = "tenants/t1/venues/v1/recordings/2026/08/10/a2.mp4"

        async def stream_generator() -> AsyncIterator[bytes]:
            yield test_payload

        with pytest.raises(StorageIntegrityError, match="Checksum mismatch"):
            await adapter.put_object_stream(
                key,
                stream_generator(),
                content_type="video/mp4",
                size_bytes=len(test_payload),
                checksum_sha256=wrong_sha256,
            )

    @pytest.mark.asyncio
    async def test_presigned_upload_and_download_urls(self) -> None:
        adapter = FakeStorageAdapter()
        await adapter.initialize()

        key = "tenants/t1/venues/v1/evidence/2026/08/10/a3.jpg"

        # Generate upload URL
        req = UploadInitiationRequest(
            object_key=key, content_type="image/jpeg", expires_in_seconds=600
        )
        upload_res = await adapter.generate_presigned_upload_url(req)

        assert "upload=true" in upload_res.upload_url
        assert upload_res.object_key == key
        assert upload_res.expires_in_seconds == 600
        assert upload_res.expires_at > datetime.now(UTC)

        # Download URL before object exists raises ObjectNotFoundError
        with pytest.raises(ObjectNotFoundError):
            await adapter.generate_presigned_download_url(key)

        # Put object
        async def dummy_stream() -> AsyncIterator[bytes]:
            yield b"dummy"

        await adapter.put_object_stream(
            key, dummy_stream(), content_type="image/jpeg", size_bytes=5
        )

        # Download URL now succeeds
        download_res = await adapter.generate_presigned_download_url(
            key,
            expires_in_seconds=300,
            response_content_disposition="inline; filename=test.jpg",
        )
        assert "download=true" in download_res.download_url
        assert "disposition=inline" in download_res.download_url

    @pytest.mark.asyncio
    async def test_idempotent_deletion(self) -> None:
        adapter = FakeStorageAdapter()
        await adapter.initialize()

        key = "tenants/t1/venues/v1/evidence/2026/08/10/a4.jpg"

        # Deleting non-existent object returns True (idempotent)
        assert await adapter.delete_object(key) is True

        # Put and delete
        async def dummy_stream() -> AsyncIterator[bytes]:
            yield b"to_be_deleted"

        await adapter.put_object_stream(
            key, dummy_stream(), content_type="image/jpeg", size_bytes=13
        )
        assert await adapter.object_exists(key) is True

        assert await adapter.delete_object(key) is True
        assert await adapter.object_exists(key) is False

        # Repeated delete is idempotent
        assert await adapter.delete_object(key) is True

    @pytest.mark.asyncio
    async def test_multipart_lifecycle(self) -> None:
        adapter = FakeStorageAdapter()
        await adapter.initialize()

        key = "tenants/t1/venues/v1/recordings/2026/08/10/a5.mp4"

        # 1. Initiate
        init_res = await adapter.initiate_multipart_upload(key, "video/mp4")
        assert init_res.object_key == key
        assert init_res.upload_id is not None

        # 2. Simulate internal part uploads
        adapter._multipart_uploads[init_res.upload_id][1] = b"part1_bytes_"
        adapter._multipart_uploads[init_res.upload_id][2] = b"part2_bytes"

        # 3. Complete
        complete_req = MultipartCompleteRequest(
            upload_id=init_res.upload_id,
            object_key=key,
            parts=[
                MultipartPartInfo(part_number=1, etag="etag-1"),
                MultipartPartInfo(part_number=2, etag="etag-2"),
            ],
        )
        meta = await adapter.complete_multipart_upload(complete_req)
        assert meta.size_bytes == len(b"part1_bytes_part2_bytes")
        assert await adapter.object_exists(key) is True

    @pytest.mark.asyncio
    async def test_simulated_storage_outage(self) -> None:
        adapter = FakeStorageAdapter()
        await adapter.initialize()
        adapter.simulate_unavailable(True)

        with pytest.raises(StorageUnavailableError):
            await adapter.get_object_metadata("any_key")

        with pytest.raises(StorageUnavailableError):
            await adapter.delete_object("any_key")


class TestProviderIndependence:
    """Strict verification that the abstraction contains zero vendor SDK dependencies."""

    def test_no_vendor_sdks_imported_in_abstraction_modules(self) -> None:
        forbidden_modules = {"boto3", "botocore", "minio", "google.cloud.storage", "azure.storage"}

        abstraction_modules = [
            "backend.app.infrastructure.storage.types",
            "backend.app.infrastructure.storage.exceptions",
            "backend.app.infrastructure.storage.key_builder",
            "backend.app.infrastructure.storage.protocol",
            "backend.app.infrastructure.storage.fake",
        ]

        for mod_name in abstraction_modules:
            module = sys.modules.get(mod_name)
            if module is None:
                __import__(mod_name)
                module = sys.modules[mod_name]

            module_source = getattr(module, "__file__", "")
            with open(module_source, encoding="utf-8") as f:
                content = f.read()

            for forbidden in forbidden_modules:
                assert f"import {forbidden}" not in content, (
                    f"Violation in {mod_name}: direct import of '{forbidden}' found!"
                )
                assert f"from {forbidden}" not in content, (
                    f"Violation in {mod_name}: direct import from '{forbidden}' found!"
                )
