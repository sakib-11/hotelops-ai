"""Unit and authorization tests for Media Upload Initialization (Task 9.7).

Validates:
- MediaUploadService workflow and state transitions
- Server-controlled MediaId and deterministic object-key generation
- Tenant and venue authorization scope enforcement
- StoragePort failure handling and state rollback to FAILED
- Handling of all media categories (recordings, evidence, reports, analytics, temporary)
- Provenance attachment (camera_id, session_id, event_id, event_time)
- Invariant: Media is NEVER marked AVAILABLE at initialization
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.application.services.media_upload import MediaUploadService
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.models.identity import VenueModel
from backend.app.infrastructure.database.models.media import MediaAssetModel
from backend.app.infrastructure.storage.exceptions import (
    StorageError,
    StorageOperationError,
)
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from contracts.common import EventId, TenantId, UserId, VenueId, VideoSessionId
from contracts.identity import ActorContext, Permission, RoleName
from contracts.media.models import (
    MediaCategory,
    MediaLifecycleState,
    MediaProvenance,
    MediaUploadInitiateRequest,
)


@pytest.fixture
def settings() -> Settings:
    # Settings uses env-var ALIASES (case-insensitive) and ignores
    # unknown extras — pass alias kwargs, not field names.
    return Settings(
        _env_file=None,
        OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
        OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
        OBJECT_STORAGE_ACCESS_KEY="test-key",
        OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
        SECRET_KEY="test-secret-that-is-at-least-32-bytes-long",
    )


@pytest.fixture
def fake_storage() -> FakeStorageAdapter:
    return FakeStorageAdapter()


@pytest.fixture
def tenant_id() -> TenantId:
    return TenantId(uuid.UUID("c7a10f82-84b2-4d7a-b50a-bdfd189196b0"))


@pytest.fixture
def venue_id() -> VenueId:
    return VenueId(uuid.UUID("4a87265a-063a-4a6c-9c70-7613768b4ad3"))


@pytest.fixture
def foreign_venue_id() -> VenueId:
    return VenueId(uuid.UUID("99999999-0000-0000-0000-000000000000"))


@pytest.fixture
def actor(tenant_id: TenantId, venue_id: VenueId) -> ActorContext:
    return ActorContext(
        actor_id=UserId(uuid.uuid4()),
        tenant_id=tenant_id,
        role_name=RoleName.ADMIN,
        permissions=frozenset({
            Permission.VIDEO_READ,
            Permission.VENUE_READ,
            Permission.EVIDENCE_READ,
        }),
        venue_scope=frozenset({venue_id}),
        authenticated_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def mock_venue_repo(venue_id: VenueId, tenant_id: TenantId) -> AsyncMock:
    repo = AsyncMock()
    venue = VenueModel(
        venue_id=uuid.UUID(str(venue_id)),
        tenant_id=uuid.UUID(str(tenant_id)),
        name="Grand Test Hotel",
        status="active",
    )

    # Matches the real VenueRepository API: get_for_actor(actor, venue_id)
    # (session is bound at construction time).
    async def _get_venue(a: Any, v: Any) -> VenueModel | None:
        return venue if str(v) == str(venue_id) else None

    repo.get_for_actor = AsyncMock(side_effect=_get_venue)
    return repo


@pytest.fixture
def mock_media_repo() -> AsyncMock:
    repo = AsyncMock()

    async def _create(s: Any, a: Any, m: Any) -> Any:
        return m

    repo.create_for_actor = AsyncMock(side_effect=_create)
    return repo


class TestMediaUploadService:
    """Tests for MediaUploadService workflow."""

    async def test_valid_recording_upload_initialization(
        self,
        settings: Settings,
        fake_storage: FakeStorageAdapter,
        actor: ActorContext,
        venue_id: VenueId,
        mock_session: AsyncMock,
        mock_venue_repo: AsyncMock,
        mock_media_repo: AsyncMock,
    ) -> None:
        service = MediaUploadService(
            settings=settings,
            storage=fake_storage,
            media_repo=mock_media_repo,
            venue_repo=mock_venue_repo,
        )

        request = MediaUploadInitiateRequest(
            venue_id=venue_id,
            category=MediaCategory.RECORDINGS,
            content_type="video/mp4",
            expected_size_bytes=52428800,
            original_filename="front_desk_cam1.mp4",
            retention_class="cctv_30_days",
        )

        response = await service.initiate_upload(
            session=mock_session,
            actor=actor,
            request=request,
        )

        assert response.lifecycle_state == MediaLifecycleState.UPLOADING
        assert response.lifecycle_state != MediaLifecycleState.AVAILABLE
        assert response.object_key.startswith(
            f"tenants/{actor.tenant_id}/venues/{venue_id}/recordings/"
        )
        assert response.object_key.endswith(".mp4")
        assert response.storage_uri == f"s3://hotelops-test-bucket/{response.object_key}"
        assert response.upload_url.startswith("https://fake-storage.local/")
        assert response.expires_in_seconds == 900

        # Verify database model persisted
        mock_media_repo.create_for_actor.assert_called_once()
        persisted_media: MediaAssetModel = mock_media_repo.create_for_actor.call_args[0][2]
        assert persisted_media.tenant_id == actor.tenant_id
        assert persisted_media.venue_id == venue_id
        assert persisted_media.category == "recordings"
        assert persisted_media.lifecycle_state == "uploading"
        assert persisted_media.size_bytes == 52428800

    async def test_provenance_linkage(
        self,
        settings: Settings,
        fake_storage: FakeStorageAdapter,
        actor: ActorContext,
        venue_id: VenueId,
        mock_session: AsyncMock,
        mock_venue_repo: AsyncMock,
        mock_media_repo: AsyncMock,
    ) -> None:
        service = MediaUploadService(
            settings=settings,
            storage=fake_storage,
            media_repo=mock_media_repo,
            venue_repo=mock_venue_repo,
        )

        cam_id = uuid.uuid4()
        sess_id = VideoSessionId(uuid.uuid4())
        ev_id = EventId(uuid.uuid4())
        ev_time = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

        request = MediaUploadInitiateRequest(
            venue_id=venue_id,
            category=MediaCategory.EVIDENCE,
            content_type="image/jpeg",
            expected_size_bytes=1048576,
            provenance=MediaProvenance(
                camera_id=cam_id,
                session_id=sess_id,
                event_id=ev_id,
                event_time=ev_time,
            ),
        )

        response = await service.initiate_upload(
            session=mock_session,
            actor=actor,
            request=request,
        )

        assert response.lifecycle_state == MediaLifecycleState.UPLOADING
        persisted_media: MediaAssetModel = mock_media_repo.create_for_actor.call_args[0][2]
        assert persisted_media.camera_id == cam_id
        assert persisted_media.session_id == sess_id
        assert persisted_media.event_id == ev_id
        assert persisted_media.event_time == ev_time

    @pytest.mark.parametrize(
        ("category", "content_type", "filename", "expected_ext"),
        [
            (MediaCategory.RECORDINGS, "video/mp4", None, "mp4"),
            (MediaCategory.EVIDENCE, "image/jpeg", "snapshot.jpg", "jpg"),
            (MediaCategory.REPORTS, "application/pdf", "monthly_report.pdf", "pdf"),
            (MediaCategory.ANALYTICS, "application/json", "footfall.json.gz", "json.gz"),
            (MediaCategory.TEMPORARY, "application/octet-stream", None, "bin"),
        ],
    )
    async def test_all_categories_and_extensions(
        self,
        category: MediaCategory,
        content_type: str,
        filename: str | None,
        expected_ext: str,
        settings: Settings,
        fake_storage: FakeStorageAdapter,
        actor: ActorContext,
        venue_id: VenueId,
        mock_session: AsyncMock,
        mock_venue_repo: AsyncMock,
        mock_media_repo: AsyncMock,
    ) -> None:
        service = MediaUploadService(
            settings=settings,
            storage=fake_storage,
            media_repo=mock_media_repo,
            venue_repo=mock_venue_repo,
        )

        request = MediaUploadInitiateRequest(
            venue_id=venue_id,
            category=category,
            content_type=content_type,
            expected_size_bytes=1024,
            original_filename=filename,
        )

        response = await service.initiate_upload(
            session=mock_session,
            actor=actor,
            request=request,
        )

        assert response.object_key.endswith(f".{expected_ext}")
        assert f"/{category.value}/" in response.object_key


class TestMediaUploadAuthorization:
    """Tests verifying authorization scope enforcement."""

    async def test_unauthorized_venue_rejected(
        self,
        settings: Settings,
        fake_storage: FakeStorageAdapter,
        actor: ActorContext,
        foreign_venue_id: VenueId,
        mock_session: AsyncMock,
        mock_venue_repo: AsyncMock,
        mock_media_repo: AsyncMock,
    ) -> None:
        service = MediaUploadService(
            settings=settings,
            storage=fake_storage,
            media_repo=mock_media_repo,
            venue_repo=mock_venue_repo,
        )

        request = MediaUploadInitiateRequest(
            venue_id=foreign_venue_id,
            category=MediaCategory.RECORDINGS,
            content_type="video/mp4",
            expected_size_bytes=1024,
        )

        with pytest.raises(AuthorizationError):
            await service.initiate_upload(
                session=mock_session,
                actor=actor,
                request=request,
            )

        mock_media_repo.create_for_actor.assert_not_called()


class TestMediaUploadFailureHandling:
    """Tests verifying recovery and state update when storage initialization fails."""

    async def test_storage_failure_marks_model_failed(
        self,
        settings: Settings,
        actor: ActorContext,
        venue_id: VenueId,
        mock_session: AsyncMock,
        mock_venue_repo: AsyncMock,
        mock_media_repo: AsyncMock,
    ) -> None:
        failing_storage = AsyncMock()
        failing_storage.generate_presigned_upload_url = AsyncMock(
            side_effect=StorageError("Simulated storage network failure")
        )

        service = MediaUploadService(
            settings=settings,
            storage=failing_storage,
            media_repo=mock_media_repo,
            venue_repo=mock_venue_repo,
        )

        request = MediaUploadInitiateRequest(
            venue_id=venue_id,
            category=MediaCategory.RECORDINGS,
            content_type="video/mp4",
            expected_size_bytes=1024,
        )

        with pytest.raises(StorageOperationError):
            await service.initiate_upload(
                session=mock_session,
                actor=actor,
                request=request,
            )

        persisted_media: MediaAssetModel = mock_media_repo.create_for_actor.call_args[0][2]
        assert persisted_media.lifecycle_state == "failed"
        mock_session.flush.assert_called()
