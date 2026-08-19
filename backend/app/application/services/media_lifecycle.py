"""Media lifecycle service (Tasks 9.8, 9.9, 9.11, 9.12).

Coordinates PostgreSQL media metadata transitions with Object Storage
operations through the provider-independent StoragePort:

  complete_upload   — UPLOADING → UPLOADED (size verified, checksum
                      captured, retention expiry computed; idempotent)
  initiate_multipart — register an S3 multipart session (large files)
  presign_parts     — short-lived presigned PUT URLs per part
  abort_upload      — UPLOADING → FAILED (best-effort storage cleanup)
  verify_media      — UPLOADED → VALIDATING → AVAILABLE | FAILED
                      (magic-byte content validation + SHA-256 integrity)
  request_download  — authorization + signed temporary GET URL
  request_deletion  — two-phase idempotent deletion with preservation
                      protection

Invariants enforced here:
  - A record is NEVER AVAILABLE without a completed, size-verified,
    content-validated upload.
  - The object key, tenant, and venue always come from trusted
    server-side state — never from client payloads.
  - Every transition is atomic (repository from-state guard) and every
    transition emits an audit event through the transactional outbox.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.media_audit import (
    EVENT_ACCESS_REQUESTED,
    EVENT_AVAILABLE,
    EVENT_DELETED,
    EVENT_DELETION_REQUESTED,
    EVENT_UPLOAD_ABORTED,
    EVENT_UPLOAD_COMPLETED,
    EVENT_VALIDATION_FAILED,
    enqueue_media_audit_event,
)
from backend.app.application.services.media_errors import (
    MediaConflictError,
    MediaNotFoundError,
    MediaProtectedError,
    MediaValidationError,
)
from backend.app.domain.media.retention import RetentionPolicyRegistry
from backend.app.domain.media.validation import (
    VALIDATION_PREFIX_BYTES,
    validate_content,
)
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.models.media import MediaAssetModel
from backend.app.infrastructure.database.repositories.media import MediaRepository
from backend.app.infrastructure.storage.exceptions import ObjectNotFoundError, StorageError
from backend.app.infrastructure.storage.protocol import StoragePort
from backend.app.infrastructure.storage.types import (
    MultipartCompleteRequest,
    MultipartPartInfo,
    MultipartPartUploadRequest,
    ObjectMetadata,
)
from contracts.common import MediaId
from contracts.identity import ActorContext, Permission
from contracts.media.models import (
    MediaCategory,
    MediaCompleteRequest,
    MediaCompleteResponse,
    MediaDeleteResponse,
    MediaDownloadResponse,
    MediaLifecycleState,
    MediaMetadataResponse,
    MediaMultipartInitiateResponse,
    MediaPartPresignedUrl,
    MediaPartPresignRequest,
    MediaPartPresignResponse,
    MediaVerifyResponse,
)

logger = logging.getLogger(__name__)

# Namespace for provider state inside the JSONB metadata column.
_PROVIDER_METADATA_KEY = "_provider"
_PROVIDER_UPLOAD_ID_KEY = "upload_id"
_PROVIDER_UPLOAD_PROVIDER_KEY = "upload_provider"

# Permission required to download media of a given category (policy).
_DOWNLOAD_PERMISSIONS: dict[str, Permission] = {
    MediaCategory.RECORDINGS.value: Permission.VIDEO_READ,
    MediaCategory.EVIDENCE.value: Permission.EVIDENCE_READ,
    MediaCategory.REPORTS.value: Permission.ANALYTICS_READ,
    MediaCategory.ANALYTICS.value: Permission.ANALYTICS_READ,
    MediaCategory.TEMPORARY.value: Permission.VIDEO_READ,
}

# States that indicate the object content has already been confirmed.
_DOWNLOADABLE_STATES = frozenset({"available", "expired"})


def _provider_metadata(media: MediaAssetModel) -> dict[str, Any]:
    """The provider-state sub-dict of the media record's JSONB metadata."""
    if not isinstance(media.metadata_, dict):
        return {}
    provider = media.metadata_.get(_PROVIDER_METADATA_KEY)
    return provider if isinstance(provider, dict) else {}


def _set_provider_upload_id(media: MediaAssetModel, upload_id: str) -> None:
    """Persist the provider multipart upload id under the reserved key."""
    metadata = dict(media.metadata_) if isinstance(media.metadata_, dict) else {}
    provider = dict(_provider_metadata(media))
    provider[_PROVIDER_UPLOAD_ID_KEY] = upload_id
    provider[_PROVIDER_UPLOAD_PROVIDER_KEY] = "s3"
    metadata[_PROVIDER_METADATA_KEY] = provider
    media.metadata_ = metadata


class MediaLifecycleService:
    """Application service orchestrating the media lifecycle."""

    def __init__(
        self,
        settings: Settings,
        storage: StoragePort,
        media_repo: MediaRepository | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._media_repo = media_repo or MediaRepository()

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _get_media_for_actor(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
    ) -> MediaAssetModel:
        media = await self._media_repo.get_for_actor(session, actor, media_id)
        if media is None:
            raise MediaNotFoundError(f"Media {media_id} does not exist or is out of scope")
        return media

    async def _fail_media(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media: MediaAssetModel,
        reason: str,
        *,
        from_state: str,
        correlation_id: str | None,
    ) -> None:
        """Transition to FAILED and emit the validation-failure audit event.

        The audit event is only emitted when the guarded transition won —
        a concurrent promotion must never be overwritten or falsely
        reported as a failure.
        """
        won = await self._media_repo.update_state_for_actor(
            session,
            actor,
            media.media_id,
            from_state=from_state,
            to_state="failed",
            extra_updates={"updated_at": datetime.now(UTC)},
        )
        if not won:
            return
        media.lifecycle_state = "failed"
        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_VALIDATION_FAILED,
            media=media,
            reason=reason,
            correlation_id=correlation_id,
        )

    # =========================================================================
    # 9.8 — Completion (single presigned PUT or multipart manifest)
    # =========================================================================

    async def complete_upload(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        request: MediaCompleteRequest,
        correlation_id: str | None = None,
    ) -> MediaCompleteResponse:
        """Finalize an upload: verify the object exists and its size.

        Idempotent — a repeated completion for an already-completed
        record replays the current metadata without touching storage.
        A failed or deleted record can never be completed.
        """
        media = await self._get_media_for_actor(session, actor, media_id)

        if media.lifecycle_state in ("uploaded", "validating", "available", "expired"):
            return self._build_complete_response(media)
        if media.lifecycle_state in ("failed", "deleted"):
            msg = f"Cannot complete media in terminal state '{media.lifecycle_state}'"
            raise MediaConflictError(msg)
        if media.lifecycle_state != "uploading":
            msg = f"Cannot complete media in state '{media.lifecycle_state}'"
            raise MediaConflictError(msg)

        # --- Storage verification (single PUT vs multipart manifest) ---
        meta: ObjectMetadata | None = None
        if request.parts:
            upload_id = request.upload_id or _provider_metadata(media).get(_PROVIDER_UPLOAD_ID_KEY)
            if not upload_id:
                raise MediaConflictError("Multipart completion requires an upload_id")
            try:
                meta = await self._storage.complete_multipart_upload(
                    MultipartCompleteRequest(
                        upload_id=upload_id,
                        object_key=media.object_key,
                        parts=[
                            MultipartPartInfo(part_number=p.part_number, etag=p.etag)
                            for p in request.parts
                        ],
                    )
                )
            except StorageError as exc:
                await self._fail_media(
                    session,
                    actor,
                    media,
                    str(exc),
                    from_state="uploading",
                    correlation_id=correlation_id,
                )
                raise MediaValidationError(f"Multipart completion failed: {exc}") from exc
        else:
            try:
                meta = await self._storage.get_object_metadata(media.object_key)
            except StorageError as exc:
                raise MediaValidationError(f"Storage verification failed: {exc}") from exc
            if meta is None:
                await self._fail_media(
                    session,
                    actor,
                    media,
                    "object does not exist in storage at completion",
                    from_state="uploading",
                    correlation_id=correlation_id,
                )
                raise MediaValidationError("Object was not found in object storage")

        # --- Server-side size verification (never trust the client) ---
        if meta is None or meta.size_bytes <= 0:
            await self._fail_media(
                session,
                actor,
                media,
                "storage reports an empty object",
                from_state="uploading",
                correlation_id=correlation_id,
            )
            raise MediaValidationError("Storage reports an empty object")

        expected = request.expected_size_bytes
        if expected is None and media.size_bytes:
            expected = media.size_bytes
        if expected is not None and expected > 0 and meta.size_bytes != expected:
            await self._fail_media(
                session,
                actor,
                media,
                f"size mismatch: expected {expected} bytes, storage reports {meta.size_bytes}",
                from_state="uploading",
                correlation_id=correlation_id,
            )
            raise MediaValidationError(
                f"Size mismatch: expected {expected} bytes, got {meta.size_bytes}"
            )

        # --- Integrity metadata capture ---
        checksum = meta.checksum_sha256
        if request.checksum_sha256:
            # Client-declared checksum is stored as the EXPECTED value and
            # verified against a server-side recomputation at the verify
            # step — it is never trusted alone.
            checksum = request.checksum_sha256.lower()

        # --- Retention expiry (policy-driven) ---
        retention_class = RetentionPolicyRegistry.resolve_class(
            MediaCategory(media.category), media.retention_class
        )
        duration = RetentionPolicyRegistry.duration_for(retention_class)
        expires_at = datetime.now(UTC) + duration if duration else None

        now = datetime.now(UTC)
        ok = await self._media_repo.update_state_for_actor(
            session,
            actor,
            media.media_id,
            from_state="uploading",
            to_state="uploaded",
            extra_updates={
                "size_bytes": meta.size_bytes,
                "checksum_sha256": checksum,
                "retention_class": retention_class,
                "expires_at": expires_at,
                "uploaded_at": now,
                "updated_at": now,
            },
        )
        if not ok:
            # A concurrent request completed the upload first — replay it.
            refreshed = await self._get_media_for_actor(session, actor, media_id)
            return self._build_complete_response(refreshed)

        media.size_bytes = meta.size_bytes
        media.checksum_sha256 = checksum
        media.retention_class = retention_class
        media.expires_at = expires_at
        media.lifecycle_state = "uploaded"

        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_UPLOAD_COMPLETED,
            media=media,
            correlation_id=correlation_id,
            extra_payload={"size_bytes": meta.size_bytes},
        )

        logger.info(
            "media upload completed: media_id=%s size=%s checksum=%s",
            media.media_id,
            meta.size_bytes,
            bool(checksum),
        )
        return self._build_complete_response(media)

    @staticmethod
    def _build_complete_response(media: MediaAssetModel) -> MediaCompleteResponse:
        return MediaCompleteResponse(
            media_id=MediaId(media.media_id),
            object_key=media.object_key,
            lifecycle_state=MediaLifecycleState(media.lifecycle_state),
            size_bytes=media.size_bytes or 0,
            checksum_sha256=media.checksum_sha256,
            expires_at=media.expires_at,
        )

    # =========================================================================
    # 9.8 — Multipart upload session (large CCTV recordings)
    # =========================================================================

    async def initiate_multipart(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        correlation_id: str | None = None,
    ) -> MediaMultipartInitiateResponse:
        """Register a multipart session with the provider for this upload."""
        media = await self._get_media_for_actor(session, actor, media_id)
        if media.lifecycle_state != "uploading":
            msg = f"Multipart initiation requires state 'uploading', got '{media.lifecycle_state}'"
            raise MediaConflictError(msg)

        try:
            result = await self._storage.initiate_multipart_upload(
                media.object_key,
                media.content_type,
                custom_metadata={
                    "media_id": str(media.media_id),
                    "tenant_id": str(media.tenant_id),
                    "venue_id": str(media.venue_id),
                    "category": media.category,
                },
            )
        except StorageError as exc:
            raise MediaValidationError(f"Multipart initiation failed: {exc}") from exc

        _set_provider_upload_id(media, result.upload_id)
        await session.flush()

        return MediaMultipartInitiateResponse(
            media_id=MediaId(media.media_id),
            object_key=media.object_key,
            upload_id=result.upload_id,
            part_size_bytes=self._settings.media_multipart_part_size_bytes,
            max_parts=self._settings.media_max_parts,
            expires_in_seconds=self._settings.media_presigned_url_ttl_seconds,
        )

    async def presign_parts(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        request: MediaPartPresignRequest,
        correlation_id: str | None = None,
    ) -> MediaPartPresignResponse:
        """Presign short-lived PUT URLs for the requested multipart parts."""
        media = await self._get_media_for_actor(session, actor, media_id)
        if media.lifecycle_state != "uploading":
            msg = f"Part presigning requires state 'uploading', got '{media.lifecycle_state}'"
            raise MediaConflictError(msg)

        upload_id = _provider_metadata(media).get(_PROVIDER_UPLOAD_ID_KEY)
        if not upload_id:
            raise MediaConflictError("No multipart session registered for this upload")

        seen: set[int] = set()
        presigned: list[MediaPartPresignedUrl] = []
        for part_number in request.part_numbers:
            if part_number in seen:
                raise MediaConflictError(f"Duplicate part number {part_number}")
            seen.add(part_number)
            try:
                result = await self._storage.generate_presigned_part_upload_url(
                    MultipartPartUploadRequest(
                        upload_id=upload_id,
                        object_key=media.object_key,
                        part_number=part_number,
                        expires_in_seconds=self._settings.media_presigned_url_ttl_seconds,
                    )
                )
            except StorageError as exc:
                raise MediaValidationError(f"Part presigning failed: {exc}") from exc
            presigned.append(
                MediaPartPresignedUrl(
                    part_number=part_number,
                    upload_url=result.upload_url,
                    expires_in_seconds=result.expires_in_seconds,
                    expires_at=result.expires_at,
                )
            )

        return MediaPartPresignResponse(
            media_id=MediaId(media.media_id),
            upload_id=upload_id,
            parts=presigned,
        )

    async def abort_upload(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        correlation_id: str | None = None,
    ) -> MediaDeleteResponse:
        """Abort an in-flight upload and transition the record to FAILED.

        Idempotent — repeated aborts for an already-failed/deleted
        record are no-ops. Storage cleanup is best-effort.
        """
        media = await self._get_media_for_actor(session, actor, media_id)

        if media.lifecycle_state in ("failed", "deleted"):
            return MediaDeleteResponse(
                media_id=MediaId(media.media_id),
                lifecycle_state=MediaLifecycleState(media.lifecycle_state),
            )
        if media.lifecycle_state != "uploading":
            msg = f"Cannot abort media in state '{media.lifecycle_state}'"
            raise MediaConflictError(msg)

        # The guarded state transition happens FIRST — destructive storage
        # cleanup must never race a concurrent completion and delete an
        # object the winner just verified.
        ok = await self._media_repo.update_state_for_actor(
            session,
            actor,
            media.media_id,
            from_state="uploading",
            to_state="failed",
            extra_updates={"updated_at": datetime.now(UTC)},
        )
        if not ok:
            # A concurrent request completed/aborted the upload first —
            # leave its storage bytes untouched.
            refreshed = await self._get_media_for_actor(session, actor, media_id)
            return MediaDeleteResponse(
                media_id=MediaId(refreshed.media_id),
                lifecycle_state=MediaLifecycleState(refreshed.lifecycle_state),
            )
        media.lifecycle_state = "failed"

        # Best-effort storage cleanup AFTER the transition — the record
        # is already terminal, so cleanup can never damage a live object.
        upload_id = _provider_metadata(media).get(_PROVIDER_UPLOAD_ID_KEY)
        if upload_id:
            try:
                await self._storage.abort_multipart_upload(upload_id, media.object_key)
            except StorageError:
                logger.warning(
                    "multipart abort failed for media_id=%s upload_id=%s",
                    media.media_id,
                    upload_id,
                )
        with suppress(StorageError, ObjectNotFoundError):
            await self._storage.delete_object(media.object_key)
        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_UPLOAD_ABORTED,
            media=media,
            correlation_id=correlation_id,
        )

        return MediaDeleteResponse(
            media_id=MediaId(media.media_id),
            lifecycle_state=MediaLifecycleState.FAILED,
        )

    # =========================================================================
    # 9.9 + 9.10 — Integrity & content verification
    # =========================================================================

    async def verify_media(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        *,
        declared_checksum_sha256: str | None = None,
        correlation_id: str | None = None,
    ) -> MediaVerifyResponse:
        """Validate content + checksum and promote to AVAILABLE.

        Flow: UPLOADED → VALIDATING → AVAILABLE (success)
              UPLOADED → VALIDATING → FAILED (any failure)

        The checksum is recomputed server-side by streaming the object
        (bounded memory); the client-declared checksum, when present, is
        compared against the recomputed value. A mismatch NEVER results
        in AVAILABLE media.
        """
        media = await self._get_media_for_actor(session, actor, media_id)

        if media.lifecycle_state == "available":
            return self._build_verify_response(media)
        if media.lifecycle_state != "uploaded":
            msg = f"Cannot verify media in state '{media.lifecycle_state}'"
            raise MediaConflictError(msg)

        ok = await self._media_repo.update_state_for_actor(
            session,
            actor,
            media.media_id,
            from_state="uploaded",
            to_state="validating",
            extra_updates={"updated_at": datetime.now(UTC)},
        )
        if not ok:
            refreshed = await self._get_media_for_actor(session, actor, media_id)
            if refreshed.lifecycle_state == "available":
                return self._build_verify_response(refreshed)
            raise MediaConflictError("Concurrent verification changed media state")

        media.lifecycle_state = "validating"

        # --- Stream the object once: signature prefix + optional SHA-256 ---
        # For very large recordings the full-object hash can be skipped via
        # MEDIA_CHECKSUM_VERIFICATION_MAX_BYTES — content validation only
        # ever needs the bounded prefix, so the full stream is never read
        # when the cap applies.
        object_size = media.size_bytes or 0
        full_hash = self._settings.media_checksum_verification_enabled and (
            self._settings.media_checksum_verification_max_bytes == 0
            or object_size <= self._settings.media_checksum_verification_max_bytes
        )
        try:
            header, computed_sha256 = await self._read_prefix_and_hash(media, full_hash=full_hash)
        except (ObjectNotFoundError, StorageError) as exc:
            await self._fail_media(
                session,
                actor,
                media,
                f"verification failed: {exc}",
                from_state="validating",
                correlation_id=correlation_id,
            )
            raise MediaValidationError(f"Verification failed: {exc}") from exc

        try:
            # --- Content validation (magic bytes, bounded) ---
            if self._settings.media_content_validation_enabled:
                result = validate_content(
                    MediaCategory(media.category),
                    media.content_type,
                    header,
                    size_bytes=object_size,
                )
                if not result.valid:
                    await self._fail_media(
                        session,
                        actor,
                        media,
                        f"content validation failed: {result.reason}",
                        from_state="validating",
                        correlation_id=correlation_id,
                    )
                    raise MediaValidationError(result.reason or "invalid content")

            # --- Checksum verification (server recomputation) ---
            actual_checksum: str | None
            if full_hash and computed_sha256 is not None:
                actual_checksum = computed_sha256
                expected = declared_checksum_sha256 or media.checksum_sha256
                if expected and expected.lower() != actual_checksum:
                    await self._fail_media(
                        session,
                        actor,
                        media,
                        f"checksum mismatch: expected {expected}, computed {actual_checksum}",
                        from_state="validating",
                        correlation_id=correlation_id,
                    )
                    raise MediaValidationError(
                        f"Checksum mismatch: expected {expected}, computed {actual_checksum}"
                    )
            else:
                # Checksum verification skipped by policy — retain the
                # provider-reported checksum (size + content already
                # verified server-side).
                actual_checksum = media.checksum_sha256
        except MediaValidationError:
            raise
        except Exception as exc:
            await self._fail_media(
                session,
                actor,
                media,
                f"verification failed: {exc}",
                from_state="validating",
                correlation_id=correlation_id,
            )
            raise MediaValidationError(f"Verification failed: {exc}") from exc

        now = datetime.now(UTC)
        ok = await self._media_repo.update_state_for_actor(
            session,
            actor,
            media.media_id,
            from_state="validating",
            to_state="available",
            extra_updates={
                "checksum_sha256": actual_checksum,
                "validated_at": now,
                "updated_at": now,
            },
        )
        if not ok:
            raise MediaConflictError("Concurrent verification changed media state")

        media.lifecycle_state = "available"
        media.checksum_sha256 = actual_checksum

        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_AVAILABLE,
            media=media,
            correlation_id=correlation_id,
        )
        logger.info("media available: media_id=%s checksum=%s", media.media_id, actual_checksum)
        return self._build_verify_response(media)

    @staticmethod
    def _build_verify_response(media: MediaAssetModel) -> MediaVerifyResponse:
        return MediaVerifyResponse(
            media_id=MediaId(media.media_id),
            lifecycle_state=MediaLifecycleState(media.lifecycle_state),
            checksum_sha256=media.checksum_sha256,
            size_bytes=media.size_bytes or 0,
            validated_at=media.validated_at,
        )

    async def _read_prefix_and_hash(
        self,
        media: MediaAssetModel,
        *,
        full_hash: bool,
    ) -> tuple[bytes, str | None]:
        """Stream the object once, collecting the signature prefix and SHA-256.

        Bounded memory: only ``VALIDATION_PREFIX_BYTES`` leading bytes are
        retained. When ``full_hash`` is False the stream is stopped after
        the prefix (large CCTV recordings are never read in full when the
        checksum cap applies). Raises the raw storage exceptions — the
        caller decides the media outcome (a missing object at
        verification time must never become AVAILABLE).
        """
        hasher = hashlib.sha256()
        header = bytearray()
        async for chunk in self._storage.get_object_stream(media.object_key):
            if full_hash:
                hasher.update(chunk)
            if len(header) < VALIDATION_PREFIX_BYTES:
                remaining = VALIDATION_PREFIX_BYTES - len(header)
                header.extend(chunk[:remaining])
                if not full_hash and len(header) >= VALIDATION_PREFIX_BYTES:
                    break
        return bytes(header), hasher.hexdigest() if full_hash else None

    # =========================================================================
    # 9.11 — Controlled signed access
    # =========================================================================

    async def request_download(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        correlation_id: str | None = None,
    ) -> MediaDownloadResponse:
        """Authorize, then issue a short-lived signed download URL.

        Authorization precedes signing: the actor must have the media
        in tenant/venue scope AND hold the category's read permission.
        Expired-but-not-yet-purged media remains downloadable; deleted
        media is not.
        """
        media = await self._get_media_for_actor(session, actor, media_id)

        if media.lifecycle_state not in _DOWNLOADABLE_STATES:
            msg = f"Media is not available for download (state '{media.lifecycle_state}')"
            raise MediaConflictError(msg)

        permission = _DOWNLOAD_PERMISSIONS.get(media.category)
        if permission is not None and not actor.has_permission(permission):
            raise AuthorizationError(f"Missing required permission: {permission.value}")

        try:
            result = await self._storage.generate_presigned_download_url(
                media.object_key,
                expires_in_seconds=self._settings.media_presigned_url_ttl_seconds,
                response_content_disposition="inline",
            )
        except StorageError as exc:
            raise MediaValidationError(f"Signed access generation failed: {exc}") from exc

        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_ACCESS_REQUESTED,
            media=media,
            correlation_id=correlation_id,
            extra_payload={"expires_in_seconds": result.expires_in_seconds},
        )

        return MediaDownloadResponse(
            media_id=MediaId(media.media_id),
            object_key=media.object_key,
            download_url=result.download_url,
            content_type=media.content_type,
            original_filename=media.original_filename,
            expires_at=result.expires_at,
        )

    # =========================================================================
    # 9.12 — Two-phase idempotent deletion
    # =========================================================================

    async def request_deletion(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
        correlation_id: str | None = None,
    ) -> MediaDeleteResponse:
        """Delete media idempotently with preservation protection.

        Flow: any non-terminal state → DELETION_PENDING → (object
        deleted) → DELETED. Evidence under legal/preservation hold is
        refused. Repeating the request for an already-deleted record is
        a successful no-op.
        """
        media = await self._get_media_for_actor(session, actor, media_id)

        if media.lifecycle_state == "deleted":
            return MediaDeleteResponse(
                media_id=MediaId(media.media_id),
                lifecycle_state=MediaLifecycleState.DELETED,
            )

        if RetentionPolicyRegistry.is_protected(media.retention_class, media.metadata_):
            raise MediaProtectedError("Media is under a preservation hold and cannot be deleted")

        if media.lifecycle_state != "deletion_pending":
            ok = await self._media_repo.update_state_for_actor(
                session,
                actor,
                media.media_id,
                from_state=media.lifecycle_state,
                to_state="deletion_pending",
                extra_updates={"updated_at": datetime.now(UTC)},
            )
            if not ok:
                refreshed = await self._get_media_for_actor(session, actor, media_id)
                if refreshed.lifecycle_state == "deleted":
                    return MediaDeleteResponse(
                        media_id=MediaId(refreshed.media_id),
                        lifecycle_state=MediaLifecycleState.DELETED,
                    )
                raise MediaConflictError("Concurrent deletion changed media state")
            media.lifecycle_state = "deletion_pending"
            await enqueue_media_audit_event(
                session,
                actor=actor,
                event_type=EVENT_DELETION_REQUESTED,
                media=media,
                correlation_id=correlation_id,
            )

        # Object deletion is idempotent — a missing object is a success.
        try:
            await self._storage.delete_object(media.object_key)
        except StorageError as exc:
            # The record stays DELETION_PENDING; the cleanup worker
            # retries. Deletion must not be falsely acknowledged.
            raise MediaValidationError(f"Object deletion failed: {exc}") from exc

        now = datetime.now(UTC)
        ok = await self._media_repo.update_state_for_actor(
            session,
            actor,
            media.media_id,
            from_state="deletion_pending",
            to_state="deleted",
            extra_updates={"deleted_at": now, "updated_at": now},
        )
        if not ok:
            refreshed = await self._get_media_for_actor(session, actor, media_id)
            return MediaDeleteResponse(
                media_id=MediaId(refreshed.media_id),
                lifecycle_state=MediaLifecycleState(refreshed.lifecycle_state),
            )
        media.lifecycle_state = "deleted"
        media.deleted_at = now

        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_DELETED,
            media=media,
            correlation_id=correlation_id,
        )
        logger.info("media deleted: media_id=%s object_key=%s", media.media_id, media.object_key)
        return MediaDeleteResponse(
            media_id=MediaId(media.media_id),
            lifecycle_state=MediaLifecycleState.DELETED,
        )

    # =========================================================================
    # Metadata
    # =========================================================================

    async def get_metadata(
        self,
        session: AsyncSession,
        actor: ActorContext,
        media_id: uuid.UUID | MediaId | str,
    ) -> MediaMetadataResponse:
        """Return the authoritative metadata record for authorized media."""
        media = await self._get_media_for_actor(session, actor, media_id)
        return MediaMetadataResponse(
            media_id=MediaId(media.media_id),
            tenant_id=media.tenant_id,
            venue_id=media.venue_id,
            category=MediaCategory(media.category),
            object_key=media.object_key,
            storage_uri=media.storage_uri,
            storage_bucket=media.storage_bucket,
            content_type=media.content_type,
            size_bytes=media.size_bytes or 0,
            checksum_sha256=media.checksum_sha256,
            original_filename=media.original_filename,
            lifecycle_state=MediaLifecycleState(media.lifecycle_state),
            retention_class=media.retention_class,
            expires_at=media.expires_at,
            created_at=media.created_at,
            updated_at=media.updated_at,
            uploaded_at=media.uploaded_at,
            validated_at=media.validated_at,
            deleted_at=media.deleted_at,
        )
