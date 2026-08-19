"""Unit tests for the media cleanup/reconciliation worker (Task 9.13).

Validates the four sweeps: retention expiry, stale uploads, missing
objects (with grace), and orphan objects (report-only by default).
Safety: idempotency, transient-failure tolerance, and the absence of
accidental mass deletion.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.workers.media_cleanup import MediaCleanupWorker
from tests.unit.fakes import FakeMediaRepository, make_media

MP4_PAYLOAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 96


def _stream(payload: bytes) -> AsyncIterator[bytes]:
    async def _gen() -> AsyncIterator[bytes]:
        yield payload

    return _gen()


class FakeDatabaseClient:
    """Minimal stand-in: yields a shared session per unit of work."""

    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class FakeSession:
    """A session that records added objects and accepts flushes."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,  # type: ignore[call-arg]
        OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
        OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
        OBJECT_STORAGE_ACCESS_KEY="test-key",
        OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
        MEDIA_CLEANUP_BATCH_SIZE=50,
        MEDIA_UPLOAD_TIMEOUT_SECONDS=60,
        MEDIA_MISSING_OBJECT_GRACE_SECONDS=3600,
        MEDIA_ORPHAN_OBJECT_GRACE_SECONDS=86400,
        MEDIA_ORPHAN_OBJECT_DELETION_ENABLED=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("c7a10f82-84b2-4d7a-b50a-bdfd189196b0")


@pytest.fixture
def venue_id() -> uuid.UUID:
    return uuid.UUID("4a87265a-063a-4a6c-9c70-7613768b4ad3")


def _make_worker(
    *,
    repo: FakeMediaRepository,
    storage: FakeStorageAdapter,
    session: FakeSession,
    settings: Settings,
) -> MediaCleanupWorker:
    db = FakeDatabaseClient(session)
    return MediaCleanupWorker(db, storage, settings, repo=repo)


async def _seed_object(storage: FakeStorageAdapter, object_key: str) -> None:
    await storage.put_object_stream(
        object_key,
        _stream(MP4_PAYLOAD),
        content_type="video/mp4",
        size_bytes=len(MP4_PAYLOAD),
    )


def _age_object(storage: FakeStorageAdapter, object_key: str, days: int) -> None:
    """Rewrite an object's metadata timestamp to make it look old."""
    data, meta = storage._objects[object_key]
    old = datetime.now(UTC) - timedelta(days=days)
    storage._objects[object_key] = (
        data,
        meta.__class__(
            object_key=meta.object_key,
            size_bytes=meta.size_bytes,
            content_type=meta.content_type,
            etag=meta.etag,
            last_modified=old,
            checksum_sha256=meta.checksum_sha256,
            custom_metadata=meta.custom_metadata,
        ),
    )


class TestExpirySweep:
    """Retention expiry → two-phase deletion."""

    async def test_expired_media_deleted(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        repo.seed(media)
        await _seed_object(storage, media.object_key)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        handled = await worker.run_once()

        assert handled >= 1
        assert media.lifecycle_state == "deleted"
        assert await storage.object_exists(media.object_key) is False

    async def test_unexpired_media_untouched(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
            expires_at=datetime.now(UTC) + timedelta(days=10),
        )
        repo.seed(media)
        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()
        assert media.lifecycle_state == "available"

    async def test_protected_media_never_deleted(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
            expires_at=datetime.now(UTC) - timedelta(days=1),
            metadata_={"preservation_hold": "true"},
        )
        repo.seed(media)
        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()
        assert media.lifecycle_state == "available"  # never swept


class TestStaleUploadSweep:
    """Abandoned UPLOADING records are failed and cleaned up."""

    async def test_stale_upload_failed(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="uploading",
            created_at=datetime.now(UTC) - timedelta(hours=2),
            updated_at=datetime.now(UTC) - timedelta(hours=2),
        )
        repo.seed(media)
        await _seed_object(storage, media.object_key)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        assert media.lifecycle_state == "failed"
        # Partial bytes were cleaned up.
        assert await storage.object_exists(media.object_key) is False

    async def test_fresh_upload_untouched(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="uploading",
            created_at=datetime.now(UTC),
        )
        repo.seed(media)
        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()
        assert media.lifecycle_state == "uploading"


class TestMissingObjectReconciliation:
    """Type-B orphans: record exists, object does not."""

    async def test_missing_object_marked_failed_after_grace(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
            updated_at=datetime.now(UTC) - timedelta(days=3),
        )
        repo.seed(media)  # no object in storage

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        assert media.lifecycle_state == "failed"

    async def test_recent_missing_object_kept_during_grace(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
            updated_at=datetime.now(UTC),  # well within the grace window
        )
        repo.seed(media)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        assert media.lifecycle_state == "available"

    async def test_transient_storage_failure_tolerated(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="available",
            size_bytes=len(MP4_PAYLOAD),
            updated_at=datetime.now(UTC) - timedelta(days=3),
        )
        repo.seed(media)
        storage.simulate_unavailable(True)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        # Transient outage must not nuke a valid record.
        assert media.lifecycle_state == "available"


class TestOrphanObjectScan:
    """Type-A orphans: object exists, record does not (or is terminal)."""

    async def test_orphan_reported_but_not_deleted_by_default(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        # An existing record keeps the pair in scope for scanning.
        existing = make_media(tenant_id=tenant_id, venue_id=venue_id, lifecycle_state="available")
        repo.seed(existing)
        await _seed_object(storage, existing.object_key)

        # The orphan: an object key with NO record and a key parseable.
        orphan_key = (
            f"tenants/{tenant_id}/venues/{venue_id}/temporary/"
            f"{datetime.now(UTC).year:04d}/{(datetime.now(UTC).month):02d}/"
            f"{(datetime.now(UTC).day):02d}/{uuid.uuid4()}.bin"
        )
        await _seed_object(storage, orphan_key)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        # Deletion is off by default — the orphan object survives.
        assert await storage.object_exists(orphan_key) is True

    async def test_orphan_deleted_when_enabled_and_aged(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        existing = make_media(tenant_id=tenant_id, venue_id=venue_id, lifecycle_state="available")
        repo.seed(existing)
        await _seed_object(storage, existing.object_key)

        orphan_key = (
            f"tenants/{tenant_id}/venues/{venue_id}/temporary/"
            f"{datetime.now(UTC).year:04d}/{datetime.now(UTC).month:02d}/"
            f"{datetime.now(UTC).day:02d}/{uuid.uuid4()}.bin"
        )
        await _seed_object(storage, orphan_key)
        _age_object(storage, orphan_key, days=30)

        worker = _make_worker(
            repo=repo,
            storage=storage,
            session=session,
            settings=_settings(MEDIA_ORPHAN_OBJECT_DELETION_ENABLED=True),
        )
        await worker.run_once()

        assert await storage.object_exists(orphan_key) is False

    async def test_live_uploading_record_never_orphaned(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        # An UPLOADING record owns this object — the client may be mid-upload.
        media = make_media(tenant_id=tenant_id, venue_id=venue_id, lifecycle_state="uploading")
        repo.seed(media)
        await _seed_object(storage, media.object_key)

        worker = _make_worker(
            repo=repo,
            storage=storage,
            session=session,
            settings=_settings(MEDIA_ORPHAN_OBJECT_DELETION_ENABLED=True),
        )
        await worker.run_once()

        assert await storage.object_exists(media.object_key) is True

    async def test_available_record_object_never_deleted_as_orphan(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        """Data-loss regression: an object owned by a LIVE record must never
        be treated as an orphan, even with deletion enabled + aged."""
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(tenant_id=tenant_id, venue_id=venue_id, lifecycle_state="available")
        repo.seed(media)
        await _seed_object(storage, media.object_key)
        _age_object(storage, media.object_key, days=90)  # far past any grace

        worker = _make_worker(
            repo=repo,
            storage=storage,
            session=session,
            settings=_settings(MEDIA_ORPHAN_OBJECT_DELETION_ENABLED=True),
        )
        await worker.run_once()

        assert media.lifecycle_state == "available"
        assert await storage.object_exists(media.object_key) is True

    async def test_failed_record_leftover_object_is_orphan_eligible(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        """A leftover object whose record is terminal (failed) IS eligible."""
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        # The orphan scan is scoped to the temporary/ namespace — build the
        # failed record's key there.
        failed_key = (
            f"tenants/{tenant_id}/venues/{venue_id}/temporary/"
            f"{datetime.now(UTC).year:04d}/{datetime.now(UTC).month:02d}/"
            f"{datetime.now(UTC).day:02d}/{uuid.uuid4()}.bin"
        )
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="failed",
            object_key=failed_key,
        )
        repo.seed(media)
        await _seed_object(storage, media.object_key)
        _age_object(storage, media.object_key, days=30)

        worker = _make_worker(
            repo=repo,
            storage=storage,
            session=session,
            settings=_settings(MEDIA_ORPHAN_OBJECT_DELETION_ENABLED=True),
        )
        await worker.run_once()

        assert await storage.object_exists(media.object_key) is False

    async def test_deletion_pending_missing_object_completes_deletion(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        """A DELETION_PENDING record whose object is gone → DELETED, not FAILED."""
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="deletion_pending",
            updated_at=datetime.now(UTC) - timedelta(days=3),
        )
        repo.seed(media)  # object already gone

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        assert media.lifecycle_state == "deleted"

    async def test_expired_missing_object_completes_deletion(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="expired",
            updated_at=datetime.now(UTC) - timedelta(days=3),
        )
        repo.seed(media)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        assert media.lifecycle_state == "deleted"

    async def test_deletion_pending_retried_when_delete_failed(
        self,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        """A DELETION_PENDING record past expiry is retried (not stuck)."""
        repo = FakeMediaRepository()
        storage = FakeStorageAdapter()
        session = FakeSession()
        media = make_media(
            tenant_id=tenant_id,
            venue_id=venue_id,
            lifecycle_state="deletion_pending",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        repo.seed(media)
        await _seed_object(storage, media.object_key)

        worker = _make_worker(repo=repo, storage=storage, session=session, settings=_settings())
        await worker.run_once()

        assert media.lifecycle_state == "deleted"
        assert await storage.object_exists(media.object_key) is False
