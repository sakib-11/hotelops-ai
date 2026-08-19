"""Shared fakes for unit/security tests of the Task 7 reliability layer.

FakeIdempotencyRepository emulates the database-backed
IdempotencyRepository contract (get/create_claim/complete/reclaim) with
the unique-key serialization of a real PostgreSQL unique index: a
create_claim for an in-progress unit blocks until the holder completes
or rolls back (mirroring INSERT ... ON CONFLICT DO NOTHING blocking).
It also records every call so tests can assert tenant scoping.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.app.domain.evidence.extraction import (
    ExtractedEvidence,
    ExtractionCancellationToken,
    ExtractionStatus,
)
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceResolutionStatus,
)
from backend.app.infrastructure.database.models.media import MediaAssetModel
from contracts.common import MediaId, VideoAssetId, VideoSessionId, utc_now
from contracts.events import EvidenceRef
from contracts.identity import ActorContext, Permission, RoleName
from contracts.temporal import TEMPORAL_ID_NAMESPACE


def make_media(
    *,
    media_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    category: str = "recordings",
    object_key: str | None = None,
    content_type: str = "video/mp4",
    size_bytes: int = 0,
    checksum_sha256: str | None = None,
    lifecycle_state: str = "uploading",
    retention_class: str | None = None,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    metadata_: dict[str, Any] | None = None,
) -> MediaAssetModel:
    """Build a minimal MediaAssetModel for tests."""
    uid = media_id or uuid.uuid4()
    now = created_at or datetime.now(UTC)
    key = object_key or (
        f"tenants/{tenant_id}/venues/{venue_id}/recordings/"
        f"{now.year:04d}/{now.month:02d}/{now.day:02d}/{uid}.mp4"
    )
    return MediaAssetModel(
        media_id=uid,
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=category,
        object_key=key,
        storage_uri=f"s3://test-bucket/{key}",
        storage_bucket="test-bucket",
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum_sha256,
        original_filename=None,
        lifecycle_state=lifecycle_state,
        retention_class=retention_class,
        expires_at=expires_at,
        metadata_=metadata_,
        created_at=now,
        updated_at=updated_at or now,
    )


class FakeMediaRepository:
    """In-memory stand-in for MediaRepository (Task 9 service/worker tests).

    Mirrors the state-guarded transition semantics of the real repository:
    ``update_state_for_actor`` succeeds only when the record is in the
    expected from-state and within the actor's tenant/venue scope.
    """

    def __init__(self) -> None:
        self.records: dict[uuid.UUID, MediaAssetModel] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def seed(self, media: MediaAssetModel) -> MediaAssetModel:
        self.records[media.media_id] = media
        return media

    def _get_scoped(self, actor: ActorContext, media_id: Any) -> MediaAssetModel | None:
        uid = uuid.UUID(str(media_id))
        media = self.records.get(uid)
        if media is None:
            return None
        if str(media.tenant_id) != str(actor.tenant_id):
            return None
        if actor.venue_scope:
            scope = {uuid.UUID(str(v)) for v in actor.venue_scope}
            if media.venue_id not in scope:
                return None
        return media

    async def get_for_actor(
        self, session: Any, actor: ActorContext, media_id: Any
    ) -> MediaAssetModel | None:
        self.calls.append(("get_for_actor", (str(media_id),), {}))
        return self._get_scoped(actor, media_id)

    async def update_state_for_actor(
        self,
        session: Any,
        actor: ActorContext,
        media_id: Any,
        from_state: str,
        to_state: str,
        *,
        extra_updates: dict[str, Any] | None = None,
    ) -> bool:
        self.calls.append((
            "update_state_for_actor",
            (str(media_id), from_state, to_state),
            {"extra_updates": extra_updates},
        ))
        media = self._get_scoped(actor, media_id)
        if media is None or media.lifecycle_state != from_state:
            return False
        media.lifecycle_state = to_state
        if extra_updates:
            for key, value in extra_updates.items():
                setattr(media, key, value)
        return True

    async def update_state_unscoped(
        self,
        session: Any,
        media_id: Any,
        from_state: str,
        to_state: str,
        *,
        extra_updates: dict[str, Any] | None = None,
    ) -> bool:
        media = self.records.get(uuid.UUID(str(media_id)))
        if media is None or media.lifecycle_state != from_state:
            return False
        media.lifecycle_state = to_state
        if extra_updates:
            for key, value in extra_updates.items():
                setattr(media, key, value)
        return True

    async def get_by_object_key_unscoped(
        self, session: Any, object_key: str
    ) -> MediaAssetModel | None:
        for media in self.records.values():
            if media.object_key == object_key:
                return media
        return None

    async def list_expired(
        self, session: Any, *, now: datetime, limit: int
    ) -> list[MediaAssetModel]:
        matches = [
            m
            for m in self.records.values()
            if m.lifecycle_state in ("available", "deletion_pending")
            and m.expires_at is not None
            and m.expires_at <= now
        ]
        return matches[:limit]

    async def list_stale_uploads(
        self, session: Any, *, older_than: datetime, limit: int
    ) -> list[MediaAssetModel]:
        matches = [
            m
            for m in self.records.values()
            if m.lifecycle_state == "uploading" and m.created_at < older_than
        ]
        return matches[:limit]

    async def list_for_reconciliation(
        self,
        session: Any,
        *,
        states: tuple[str, ...],
        older_than: datetime,
        limit: int,
    ) -> list[MediaAssetModel]:
        matches = [
            m
            for m in self.records.values()
            if m.lifecycle_state in states and m.updated_at < older_than
        ]
        return matches[:limit]

    async def list_tenant_venue_pairs(
        self, session: Any, *, limit: int = 1000
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        pairs = {(m.tenant_id, m.venue_id) for m in self.records.values()}
        return list(pairs)[:limit]


class FakeRecord:
    """In-memory stand-in for IdempotencyRecordModel."""

    def __init__(
        self,
        *,
        tenant_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
        actor_id: uuid.UUID | None,
        venue_id: uuid.UUID | None,
        claimed_by: str,
        claimed_until: datetime,
    ) -> None:
        self.idempotency_id = uuid.uuid4()
        self.tenant_id = tenant_id
        self.operation = operation
        self.idempotency_key = key
        self.request_hash = request_hash
        self.actor_id = actor_id
        self.venue_id = venue_id
        self.status = "in_progress"
        self.result: dict | None = None
        self.claimed_by = claimed_by
        self.claimed_until = claimed_until
        self.completed_at: datetime | None = None


class FakeIdempotencyRepository:
    """In-memory idempotency repository with unique-key semantics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.records: dict[tuple[uuid.UUID, str, str], FakeRecord] = {}
        self.calls: list[tuple[str, dict]] = []
        self._counter = itertools.count(1)

    def _key(self, tenant_id: uuid.UUID, operation: str, key: str) -> tuple[uuid.UUID, str, str]:
        return (tenant_id, operation, key)

    async def get(
        self,
        *,
        tenant_id: uuid.UUID,
        operation: str,
        key: str,
    ) -> FakeRecord | None:
        self.calls.append(("get", {"tenant_id": tenant_id, "operation": operation, "key": key}))
        return self.records.get(self._key(tenant_id, operation, key))

    async def create_claim(
        self,
        *,
        tenant_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
        actor_id: uuid.UUID | None,
        venue_id: uuid.UUID | None,
        claimed_by: str,
        lease_seconds: int,
        now: datetime,
    ) -> FakeRecord | None:
        self.calls.append((
            "create_claim",
            {"tenant_id": tenant_id, "operation": operation, "key": key},
        ))
        async with self._lock:
            # Emulate the DB unique index: block while an in-progress
            # claim exists; when it completes we lose the race.
            while True:
                existing = self.records.get(self._key(tenant_id, operation, key))
                if existing is None:
                    break
                if existing.status == "completed":
                    return None
                await asyncio.sleep(0.001)
            record = FakeRecord(
                tenant_id=tenant_id,
                operation=operation,
                key=key,
                request_hash=request_hash,
                actor_id=actor_id,
                venue_id=venue_id,
                claimed_by=claimed_by,
                claimed_until=now + timedelta(seconds=lease_seconds),
            )
            self.records[self._key(tenant_id, operation, key)] = record
            return record

    async def reclaim(
        self,
        *,
        idempotency_id: uuid.UUID,
        request_hash: str,
        actor_id: uuid.UUID | None,
        venue_id: uuid.UUID | None,
        claimed_by: str,
        lease_seconds: int,
        now: datetime,
    ) -> bool:
        self.calls.append(("reclaim", {"idempotency_id": idempotency_id}))
        for record in self.records.values():
            if record.idempotency_id == idempotency_id:
                if record.status != "in_progress" or record.claimed_until > now:
                    return False
                record.request_hash = request_hash
                record.actor_id = actor_id
                record.venue_id = venue_id
                record.claimed_by = claimed_by
                record.claimed_until = now + timedelta(seconds=lease_seconds)
                return True
        return False

    async def complete(
        self,
        *,
        idempotency_id: uuid.UUID,
        claimed_by: str,
        result: dict,
        now: datetime,
    ) -> bool:
        self.calls.append(("complete", {"idempotency_id": idempotency_id}))
        for record in self.records.values():
            if record.idempotency_id == idempotency_id:
                if record.status != "in_progress" or record.claimed_by != claimed_by:
                    return False
                record.status = "completed"
                record.result = result
                record.claimed_by = None
                record.claimed_until = None
                record.completed_at = now
                return True
        return False

    async def seed_in_progress(
        self,
        *,
        tenant_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
        claimed_until: datetime,
        venue_id: uuid.UUID | None = None,
    ) -> FakeRecord:
        """Pre-seed an in_progress record (simulates a committed claim)."""
        record = FakeRecord(
            tenant_id=tenant_id,
            operation=operation,
            key=key,
            request_hash=request_hash,
            actor_id=None,
            venue_id=venue_id,
            claimed_by="holder",
            claimed_until=claimed_until,
        )
        self.records[self._key(tenant_id, operation, key)] = record
        return record


def make_actor(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    venue_scope: frozenset[uuid.UUID] = frozenset(),
) -> ActorContext:
    """A minimal server-built ActorContext for reliability tests."""
    return ActorContext(
        actor_id=user_id or uuid.uuid4(),
        tenant_id=tenant_id,
        role_name=RoleName.ADMIN,
        permissions=frozenset(Permission),
        venue_scope=venue_scope,
        authenticated_at=utc_now(),
        active=True,
    )


# =============================================================================
# Task 17.5 — deterministic fake evidence extraction
# =============================================================================


@dataclass(frozen=True)
class FakeMedia:
    """One in-memory source recording the fake extractor can 'extract'.

    ``state`` is a deterministic content marker: ``valid`` (decodable),
    ``corrupt`` (undecodable bytes), ``missing`` (deleted after
    resolution). ``actual_start``/``actual_end`` are the byte-level
    coverage of the recording (may be narrower than the request →
    truncated/PARTIAL).
    """

    media_path: str
    duration_seconds: float
    size_bytes: int
    actual_start: datetime
    actual_end: datetime
    media_format: str = "mp4"
    start_frame: int | None = None
    end_frame: int | None = None
    state: str = "valid"


class FakeMediaStore:
    """In-memory media store keyed by (asset_id, session_id)."""

    def __init__(self) -> None:
        self._media: dict[tuple[VideoAssetId, VideoSessionId | None], FakeMedia] = {}

    def put(
        self,
        media: FakeMedia,
        *,
        asset_id: VideoAssetId,
        session_id: VideoSessionId | None = None,
    ) -> None:
        self._media[asset_id, session_id] = media

    def get(
        self,
        asset_id: VideoAssetId,
        session_id: VideoSessionId | None = None,
    ) -> FakeMedia | None:
        return self._media.get((asset_id, session_id))


class FakeEvidenceExtractor:
    """Deterministic in-memory EvidenceExtractor (Task 17.5).

    Implements the ``EvidenceExtractor`` port with an in-memory media
    store. Tracks open/closed 'handles' so tests can verify that NO
    resource leaks on any path — success, failure, corruption,
    cancellation, or an ``asyncio.CancelledError`` landing on the
    cooperative sleep point. ``latency`` simulates work for cancellation
    tests.
    """

    def __init__(self, store: FakeMediaStore, *, latency: float = 0.0) -> None:
        self._store = store
        self._latency = latency
        self.open_handles = 0
        self.closed_handles = 0

    @property
    def handles_open(self) -> int:
        return self.open_handles - self.closed_handles

    async def extract(
        self,
        evidence: EvidenceRef,
        segment: ResolvedSourceSegment,
        *,
        cancellation: ExtractionCancellationToken | None = None,
    ) -> ExtractedEvidence:
        if segment.requested_end < segment.requested_start:
            return self._result(
                segment, ExtractionStatus.EXTRACTION_FAILED, reason="invalid time range"
            )
        if cancellation is not None and cancellation.is_cancelled:
            return self._result(
                segment, ExtractionStatus.CANCELLED, reason="cancelled before extraction"
            )
        if segment.status in (
            SourceResolutionStatus.SOURCE_NOT_FOUND,
            SourceResolutionStatus.AUTHORIZATION_FAILURE,
        ):
            return self._result(
                segment,
                ExtractionStatus.SOURCE_NOT_FOUND,
                reason=segment.reason or segment.status.value,
            )
        if not segment.segments:
            return self._result(
                segment,
                ExtractionStatus.SOURCE_NOT_FOUND,
                reason="no resolved source segment",
            )

        if segment.status is SourceResolutionStatus.PARTIAL_COVERAGE:
            requested_start = segment.segments[0].start_time
            requested_end = segment.segments[-1].end_time
        else:
            requested_start = segment.requested_start
            requested_end = segment.requested_end

        first = segment.segments[0]
        media = self._store.get(first.asset_id, first.session_id)
        self._open()
        try:
            if self._latency > 0:
                await asyncio.sleep(self._latency)  # cooperative cancellation point
            if cancellation is not None and cancellation.is_cancelled:
                return self._result(
                    segment, ExtractionStatus.CANCELLED, reason="cancelled during extraction"
                )
            if media is None or media.state == "missing":
                return self._result(
                    segment,
                    ExtractionStatus.SOURCE_NOT_FOUND,
                    reason="source missing at extraction time",
                )
            if media.state == "corrupt":
                return self._result(
                    segment,
                    ExtractionStatus.CORRUPT_SOURCE,
                    reason="source bytes could not be decoded",
                )
            if media.duration_seconds == 0:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="empty interval — source contains no extractable media",
                )

            actual_start = max(requested_start, media.actual_start)
            actual_end = min(requested_end, media.actual_end)
            if actual_end < actual_start:
                return self._result(
                    segment,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="no extractable media within the requested interval",
                )
            partial = (
                segment.status is SourceResolutionStatus.PARTIAL_COVERAGE
                or actual_start > requested_start
                or actual_end < requested_end
            )
            return self._result(
                segment,
                ExtractionStatus.PARTIAL if partial else ExtractionStatus.SUCCESS,
                actual_start=actual_start,
                actual_end=actual_end,
                media=media,
            )
        finally:
            self._close()

    def _open(self) -> None:
        self.open_handles += 1

    def _close(self) -> None:
        self.closed_handles += 1

    def _result(
        self,
        segment: ResolvedSourceSegment,
        status: ExtractionStatus,
        *,
        reason: str | None = None,
        actual_start: datetime | None = None,
        actual_end: datetime | None = None,
        media: FakeMedia | None = None,
    ) -> ExtractedEvidence:
        extraction_id = MediaId(
            uuid.uuid5(
                TEMPORAL_ID_NAMESPACE,
                (
                    f"evidence_extraction|{segment.evidence_ref_id}|"
                    f"{segment.segments[0].asset_id if segment.segments else ''}|"
                    f"{segment.requested_start.isoformat()}|{segment.requested_end.isoformat()}"
                ),
            )
        )
        return ExtractedEvidence(
            extraction_id=extraction_id,
            status=status,
            evidence_ref_id=segment.evidence_ref_id,
            event_id=segment.event_id,
            tenant_id=segment.tenant_id,
            venue_id=segment.venue_id,
            session_id=segment.video_session_id,
            camera_id=segment.camera_id,
            configuration_version_id=segment.configuration_version_id,
            rule_id=segment.rule_id,
            rule_version=segment.rule_version,
            requested_start=segment.requested_start,
            requested_end=segment.requested_end,
            actual_start_time=actual_start,
            actual_end_time=actual_end,
            start_frame=media.start_frame if media is not None else None,
            end_frame=media.end_frame if media is not None else None,
            media_path=media.media_path if media is not None else None,
            media_format=media.media_format if media is not None else None,
            duration_seconds=media.duration_seconds if media is not None else None,
            size_bytes=media.size_bytes if media is not None else None,
            metadata={"source_state": media.state} if media is not None else {},
            reason=reason,
        )
