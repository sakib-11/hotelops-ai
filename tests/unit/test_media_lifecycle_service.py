"""Unit tests for the media lifecycle service (Tasks 9.8-9.12).

Covers completion (single + multipart), verification (checksum +
content), signed access, and two-phase deletion with preservation
protection — including idempotency, authorization, and failure paths.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from backend.app.application.services.idempotency import IdempotencyResult
from backend.app.application.services.media_errors import (
    MediaConflictError,
    MediaNotFoundError,
    MediaProtectedError,
    MediaValidationError,
)
from backend.app.application.services.media_lifecycle import MediaLifecycleService
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from contracts.common import UserId
from contracts.identity import ActorContext, Permission, RoleName
from contracts.media.models import (
    MediaCategory,
    MediaCompleteRequest,
    MediaLifecycleState,
    MediaPartInfo,
    MediaPartPresignRequest,
    MediaUploadInitiateRequest,
)
from tests.unit.fakes import FakeMediaRepository, make_media

MP4_PAYLOAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 96


def _stream(payload: bytes) -> AsyncIterator[bytes]:
    async def _gen() -> AsyncIterator[bytes]:
        yield payload

    return _gen()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
        OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
        OBJECT_STORAGE_ACCESS_KEY="test-key",
        OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
        SECRET_KEY="test-secret-that-is-at-least-32-bytes-long",
    )


@pytest.fixture
def storage() -> FakeStorageAdapter:
    adapter = FakeStorageAdapter()
    return adapter


@pytest.fixture
def session() -> AsyncMock:
    s = AsyncMock()
    # session.add() is synchronous on SQLAlchemy's AsyncSession — prevent
    # AsyncMock from wrapping it as a coroutine.
    from unittest.mock import MagicMock

    s.add = MagicMock()
    return s


@pytest.fixture
def repo() -> FakeMediaRepository:
    return FakeMediaRepository()


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("c7a10f82-84b2-4d7a-b50a-bdfd189196b0")


@pytest.fixture
def venue_id() -> uuid.UUID:
    return uuid.UUID("4a87265a-063a-4a6c-9c70-7613768b4ad3")


@pytest.fixture
def actor(tenant_id: uuid.UUID, venue_id: uuid.UUID) -> ActorContext:
    return ActorContext(
        actor_id=UserId(uuid.uuid4()),
        tenant_id=tenant_id,
        role_name=RoleName.ADMIN,
        permissions=frozenset({
            Permission.VIDEO_READ,
            Permission.EVIDENCE_READ,
            Permission.ANALYTICS_READ,
        }),
        venue_scope=frozenset({venue_id}),
        authenticated_at=datetime.now(UTC),
    )


def _service(
    settings: Settings, storage: FakeStorageAdapter, repo: FakeMediaRepository
) -> MediaLifecycleService:
    return MediaLifecycleService(settings=settings, storage=storage, media_repo=repo)


def _uploaded_media(
    repo: FakeMediaRepository,
    storage: FakeStorageAdapter,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    category: MediaCategory = MediaCategory.RECORDINGS,
    payload: bytes = MP4_PAYLOAD,
    lifecycle_state: str = "uploading",
    retention_class: str | None = None,
    checksum_sha256: str | None = None,
    **kwargs,
):
    """Seed a media record AND its object bytes in the fake storage."""
    size_bytes = kwargs.pop("size_bytes", len(payload))
    media = make_media(
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=category.value,
        size_bytes=size_bytes,
        lifecycle_state=lifecycle_state,
        retention_class=retention_class,
        checksum_sha256=checksum_sha256,
        **kwargs,
    )
    repo.seed(media)
    return media


class TestCompleteUpload:
    """Task 9.8 — completion (single presigned PUT and multipart)."""

    async def test_single_put_completion(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            retention_class="cctv_30_days",
        )
        await storage.put_object_stream(
            media.object_key,
            _stream(MP4_PAYLOAD),
            content_type="video/mp4",
            size_bytes=len(MP4_PAYLOAD),
        )

        service = _service(settings, storage, repo)
        resp = await service.complete_upload(
            session,
            actor,
            media.media_id,
            MediaCompleteRequest(expected_size_bytes=len(MP4_PAYLOAD)),
        )

        assert resp.lifecycle_state == MediaLifecycleState.UPLOADED
        assert resp.size_bytes == len(MP4_PAYLOAD)
        assert resp.checksum_sha256 == _sha(MP4_PAYLOAD)
        assert resp.expires_at is not None  # cctv_30_days policy applied
        assert media.lifecycle_state == "uploaded"

    async def test_completion_is_idempotent(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="uploaded",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        resp = await service.complete_upload(session, actor, media.media_id, MediaCompleteRequest())
        # Replay does not touch storage or create records — same state.
        assert resp.lifecycle_state == MediaLifecycleState.UPLOADED
        assert media.lifecycle_state == "uploaded"

    async def test_terminal_media_cannot_be_completed(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="failed",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaConflictError):
            await service.complete_upload(session, actor, media.media_id, MediaCompleteRequest())

    async def test_size_mismatch_fails_media(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            size_bytes=len(MP4_PAYLOAD),
        )
        # Client declared an expected size that does not match reality.
        await storage.put_object_stream(
            media.object_key,
            _stream(MP4_PAYLOAD),
            content_type="video/mp4",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaValidationError, match="Size mismatch"):
            await service.complete_upload(
                session,
                actor,
                media.media_id,
                MediaCompleteRequest(expected_size_bytes=999999),
            )
        assert media.lifecycle_state == "failed"

    async def test_missing_object_fails_media(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        # No object was ever uploaded to storage.
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        service = _service(settings, storage, repo)
        with pytest.raises(MediaValidationError, match="not found"):
            await service.complete_upload(session, actor, media.media_id, MediaCompleteRequest())
        assert media.lifecycle_state == "failed"

    async def test_multipart_completion(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        service = _service(settings, storage, repo)

        init = await service.initiate_multipart(session, actor, media.media_id)
        assert init.upload_id

        presign = await service.presign_parts(
            session, actor, media.media_id, MediaPartPresignRequest(part_numbers=[1, 2])
        )
        assert [p.part_number for p in presign.parts] == [1, 2]

        part1 = MP4_PAYLOAD[:64]
        part2 = MP4_PAYLOAD[64:]
        storage._multipart_uploads[init.upload_id][1] = part1
        storage._multipart_uploads[init.upload_id][2] = part2

        resp = await service.complete_upload(
            session,
            actor,
            media.media_id,
            MediaCompleteRequest(
                upload_id=init.upload_id,
                parts=[
                    MediaPartInfo(part_number=1, etag="e1"),
                    MediaPartInfo(part_number=2, etag="e2"),
                ],
            ),
        )
        assert resp.lifecycle_state == MediaLifecycleState.UPLOADED
        assert resp.size_bytes == len(MP4_PAYLOAD)

    async def test_multipart_missing_part_fails(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        service = _service(settings, storage, repo)
        init = await service.initiate_multipart(session, actor, media.media_id)
        storage._multipart_uploads[init.upload_id][1] = MP4_PAYLOAD  # part 2 never uploaded

        with pytest.raises(MediaValidationError, match="missing parts"):
            await service.complete_upload(
                session,
                actor,
                media.media_id,
                MediaCompleteRequest(
                    upload_id=init.upload_id,
                    parts=[
                        MediaPartInfo(part_number=1, etag="e1"),
                        MediaPartInfo(part_number=2, etag="e2"),
                    ],
                ),
            )
        assert media.lifecycle_state == "failed"

    async def test_cross_tenant_completion_denied(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        other_tenant = uuid.uuid4()
        media = make_media(tenant_id=other_tenant, venue_id=venue_id, size_bytes=len(MP4_PAYLOAD))
        repo.seed(media)
        service = _service(settings, storage, repo)
        with pytest.raises(MediaNotFoundError):
            await service.complete_upload(session, actor, media.media_id, MediaCompleteRequest())


class TestVerifyMedia:
    """Tasks 9.9 + 9.10 — checksum & content verification."""

    async def _to_uploaded(
        self,
        service: MediaLifecycleService,
        session: AsyncMock,
        actor: ActorContext,
        media,
    ) -> None:
        await service.complete_upload(session, actor, media.media_id, MediaCompleteRequest())

    async def test_happy_path_to_available(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        await storage.put_object_stream(
            media.object_key,
            _stream(MP4_PAYLOAD),
            content_type="video/mp4",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        await self._to_uploaded(service, session, actor, media)

        resp = await service.verify_media(session, actor, media.media_id)
        assert resp.lifecycle_state == MediaLifecycleState.AVAILABLE
        assert resp.checksum_sha256 == _sha(MP4_PAYLOAD)
        assert media.lifecycle_state == "available"

    async def test_checksum_mismatch_fails(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        await storage.put_object_stream(
            media.object_key,
            _stream(MP4_PAYLOAD),
            content_type="video/mp4",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        await self._to_uploaded(service, session, actor, media)

        wrong = "0" * 64
        with pytest.raises(MediaValidationError, match="Checksum mismatch"):
            await service.verify_media(
                session, actor, media.media_id, declared_checksum_sha256=wrong
            )
        assert media.lifecycle_state == "failed"

    async def test_invalid_content_fails(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        garbage = b"\x89PNG\r\n\x1a\n" + b"not actually a video"  # PNG magic in a recordings upload
        media = _uploaded_media(
            repo, storage, tenant_id=tenant_id, venue_id=venue_id, size_bytes=len(garbage)
        )
        await storage.put_object_stream(
            media.object_key,
            _stream(garbage),
            content_type="video/mp4",
            size_bytes=len(garbage),
        )
        service = _service(settings, storage, repo)
        await self._to_uploaded(service, session, actor, media)

        with pytest.raises(MediaValidationError, match="not allowed"):
            await service.verify_media(session, actor, media.media_id)
        assert media.lifecycle_state == "failed"

    async def test_missing_object_at_verify_fails(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="uploaded",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaValidationError, match="Verification failed"):
            await service.verify_media(session, actor, media.media_id)
        assert media.lifecycle_state == "failed"

    async def test_already_available_is_idempotent(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        resp = await service.verify_media(session, actor, media.media_id)
        assert resp.lifecycle_state == MediaLifecycleState.AVAILABLE


class TestRequestDownload:
    """Task 9.11 — controlled signed access."""

    async def test_download_url_for_available(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        await storage.put_object_stream(
            media.object_key,
            _stream(MP4_PAYLOAD),
            content_type="video/mp4",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        resp = await service.request_download(session, actor, media.media_id)
        assert "download=true" in resp.download_url
        assert resp.expires_at > datetime.now(UTC)
        assert resp.content_type == "video/mp4"

    async def test_download_denied_for_unavailable_state(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        service = _service(settings, storage, repo)
        with pytest.raises(MediaConflictError):
            await service.request_download(session, actor, media.media_id)

    async def test_download_denied_for_deleted(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="deleted",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaConflictError):
            await service.request_download(session, actor, media.media_id)

    async def test_download_requires_permission(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        # Actor WITHOUT the evidence.read permission.
        restricted = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=tenant_id,
            role_name=RoleName.OPERATOR,
            permissions=frozenset({Permission.VIDEO_READ}),
            venue_scope=frozenset({venue_id}),
            authenticated_at=datetime.now(UTC),
        )
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            category=MediaCategory.EVIDENCE,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(AuthorizationError):
            await service.request_download(session, restricted, media.media_id)

    async def test_cross_tenant_download_denied(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        other_tenant = uuid.uuid4()
        media = make_media(
            tenant_id=other_tenant,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        repo.seed(media)
        service = _service(settings, storage, repo)
        with pytest.raises(MediaNotFoundError):
            await service.request_download(session, actor, media.media_id)


class TestRequestDeletion:
    """Task 9.12 — two-phase idempotent deletion with preservation."""

    async def test_delete_available_media(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        await storage.put_object_stream(
            media.object_key,
            _stream(MP4_PAYLOAD),
            content_type="video/mp4",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)

        resp = await service.request_deletion(session, actor, media.media_id)
        assert resp.lifecycle_state == MediaLifecycleState.DELETED
        assert await storage.object_exists(media.object_key) is False

        # Idempotent repeat.
        again = await service.request_deletion(session, actor, media.media_id)
        assert again.lifecycle_state == MediaLifecycleState.DELETED

    async def test_legal_hold_protected(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            retention_class="legal_hold",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaProtectedError):
            await service.request_deletion(session, actor, media.media_id)
        assert media.lifecycle_state == "available"

    async def test_preservation_hold_metadata_protects(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            metadata_={"preservation_hold": "true"},
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaProtectedError):
            await service.request_deletion(session, actor, media.media_id)

    async def test_delete_transient_storage_failure_keeps_pending(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        failing_storage = FakeStorageAdapter()
        failing_storage.simulate_unavailable(True)
        service = _service(settings, failing_storage, repo)
        with pytest.raises(MediaValidationError):
            await service.request_deletion(session, actor, media.media_id)
        # Record stays DELETION_PENDING — the cleanup worker retries.
        assert media.lifecycle_state == "deletion_pending"


class TestAbortUpload:
    """Task 9.8 — abort of an in-flight upload."""

    async def test_abort_uploading_to_failed(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(repo, storage, tenant_id=tenant_id, venue_id=venue_id)
        service = _service(settings, storage, repo)
        resp = await service.abort_upload(session, actor, media.media_id)
        assert resp.lifecycle_state == MediaLifecycleState.FAILED
        assert media.lifecycle_state == "failed"

        # Idempotent repeat.
        again = await service.abort_upload(session, actor, media.media_id)
        assert again.lifecycle_state == MediaLifecycleState.FAILED

    async def test_abort_rejects_available_media(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        with pytest.raises(MediaConflictError):
            await service.abort_upload(session, actor, media.media_id)


class TestInitiateIdempotency:
    """Task 9.7 — the initiate route honors Idempotency-Key (wiring test).

    Uses a stubbed IdempotencyService so the test stays deterministic
    (the service itself is covered by tests/unit/test_idempotency_service.py
    and the DB-backed integration suite).
    """

    async def test_route_forwards_key_and_maps_result(
        self,
        monkeypatch,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        actor: ActorContext,
        venue_id: uuid.UUID,
    ) -> None:
        from backend.app.api.routes.media import initiate_media_upload

        captured: dict = {}

        class _StubIdempotency:
            def __init__(self, s: Settings) -> None:
                pass

            async def execute(self, **kwargs):
                captured.update(kwargs)
                return IdempotencyResult(
                    idempotency_id=uuid.uuid4(),
                    replayed=True,
                    result={
                        "media_id": str(uuid.uuid4()),
                        "object_key": "tenants/t/venues/v/recordings/2026/08/10/a.mp4",
                        "storage_uri": "s3://b/key",
                        "upload_url": "https://stub/upload",
                        "required_headers": {"Content-Type": "video/mp4"},
                        "expires_in_seconds": 900,
                        "lifecycle_state": "uploading",
                        "schema_version": "1.0",
                    },
                )

        monkeypatch.setattr("backend.app.api.routes.media.IdempotencyService", _StubIdempotency)

        request = MediaUploadInitiateRequest(
            venue_id=venue_id,
            category=MediaCategory.RECORDINGS,
            content_type="video/mp4",
            expected_size_bytes=1024,
        )
        response = await initiate_media_upload(
            request=request,
            idempotency_key="key-123",
            actor=actor,
            session=session,
            settings=settings,
            storage=storage,
        )

        assert captured["key"] == "key-123"
        assert captured["operation"] == "media.upload.initiate"
        assert captured["request"] is request
        assert response.lifecycle_state == MediaLifecycleState.UPLOADING
        assert response.upload_url == "https://stub/upload"


class TestGetMetadata:
    """Media metadata readback."""

    async def test_metadata_readback(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = _uploaded_media(
            repo,
            storage,
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
        )
        service = _service(settings, storage, repo)
        meta = await service.get_metadata(session, actor, media.media_id)
        assert meta.media_id == media.media_id
        assert meta.lifecycle_state == MediaLifecycleState.AVAILABLE
        assert meta.category == MediaCategory.RECORDINGS
        assert meta.object_key == media.object_key

    async def test_cross_tenant_metadata_denied(
        self,
        settings: Settings,
        storage: FakeStorageAdapter,
        session: AsyncMock,
        repo: FakeMediaRepository,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        other_tenant = uuid.uuid4()
        media = make_media(tenant_id=other_tenant, venue_id=venue_id)
        repo.seed(media)
        service = _service(settings, storage, repo)
        with pytest.raises(MediaNotFoundError):
            await service.get_metadata(session, actor, media.media_id)
