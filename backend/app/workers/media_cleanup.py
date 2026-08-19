"""Media retention & orphan reconciliation worker (Task 9.13).

Runs periodic bounded sweeps over the media lifecycle using the shared
PollingWorker base — no competing worker framework:

  1. Expiry sweep       — AVAILABLE records past ``expires_at`` are
                          deleted (two-phase: DELETION_PENDING → object
                          delete → DELETED). Preservation-held records
                          are never touched.
  2. Stale upload sweep — UPLOADING records abandoned past the upload
                          timeout are marked FAILED; their multipart
                          session (if any) and partial object are
                          cleaned up best-effort.
  3. Missing-object     — records whose object vanished are marked
                          FAILED after a grace period (a transient
                          provider hiccup never nukes a valid record;
                          a missing object never stays AVAILABLE).
  4. Orphan-object scan — Type-A orphans (object exists, no record) are
                          detected and REPORTED. Deletion is disabled by
                          default (``MEDIA_ORPHAN_OBJECT_DELETION_ENABLED``)
                          and only applies to the ``temporary/``
                          namespace after a grace period — accidental
                          mass deletion is architecturally impossible.

Safety properties:
  - Every sweep is bounded by ``MEDIA_CLEANUP_BATCH_SIZE`` per cycle.
  - DB state transitions are short transactions; storage I/O happens
    OUTSIDE those transactions (the outbox worker pattern).
  - Every state change is guarded (from-state atomically matched) and
    every failure is retried on the next cycle.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from backend.app.application.services.media_audit import (
    EVENT_CLEANUP_FAILED,
    EVENT_DELETED,
    EVENT_DELETION_REQUESTED,
    EVENT_UPLOAD_ABORTED,
    enqueue_media_audit_event,
    system_actor,
)
from backend.app.domain.media.retention import RetentionPolicyRegistry
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.database.models.media import MediaAssetModel
from backend.app.infrastructure.database.repositories.media import (
    RECONCILABLE_STATES,
    MediaRepository,
)
from backend.app.infrastructure.storage.exceptions import StorageError
from backend.app.infrastructure.storage.key_builder import parse_object_key
from backend.app.infrastructure.storage.protocol import StoragePort
from backend.app.workers.base import PollingWorker

logger = logging.getLogger(__name__)


class MediaCleanupWorker(PollingWorker):
    """Bounded, auditable, idempotent media lifecycle maintenance worker."""

    def __init__(
        self,
        database: DatabaseClient,
        storage: StoragePort,
        settings: Settings,
        *,
        worker_id: str | None = None,
        repo: MediaRepository | None = None,
    ) -> None:
        super().__init__(
            poll_interval=settings.media_cleanup_poll_interval,
            worker_id=worker_id or f"media-cleanup:{uuid.uuid4().hex[:8]}",
        )
        self._database = database
        self._storage = storage
        self._settings = settings
        self._repo = repo or MediaRepository()
        self._batch_size = settings.media_cleanup_batch_size

    # =========================================================================
    # Cycle
    # =========================================================================

    async def run_once(self) -> int:
        """Run all bounded sweeps; returns the number of records handled."""
        handled = 0
        handled += await self._sweep_expired()
        if self._stop_event.is_set():
            return handled
        handled += await self._sweep_stale_uploads()
        if self._stop_event.is_set():
            return handled
        handled += await self._reconcile_missing_objects()
        if self._stop_event.is_set():
            return handled
        handled += await self._scan_orphan_objects()
        return handled

    # =========================================================================
    # 1. Retention expiry
    # =========================================================================

    async def _sweep_expired(self) -> int:
        now = datetime.now(UTC)
        async with self._database.session() as session:
            expired = await self._repo.list_expired(session, now=now, limit=self._batch_size)

        handled = 0
        for media in expired:
            if self._stop_event.is_set():
                break
            try:
                if await self._delete_expired(media):
                    handled += 1
            except Exception:
                logger.exception("expiry sweep failed for media_id=%s", media.media_id)
        return handled

    async def _delete_expired(self, media: MediaAssetModel) -> bool:
        """Two-phase deletion of one expired record (retry-safe)."""
        # Defense in depth: protected records are never deleted by policy,
        # even if a data race wrote an expires_at onto one.
        if RetentionPolicyRegistry.is_protected(media.retention_class, media.metadata_):
            logger.warning("refusing to delete protected media: media_id=%s", media.media_id)
            return False
        actor = system_actor(media.tenant_id)

        # Phase 1: ensure DELETION_PENDING (atomic, short txn). Records
        # already in DELETION_PENDING (a prior delete failed) are retried
        # rather than skipped.
        if media.lifecycle_state == "available":
            async with self._database.session() as session:
                if not await self._repo.update_state_unscoped(
                    session,
                    media.media_id,
                    from_state="available",
                    to_state="deletion_pending",
                    extra_updates={"updated_at": datetime.now(UTC)},
                ):
                    return False  # concurrent change — next cycle re-evaluates
                media.lifecycle_state = "deletion_pending"
                await enqueue_media_audit_event(
                    session,
                    actor=actor,
                    event_type=EVENT_DELETION_REQUESTED,
                    media=media,
                    extra_payload={"trigger": "retention_expiry"},
                )
        elif media.lifecycle_state != "deletion_pending":
            return False

        # Phase 2: external storage delete (outside any transaction).
        try:
            await self._storage.delete_object(media.object_key)
        except StorageError as exc:
            async with self._database.session() as session:
                await enqueue_media_audit_event(
                    session,
                    actor=actor,
                    event_type=EVENT_CLEANUP_FAILED,
                    media=media,
                    reason=f"storage delete failed: {exc}",
                    extra_payload={"trigger": "retention_expiry"},
                )
            logger.warning(
                "retention delete failed for media_id=%s (will retry): %s",
                media.media_id,
                exc,
            )
            return False

        # Phase 3: DELETION_PENDING → DELETED.
        now = datetime.now(UTC)
        async with self._database.session() as session:
            if not await self._repo.update_state_unscoped(
                session,
                media.media_id,
                from_state="deletion_pending",
                to_state="deleted",
                extra_updates={"deleted_at": now, "updated_at": now},
            ):
                return False
            media.lifecycle_state = "deleted"
            await enqueue_media_audit_event(
                session,
                actor=actor,
                event_type=EVENT_DELETED,
                media=media,
                extra_payload={"trigger": "retention_expiry"},
            )
        logger.info(
            "media expired and deleted: media_id=%s object_key=%s",
            media.media_id,
            media.object_key,
        )
        return True

    # =========================================================================
    # 2. Stale uploads
    # =========================================================================

    async def _sweep_stale_uploads(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=self._settings.media_upload_timeout_seconds)
        async with self._database.session() as session:
            stale = await self._repo.list_stale_uploads(
                session, older_than=cutoff, limit=self._batch_size
            )

        handled = 0
        for media in stale:
            if self._stop_event.is_set():
                break
            try:
                if await self._fail_stale_upload(media):
                    handled += 1
            except Exception:
                logger.exception("stale-upload sweep failed for media_id=%s", media.media_id)
        return handled

    async def _fail_stale_upload(self, media: MediaAssetModel) -> bool:
        actor = system_actor(media.tenant_id)

        # Abort any multipart session and remove partial bytes (best effort).
        provider = media.metadata_.get("_provider") if isinstance(media.metadata_, dict) else None
        upload_id = provider.get("upload_id") if isinstance(provider, dict) else None
        if upload_id:
            try:
                await self._storage.abort_multipart_upload(upload_id, media.object_key)
            except StorageError:
                logger.debug("multipart abort failed for media_id=%s", media.media_id)
        with suppress(StorageError):
            await self._storage.delete_object(media.object_key)

        async with self._database.session() as session:
            if not await self._repo.update_state_unscoped(
                session,
                media.media_id,
                from_state="uploading",
                to_state="failed",
                extra_updates={"updated_at": datetime.now(UTC)},
            ):
                return False
            media.lifecycle_state = "failed"
            await enqueue_media_audit_event(
                session,
                actor=actor,
                event_type=EVENT_UPLOAD_ABORTED,
                media=media,
                extra_payload={"trigger": "upload_timeout"},
            )
        logger.info("stale upload failed: media_id=%s", media.media_id)
        return True

    # =========================================================================
    # 3. Missing-object reconciliation (Type B)
    # =========================================================================

    async def _reconcile_missing_objects(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self._settings.media_missing_object_grace_seconds
        )
        async with self._database.session() as session:
            records = await self._repo.list_for_reconciliation(
                session, states=RECONCILABLE_STATES, older_than=cutoff, limit=self._batch_size
            )

        handled = 0
        for media in records:
            if self._stop_event.is_set():
                break
            try:
                if await self._check_object_presence(media):
                    handled += 1
            except Exception:
                logger.exception("reconciliation failed for media_id=%s", media.media_id)
        return handled

    async def _check_object_presence(self, media: MediaAssetModel) -> bool:
        try:
            exists = await self._storage.object_exists(media.object_key)
        except StorageError as exc:
            # Transient provider failure — leave the record untouched; the
            # next cycle re-checks after the grace window.
            logger.warning(
                "storage unavailable during reconciliation for media_id=%s: %s",
                media.media_id,
                exc,
            )
            return False

        if exists:
            return False

        actor = system_actor(media.tenant_id)
        now = datetime.now(UTC)
        async with self._database.session() as session:
            if media.lifecycle_state in ("deletion_pending", "expired"):
                # The object is already gone — deletion is achieved, not
                # a failure. Complete the lifecycle instead of stalling.
                if not await self._repo.update_state_unscoped(
                    session,
                    media.media_id,
                    from_state=media.lifecycle_state,
                    to_state="deleted",
                    extra_updates={"deleted_at": now, "updated_at": now},
                ):
                    return False
                media.lifecycle_state = "deleted"
                event_type = EVENT_DELETED
                reason = None
            else:
                if not await self._repo.update_state_unscoped(
                    session,
                    media.media_id,
                    from_state=media.lifecycle_state,
                    to_state="failed",
                    extra_updates={"updated_at": now},
                ):
                    return False
                media.lifecycle_state = "failed"
                event_type = EVENT_CLEANUP_FAILED
                reason = "object missing from storage after grace period"

            await enqueue_media_audit_event(
                session,
                actor=actor,
                event_type=event_type,
                media=media,
                reason=reason,
            )
        logger.warning(
            "media object missing; media_id=%s now %s",
            media.media_id,
            media.lifecycle_state,
        )
        return True

    # =========================================================================
    # 4. Orphan-object scan (Type A — report-only unless explicitly enabled)
    # =========================================================================

    async def _scan_orphan_objects(self) -> int:
        """Detect storage objects with no metadata record (bounded, scoped)."""
        async with self._database.session() as session:
            pairs = await self._repo.list_tenant_venue_pairs(session)

        handled = 0
        for tenant_id, venue_id in pairs:
            if self._stop_event.is_set():
                break
            prefix = f"tenants/{tenant_id}/venues/{venue_id}/temporary/"
            try:
                keys = await self._storage.list_objects(prefix, max_keys=self._batch_size)
            except StorageError as exc:
                logger.warning("orphan scan listing failed for prefix=%s: %s", prefix, exc)
                continue

            for key in keys:
                try:
                    if await self._evaluate_orphan(key, tenant_id, venue_id):
                        handled += 1
                except Exception:
                    logger.exception("orphan evaluation failed for key=%s", key)
        return handled

    async def _evaluate_orphan(
        self,
        object_key: str,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> bool:
        """Report (and optionally delete) one candidate orphan object."""
        try:
            parse_object_key(object_key)  # canonical-hierarchy validation
        except Exception:
            logger.warning("skipping unparseable object key during scan: %s", object_key)
            return False

        # A record owning this object means it is NOT an orphan. Only
        # records with no metadata row at all, or whose record is
        # terminal (failed/deleted — a leftover of failed cleanup), are
        # candidates. Live UPLOADING/AVAILABLE/etc. objects are never
        # touched — an object being present is never enough to delete.
        async with self._database.session() as session:
            media = await self._repo.get_by_object_key_unscoped(session, object_key)

        if media is not None and media.lifecycle_state not in ("failed", "deleted"):
            return False

        if not self._settings.media_orphan_object_deletion_enabled:
            logger.warning(
                "orphan object detected (report-only): tenant=%s venue=%s key=%s",
                tenant_id,
                venue_id,
                object_key,
            )
            return False

        # Grace period — never delete an object found in a single scan.
        try:
            meta = await self._storage.get_object_metadata(object_key)
        except StorageError:
            return False
        if meta is None:
            return False
        age = datetime.now(UTC) - meta.last_modified.replace(tzinfo=UTC)
        if age < timedelta(seconds=self._settings.media_orphan_object_grace_seconds):
            return False

        await self._storage.delete_object(object_key)
        logger.warning(
            "orphan object deleted: tenant=%s venue=%s key=%s age=%s",
            tenant_id,
            venue_id,
            object_key,
            age,
        )
        return True


async def _main() -> None:
    from backend.app.infrastructure.logging import configure_logging
    from backend.app.infrastructure.observability import tracing
    from backend.app.infrastructure.storage.s3_adapter import S3StorageAdapter

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings=settings)
    tracing.configure_tracing(settings)
    database = DatabaseClient(settings)
    await database.initialize()
    storage = S3StorageAdapter(settings)
    await storage.initialize()
    try:
        worker = MediaCleanupWorker(database, storage, settings)
        await worker.run_forever()
    finally:
        await storage.close()
        await database.dispose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
