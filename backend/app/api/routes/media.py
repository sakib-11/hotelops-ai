"""FastAPI routes for the media lifecycle (Tasks 9.7-9.12).

Covers upload initiation, completion, multipart sessions, verification,
signed download access, deletion, and metadata. Authorization is always
enforced server-side from the ActorContext before any storage access.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.idempotency import IdempotencyService
from backend.app.application.services.media_errors import MediaConflictError
from backend.app.application.services.media_lifecycle import MediaLifecycleService
from backend.app.application.services.media_upload import MediaUploadService
from backend.app.dependencies import get_db_session, get_settings, get_storage
from backend.app.infrastructure.auth.deps import get_actor_context
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability.context import correlation_id
from backend.app.infrastructure.storage.protocol import StoragePort
from contracts.common import MediaId
from contracts.identity import ActorContext
from contracts.media.models import (
    MediaCompleteRequest,
    MediaCompleteResponse,
    MediaDeleteResponse,
    MediaDownloadResponse,
    MediaMetadataResponse,
    MediaMultipartInitiateResponse,
    MediaPartPresignRequest,
    MediaPartPresignResponse,
    MediaUploadInitiateRequest,
    MediaUploadInitiateResponse,
    MediaVerifyRequest,
    MediaVerifyResponse,
)

router = APIRouter(prefix="/media", tags=["Media"])


# ---------------------------------------------------------------------------
# Upload lifecycle
# ---------------------------------------------------------------------------
# Media service errors (MediaNotFoundError/MediaConflictError/
# MediaValidationError/MediaProtectedError) are mapped to HTTP status
# codes at the application level in backend/app/main.py (the same
# pattern used for AuthenticationError/AuthorizationError).
# ---------------------------------------------------------------------------


@router.post(
    "/uploads/initiate",
    response_model=MediaUploadInitiateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate a controlled media upload",
)
async def initiate_media_upload(
    request: MediaUploadInitiateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaUploadInitiateResponse:
    """Register media metadata (state: UPLOADING) and return upload coordinates.

    Honors an ``Idempotency-Key`` header via the project's transactional
    IdempotencyService: a replayed initiate with the same key returns the
    previously created media record instead of creating a duplicate.
    """
    service = MediaUploadService(settings=settings, storage=storage)

    if not idempotency_key:
        return await service.initiate_upload(
            session=session,
            actor=actor,
            request=request,
            correlation_id=correlation_id(),
        )

    async def _initiate_handler(
        sess: AsyncSession, req: MediaUploadInitiateRequest
    ) -> dict[str, Any]:
        response = await service.initiate_upload(
            session=sess,
            actor=actor,
            request=req,
            correlation_id=correlation_id(),
        )
        return response.model_dump(mode="json")

    result = await IdempotencyService(settings).execute(
        session=session,
        actor=actor,
        operation="media.upload.initiate",
        key=idempotency_key,
        request=request,
        handler=_initiate_handler,
        venue_id=request.venue_id,
    )
    if result.result is None:
        raise MediaConflictError("Idempotency replay produced no stored result")
    return MediaUploadInitiateResponse.model_validate(result.result)


@router.post(
    "/uploads/{media_id}/complete",
    response_model=MediaCompleteResponse,
    summary="Complete a media upload (single PUT or multipart manifest)",
)
async def complete_media_upload(
    media_id: MediaId,
    request: MediaCompleteRequest,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaCompleteResponse:
    """Verify the object in storage and transition the record to UPLOADED."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.complete_upload(
        session=session,
        actor=actor,
        media_id=media_id,
        request=request,
        correlation_id=correlation_id(),
    )


@router.post(
    "/uploads/{media_id}/multipart",
    response_model=MediaMultipartInitiateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a multipart upload session (large recordings)",
)
async def initiate_media_multipart(
    media_id: MediaId,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaMultipartInitiateResponse:
    """Start a provider multipart session and return its upload_id."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.initiate_multipart(
        session=session,
        actor=actor,
        media_id=media_id,
        correlation_id=correlation_id(),
    )


@router.post(
    "/uploads/{media_id}/multipart/presign",
    response_model=MediaPartPresignResponse,
    summary="Presign PUT URLs for multipart parts",
)
async def presign_media_parts(
    media_id: MediaId,
    request: MediaPartPresignRequest,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaPartPresignResponse:
    """Return short-lived presigned PUT URLs for the requested parts."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.presign_parts(
        session=session,
        actor=actor,
        media_id=media_id,
        request=request,
        correlation_id=correlation_id(),
    )


@router.post(
    "/uploads/{media_id}/abort",
    response_model=MediaDeleteResponse,
    summary="Abort an in-flight upload",
)
async def abort_media_upload(
    media_id: MediaId,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaDeleteResponse:
    """Abort the upload, clean up storage best-effort, mark the record FAILED."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.abort_upload(
        session=session,
        actor=actor,
        media_id=media_id,
        correlation_id=correlation_id(),
    )


@router.post(
    "/uploads/{media_id}/verify",
    response_model=MediaVerifyResponse,
    summary="Verify content + checksum and promote to AVAILABLE",
)
async def verify_media(
    media_id: MediaId,
    request: MediaVerifyRequest,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaVerifyResponse:
    """Validate magic bytes + SHA-256; AVAILABLE only on success."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.verify_media(
        session=session,
        actor=actor,
        media_id=media_id,
        declared_checksum_sha256=request.checksum_sha256,
        correlation_id=correlation_id(),
    )


# ---------------------------------------------------------------------------
# Access / metadata / deletion
# ---------------------------------------------------------------------------


@router.get(
    "/{media_id}/download",
    response_model=MediaDownloadResponse,
    summary="Issue a short-lived signed download URL",
)
async def download_media(
    media_id: MediaId,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaDownloadResponse:
    """Authorize the actor, then sign a temporary GET URL for the object."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.request_download(
        session=session,
        actor=actor,
        media_id=media_id,
        correlation_id=correlation_id(),
    )


@router.get(
    "/{media_id}",
    response_model=MediaMetadataResponse,
    summary="Get authoritative media metadata",
)
async def get_media_metadata(
    media_id: MediaId,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaMetadataResponse:
    """Return the metadata record for authorized media."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.get_metadata(session=session, actor=actor, media_id=media_id)


@router.delete(
    "/{media_id}",
    response_model=MediaDeleteResponse,
    summary="Delete media idempotently (preservation-protected)",
)
async def delete_media(
    media_id: MediaId,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    storage: StoragePort = Depends(get_storage),
) -> MediaDeleteResponse:
    """Two-phase idempotent deletion of the object and metadata record."""
    service = MediaLifecycleService(settings=settings, storage=storage)
    return await service.request_deletion(
        session=session,
        actor=actor,
        media_id=media_id,
        correlation_id=correlation_id(),
    )
