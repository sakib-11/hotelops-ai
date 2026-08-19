"""Media upload initialization service (Task 9.7).

Coordinates PostgreSQL media metadata persistence and Object Storage upload
initialization. Enforces tenant and venue boundary authorization, deterministic
object-key generation, and failure recovery.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.media_audit import (
    EVENT_UPLOAD_INITIATED,
    enqueue_media_audit_event,
)
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.auth.scope import require_tenant_venue_access
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.models.media import MediaAssetModel
from backend.app.infrastructure.database.repositories.identity import VenueRepository
from backend.app.infrastructure.database.repositories.media import MediaRepository
from backend.app.infrastructure.storage.exceptions import (
    StorageError,
    StorageOperationError,
)
from backend.app.infrastructure.storage.key_builder import (
    build_object_key,
    normalize_extension,
)
from backend.app.infrastructure.storage.protocol import StoragePort
from backend.app.infrastructure.storage.types import (
    ObjectCategory,
    UploadInitiationRequest,
)
from contracts.common import MediaId
from contracts.identity import ActorContext
from contracts.media.models import (
    MediaCategory,
    MediaLifecycleState,
    MediaUploadInitiateRequest,
    MediaUploadInitiateResponse,
)

logger = logging.getLogger(__name__)


_COMPOUND_EXTENSIONS = (".json.gz",)


def _infer_extension(
    category: MediaCategory,
    content_type: str,
    original_filename: str | None,
) -> str:
    """Infer and normalize a safe file extension from metadata."""
    if original_filename and "." in original_filename:
        # Preserve compound extensions (e.g. "footfall.json.gz" -> "json.gz")
        # before falling back to the last single segment.
        lower_name = original_filename.lower()
        for compound in _COMPOUND_EXTENSIONS:
            if lower_name.endswith(compound):
                try:
                    return normalize_extension(compound.lstrip("."))
                except Exception:
                    break
        raw_ext = original_filename.rsplit(".", 1)[-1]
        try:
            return normalize_extension(raw_ext)
        except Exception:
            pass

    ctype = content_type.lower().strip()
    if "video" in ctype or "mp4" in ctype:
        return "mp4"
    if "jpeg" in ctype or "jpg" in ctype:
        return "jpg"
    if "png" in ctype:
        return "png"
    if "pdf" in ctype:
        return "pdf"
    if "json" in ctype:
        return "json.gz" if "gzip" in ctype else "json"

    # Default category extensions
    match category:
        case MediaCategory.RECORDINGS:
            return "mp4"
        case MediaCategory.EVIDENCE:
            return "jpg"
        case MediaCategory.REPORTS:
            return "pdf"
        case MediaCategory.ANALYTICS:
            return "json.gz"
        case MediaCategory.TEMPORARY:
            return "bin"


class MediaUploadService:
    """Application service for initiating media uploads."""

    def __init__(
        self,
        settings: Settings,
        storage: StoragePort,
        media_repo: MediaRepository | None = None,
        venue_repo: VenueRepository | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._media_repo = media_repo or MediaRepository()
        # VenueRepository requires the session at construction time (its
        # get_for_actor takes actor + venue_id only); it is created per
        # request with the active session in initiate_upload.
        self._venue_repo = venue_repo

    async def initiate_upload(
        self,
        session: AsyncSession,
        actor: ActorContext,
        request: MediaUploadInitiateRequest,
        correlation_id: str | None = None,
    ) -> MediaUploadInitiateResponse:
        """Initiate a controlled media upload workflow.

        1. Validates tenant and venue authorization against ActorContext.
        2. Generates server-assigned MediaId and deterministic ObjectKey.
        3. Persists media metadata record in PostgreSQL (state: UPLOADING).
        4. Initializes object storage upload via StoragePort.
        5. Returns presigned upload parameters to client.

        Raises:
            AuthorizationError: If actor lacks access to target tenant or venue.
            StorageOperationError: If storage provider fails to initiate upload.
        """
        # Step 1: Authorization checks
        require_tenant_venue_access(
            actor=actor,
            resource_tenant_id=actor.tenant_id,
            venue_id=request.venue_id,
        )

        venue_repo = self._venue_repo or VenueRepository(session)
        venue = await venue_repo.get_for_actor(actor, request.venue_id)
        if venue is None:
            msg = f"Venue {request.venue_id} does not exist or access is forbidden"
            raise AuthorizationError(msg)

        # Step 2: Key & Identity generation
        media_id = MediaId(uuid.uuid4())
        ext = _infer_extension(request.category, request.content_type, request.original_filename)
        storage_category = ObjectCategory(request.category.value)

        object_key = build_object_key(
            tenant_id=actor.tenant_id,
            venue_id=request.venue_id,
            category=storage_category,
            artifact_id=media_id,
            extension=ext,
        )

        bucket = self._settings.object_storage_bucket
        storage_uri = f"s3://{bucket}/{object_key}"

        # Step 3: Database metadata record creation
        media = MediaAssetModel(
            media_id=media_id,
            tenant_id=actor.tenant_id,
            venue_id=request.venue_id,
            category=request.category.value,
            object_key=object_key,
            storage_uri=storage_uri,
            storage_bucket=bucket,
            content_type=request.content_type,
            size_bytes=request.expected_size_bytes,
            checksum_sha256=request.checksum_sha256,
            original_filename=request.original_filename,
            lifecycle_state="uploading",
            retention_class=request.retention_class,
            camera_id=request.provenance.camera_id if request.provenance else None,
            session_id=request.provenance.session_id if request.provenance else None,
            event_id=request.provenance.event_id if request.provenance else None,
            event_time=request.provenance.event_time if request.provenance else None,
            created_by_user_id=actor.actor_id,
            metadata_=request.custom_metadata,
        )

        await self._media_repo.create_for_actor(session, actor, media)

        # Step 4: StoragePort upload initialization
        try:
            init_req = UploadInitiationRequest(
                object_key=object_key,
                content_type=request.content_type,
                expected_size_bytes=request.expected_size_bytes,
                checksum_sha256=request.checksum_sha256,
                custom_metadata={
                    "media_id": str(media_id),
                    "tenant_id": str(actor.tenant_id),
                    "venue_id": str(request.venue_id),
                    "category": request.category.value,
                },
            )
            upload_result = await self._storage.generate_presigned_upload_url(init_req)
        except StorageError as exc:
            logger.error(
                "Storage upload initialization failed for media_id=%s object_key=%s: %s",
                media_id,
                object_key,
                exc,
            )
            # Mark database record as failed on storage initialization error
            media.lifecycle_state = "failed"
            await session.flush()
            msg = f"Storage upload initialization failed for '{object_key}': {exc}"
            raise StorageOperationError(msg, cause=exc) from exc

        # Step 5: Audit recording — emitted through the transactional
        # outbox so the audit row + outbox row commit atomically with
        # the media record (Task 9.15).
        media.lifecycle_state = "uploading"
        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_UPLOAD_INITIATED,
            media=media,
            correlation_id=correlation_id,
            extra_payload={"content_type": request.content_type},
        )

        logger.info(
            "Media upload initialized successfully: media_id=%s tenant_id=%s venue_id=%s object_key=%s",
            media_id,
            actor.tenant_id,
            request.venue_id,
            object_key,
        )

        return MediaUploadInitiateResponse(
            media_id=media_id,
            object_key=object_key,
            storage_uri=storage_uri,
            upload_url=upload_result.upload_url,
            required_headers=upload_result.required_headers,
            expires_in_seconds=upload_result.expires_in_seconds,
            lifecycle_state=MediaLifecycleState.UPLOADING,
        )
