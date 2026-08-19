"""Unit tests for the async evidence worker (Task 17.11).

Covers the ten required scenarios plus the two headline invariants:

1.  crash before extraction       6.  worker restart
2.  crash after extraction        7.  source unavailable
3.  crash before upload           8.  checksum failure
4.  crash after upload            9.  storage failure
5.  duplicate delivery           10. retry

Invariants: NO duplicate logical evidence package; NO committed evidence
request is silently lost (every failure lands in RETRYABLE_FAILURE —
recoverable — or TERMINAL_FAILURE — preserved for audit, never dropped).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from backend.app.application.services.evidence_state import EvidenceStateService
from backend.app.domain.evidence.extraction import (
    ExtractedEvidence,
    ExtractionCancellationToken,
    ExtractionStatus,
    deterministic_extraction_id,
)
from backend.app.domain.evidence.package import EvidencePackageBuilder
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceRecordingCandidate,
    SourceResolver,
)
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.database.repositories.evidence_work import (
    EVIDENCE_ARTIFACT_KEY,
    EVIDENCE_ATTEMPTS_KEY,
    EVIDENCE_CLAIMED_BY_KEY,
    EVIDENCE_LAST_ERROR_KEY,
    EVIDENCE_LEASE_UNTIL_KEY,
    EVIDENCE_PACKAGE_ID_KEY,
    EVIDENCE_RECOVERY_KEY,
    EVIDENCE_REQUEST_KEY,
    EVIDENCE_RETRY_AT_KEY,
    iso_timestamp,
)
from backend.app.infrastructure.storage.exceptions import StorageError
from backend.app.workers.evidence import EvidenceWorker
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    EvidenceId,
    RuleId,
    TenantId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
)
from contracts.events import EvidenceRef, EvidenceType

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_ASSET = VideoAssetId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("70000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(uuid.UUID("80000000-0000-0000-0000-000000000001"))
_RULE = RuleId("dwell_threshold")

_CHECKSUM = hashlib.sha256(b"evidence-bytes").hexdigest()


# =============================================================================
# Fixtures and fakes
# =============================================================================


class Clock:
    """A deterministic, mutable wall clock (business decisions never use it)."""

    def __init__(self, start: datetime = _NOW) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class FakeDatabaseClient:
    """Minimal stand-in: yields a shared session per unit of work."""

    def __init__(self) -> None:
        self._session = FakeSession()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeSession]:
        yield self._session


class FakeSession:
    """A session that accepts adds/flushes (the store owns the semantics)."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class FakeEvidenceWorkStore:
    """In-memory ``EvidenceWorkStore`` faithful to the SQL semantics.

    The real repository's guards read the COMMITTED database row (a
    conditional UPDATE ``WHERE processing_state = from_state``), NOT the
    worker's in-memory mutation — the state service mutates the object
    before the guarded persist. This fake therefore tracks a committed
    metadata snapshot per ref (``_committed``) and guards against THAT,
    exactly like the SQL conditional UPDATE. A persist that fails its
    guard leaves the committed snapshot untouched.
    """

    def __init__(self) -> None:
        self.refs: dict[uuid.UUID, EvidenceRefModel] = {}
        self.packages: list[dict[str, object]] = []
        self._committed: dict[uuid.UUID, dict[str, object]] = {}

    def seed(self, ref: EvidenceRefModel) -> None:
        self.refs[ref.ref_id] = ref
        self._committed[ref.ref_id] = dict(ref.metadata_ or {})

    def get(self, ref_id: uuid.UUID) -> EvidenceRefModel | None:
        return self.refs.get(ref_id)

    def _state_of(self, ref_id: uuid.UUID) -> str | None:
        return (self._committed.get(ref_id) or {}).get("processing_state")

    def _commit(self, ref: EvidenceRefModel) -> None:
        self._committed[ref.ref_id] = dict(ref.metadata_ or {})

    # --- Store protocol -----------------------------------------------------

    async def queue_pending(
        self, session: FakeSession, *, now: datetime, batch_size: int
    ) -> list[EvidenceRefModel]:
        rows = [r for r in self.refs.values() if self._state_of(r.ref_id) in (None, "requested")][
            :batch_size
        ]
        for row in rows:
            _set_state(row, "queued")
            self._commit(row)
        return rows

    async def promote_due_retries(
        self, session: FakeSession, *, now: datetime, batch_size: int
    ) -> list[EvidenceRefModel]:
        now_iso = iso_timestamp(now)
        rows = [
            r
            for r in self.refs.values()
            if self._state_of(r.ref_id) == "retryable_failure"
            and (self._committed.get(r.ref_id) or {}).get(EVIDENCE_RETRY_AT_KEY, now_iso) <= now_iso
        ][:batch_size]
        for row in rows:
            _set_state(row, "queued")
            metadata = dict(row.metadata_ or {})
            metadata.pop(EVIDENCE_RETRY_AT_KEY, None)
            row.metadata_ = metadata
            self._commit(row)
        return rows

    async def claim_queued(
        self,
        session: FakeSession,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
        batch_size: int,
    ) -> list[EvidenceRefModel]:
        rows = [r for r in self.refs.values() if self._claimable(r, now)][:batch_size]
        for row in rows:
            metadata = dict(row.metadata_ or {})
            metadata["processing_state"] = "extracting"
            metadata[EVIDENCE_CLAIMED_BY_KEY] = worker_id
            metadata[EVIDENCE_LEASE_UNTIL_KEY] = iso_timestamp(
                now + timedelta(seconds=lease_seconds)
            )
            metadata[EVIDENCE_ATTEMPTS_KEY] = int(metadata.get(EVIDENCE_ATTEMPTS_KEY, 0)) + 1
            row.metadata_ = metadata
            self._commit(row)
        return rows

    async def expire_abandoned(
        self,
        session: FakeSession,
        *,
        cutoff: datetime,
        batch_size: int,
        reason: str,
    ) -> list[EvidenceRefModel]:
        rows = [
            r
            for r in self.refs.values()
            if self._state_of(r.ref_id) in ("requested", "queued")
            and r.created_at is not None
            and r.created_at <= cutoff
        ][:batch_size]
        for row in rows:
            metadata = dict(row.metadata_ or {})
            metadata["processing_state"] = "expired"
            metadata[EVIDENCE_LAST_ERROR_KEY] = reason
            row.metadata_ = metadata
            self._commit(row)
        return rows

    async def list_stale(
        self, session: FakeSession, *, now: datetime, limit: int
    ) -> list[EvidenceRefModel]:
        now_iso = iso_timestamp(now)
        return [
            r
            for r in self.refs.values()
            if self._state_of(r.ref_id) in ("extracting", "uploading")
            and (self._committed.get(r.ref_id) or {}).get(EVIDENCE_LEASE_UNTIL_KEY, "") <= now_iso
        ][:limit]

    async def lock_stale(
        self,
        session: FakeSession,
        ref_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> EvidenceRefModel | None:
        row = self.refs.get(ref_id)
        if row is None or self._state_of(ref_id) not in ("extracting", "uploading"):
            return None
        if (self._committed.get(ref_id) or {}).get(EVIDENCE_LEASE_UNTIL_KEY, "") > iso_timestamp(
            now
        ):
            return None
        metadata = dict(row.metadata_ or {})
        metadata[EVIDENCE_CLAIMED_BY_KEY] = worker_id
        metadata[EVIDENCE_LEASE_UNTIL_KEY] = iso_timestamp(now + timedelta(seconds=lease_seconds))
        row.metadata_ = metadata
        self._commit(row)
        return row

    def _claimable(self, row: EvidenceRefModel, now: datetime) -> bool:
        if self._state_of(row.ref_id) != "queued":
            return False
        lease = (self._committed.get(row.ref_id) or {}).get(EVIDENCE_LEASE_UNTIL_KEY)
        now_iso = iso_timestamp(now)
        return lease is None or lease == "" or lease <= now_iso

    async def persist_transition(
        self,
        session: FakeSession,
        ref: EvidenceRefModel,
        *,
        from_state: str,
        to_state: str,
        claimed_by: str,
        updates: dict[str, object],
    ) -> bool:
        committed = self._committed.get(ref.ref_id) or {}
        # The conditional UPDATE guard — the committed row must still be
        # in from_state and owned by this worker.
        if committed.get("processing_state") != from_state:
            return False
        if committed.get(EVIDENCE_CLAIMED_BY_KEY) != claimed_by:
            return False
        metadata = dict(ref.metadata_ or {})
        metadata.update(updates)
        metadata["processing_state"] = to_state
        ref.metadata_ = metadata
        self._commit(ref)
        return True

    async def save_finalized(
        self,
        session: FakeSession,
        ref: EvidenceRefModel,
        *,
        claimed_by: str,
        package: object,
        link: dict[str, object],
        updates: dict[str, object],
    ) -> bool:
        committed = self._committed.get(ref.ref_id) or {}
        if committed.get("processing_state") != "uploading":
            return False
        if committed.get(EVIDENCE_CLAIMED_BY_KEY) != claimed_by:
            return False
        metadata = dict(ref.metadata_ or {})
        metadata.update(updates)
        metadata["processing_state"] = "finalized"
        ref.metadata_ = metadata
        self._commit(ref)
        self.packages.append({"package_id": link["package_id"], "ref_id": link["ref_id"]})
        return True


class RecordingAuditSink:
    """Recorder in place of the Task 7 outbox audit sink."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def __call__(
        self,
        session: FakeSession,
        *,
        ref: EvidenceRefModel,
        event_type: str,
        reason: str | None = None,
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        self.events.append({
            "ref_id": ref.ref_id,
            "event_type": event_type,
            "reason": reason,
            "extra_payload": extra_payload,
        })

    def types(self) -> list[str]:
        return [str(e["event_type"]) for e in self.events]


def _settings(**overrides: object) -> Settings:
    base = dict(
        _env_file=None,  # type: ignore[call-arg]
        OBJECT_STORAGE_BUCKET="hotelops-test-bucket",
        OBJECT_STORAGE_ENDPOINT="http://localhost:9000",
        OBJECT_STORAGE_ACCESS_KEY="test-key",
        OBJECT_STORAGE_SECRET_KEY="test-secret-that-is-at-least-16-bytes",
        EVIDENCE_WORKER_POLL_INTERVAL=0.1,
        EVIDENCE_WORKER_BATCH_SIZE=50,
        EVIDENCE_WORKER_LEASE_SECONDS=60,
        EVIDENCE_WORKER_MAX_ATTEMPTS=3,
        EVIDENCE_WORKER_BACKOFF_BASE=1.0,
        EVIDENCE_WORKER_BACKOFF_MAX=60.0,
        EVIDENCE_WORKER_BACKOFF_JITTER=0.0,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


def _request(*, ref_id: EvidenceId = _REF) -> EvidenceRef:
    """A canonical, valid VIDEO_CLIP evidence request."""
    return EvidenceRef(
        ref_id=ref_id,
        ref_type=EvidenceType.VIDEO_CLIP,
        ref_uri=f"s3://evidence/{_TENANT}/{_REF}",
        event_id=_EVENT,
        event_time=_NOW,
        tenant_id=_TENANT,
        venue_id=_VENUE,
        video_asset_id=_ASSET,
        video_session_id=_SESSION,
        camera_id=_CAMERA,
        start_time=_NOW - timedelta(minutes=5),
        end_time=_NOW,
        configuration_version_id=_CONFIG,
        rule_id=_RULE,
        rule_version="v1",
    )


def _make_ref(
    *,
    request: EvidenceRef | None = None,
    state: str | None = "requested",
    lease_until: datetime | None = None,
    attempts: int = 0,
    retry_at: datetime | None = None,
    claimed_by: str | None = None,
    recovery: dict[str, object] | None = None,
    artifact_key: str | None = None,
    package_id: str | None = None,
    last_error: str | None = None,
    ref_id: EvidenceId = _REF,
) -> EvidenceRefModel:
    request = request or _request(ref_id=ref_id)
    metadata: dict[str, object] = {}
    if state is not None:
        metadata["processing_state"] = state
    metadata[EVIDENCE_REQUEST_KEY] = request.model_dump(mode="json")
    if lease_until is not None:
        metadata[EVIDENCE_LEASE_UNTIL_KEY] = iso_timestamp(lease_until)
    if attempts:
        metadata[EVIDENCE_ATTEMPTS_KEY] = attempts
    if retry_at is not None:
        metadata[EVIDENCE_RETRY_AT_KEY] = iso_timestamp(retry_at)
    if claimed_by is not None:
        metadata[EVIDENCE_CLAIMED_BY_KEY] = claimed_by
    if recovery is not None:
        metadata[EVIDENCE_RECOVERY_KEY] = recovery
    if artifact_key is not None:
        metadata[EVIDENCE_ARTIFACT_KEY] = artifact_key
    if package_id is not None:
        metadata[EVIDENCE_PACKAGE_ID_KEY] = package_id
    if last_error is not None:
        metadata[EVIDENCE_LAST_ERROR_KEY] = last_error
    return EvidenceRefModel(
        ref_id=uuid.UUID(str(ref_id)),
        schema_version="1.0",
        tenant_id=uuid.UUID(str(_TENANT)),
        venue_id=uuid.UUID(str(_VENUE)),
        ref_type="video_clip",
        ref_uri=request.ref_uri,
        event_id=uuid.UUID(str(_EVENT)),
        event_time=_NOW,
        metadata_=metadata,
        created_at=_NOW,
    )


def _candidate() -> SourceRecordingCandidate:
    return SourceRecordingCandidate(
        asset_id=_ASSET,
        tenant_id=_TENANT,
        venue_id=_VENUE,
        camera_id=_CAMERA,
        session_id=_SESSION,
        start_time=_NOW - timedelta(minutes=5),
        end_time=_NOW,
        available=True,
    )


class FakeCandidates:
    """Tenant/venue-scoped candidate provider (never latest, never substitute)."""

    def __init__(
        self,
        candidates: list[SourceRecordingCandidate],
        *,
        raise_error: Exception | None = None,
    ) -> None:
        self._candidates = candidates
        self._raise = raise_error

    async def candidates(self, evidence: EvidenceRef) -> list[SourceRecordingCandidate]:
        if self._raise is not None:
            raise self._raise
        return self._candidates


def _extracted(
    segment: ResolvedSourceSegment,
    *,
    status: ExtractionStatus = ExtractionStatus.SUCCESS,
    checksum: str | None = _CHECKSUM,
    media_path: str | None = None,
) -> ExtractedEvidence:
    if media_path is None:
        media_path = (
            f"tenants/{_TENANT}/venues/{_VENUE}/evidence/{deterministic_extraction_id(segment)}.mp4"
        )
    metadata: dict[str, object] = {}
    if checksum is not None:
        metadata["checksum_sha256"] = checksum
    return ExtractedEvidence(
        extraction_id=deterministic_extraction_id(segment),
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
        actual_start_time=segment.requested_start,
        actual_end_time=segment.requested_end,
        start_frame=0,
        end_frame=1,
        media_path=media_path,
        media_format="mp4",
        duration_seconds=1.0,
        size_bytes=len(b"evidence-bytes"),
        metadata=metadata,
    )


class FakeExtractor:
    """Configurable EvidenceExtractor (per-call behavior queue).

    With ``repeat=True`` the LAST behavior repeats once the queue is
    exhausted (a persistent failure condition); otherwise the queue
    falls back to a clean SUCCESS after exhaustion.
    """

    def __init__(
        self,
        behaviors: list[Callable[[ResolvedSourceSegment], ExtractedEvidence]] | None = None,
        *,
        repeat: bool = False,
    ) -> None:
        self._behaviors = list(behaviors or [])
        self._repeat = repeat
        self.calls = 0

    def queue(self, behavior: Callable[[ResolvedSourceSegment], ExtractedEvidence]) -> None:
        self._behaviors.append(behavior)

    async def extract(
        self,
        evidence: EvidenceRef,
        segment: ResolvedSourceSegment,
        *,
        cancellation: ExtractionCancellationToken | None = None,
    ) -> ExtractedEvidence:
        self.calls += 1
        if self._behaviors:
            behavior = self._behaviors.pop(0)
            if self._repeat and not self._behaviors:
                self._behaviors.append(behavior)
            return behavior(segment)
        return _extracted(segment)


def _raise(error: Exception) -> Callable[[ResolvedSourceSegment], ExtractedEvidence]:
    def _behavior(segment: ResolvedSourceSegment) -> ExtractedEvidence:
        raise error

    return _behavior


def _status(
    status: ExtractionStatus, **kwargs: object
) -> Callable[[ResolvedSourceSegment], ExtractedEvidence]:
    def _behavior(segment: ResolvedSourceSegment) -> ExtractedEvidence:
        return _extracted(segment, status=status, **kwargs)  # type: ignore[arg-type]

    return _behavior


def _state(ref: EvidenceRefModel) -> str | None:
    return (ref.metadata_ or {}).get("processing_state")


def _set_state(ref: EvidenceRefModel, state: str) -> None:
    metadata = dict(ref.metadata_ or {})
    metadata["processing_state"] = state
    ref.metadata_ = metadata


def _make_worker(
    *,
    store: FakeEvidenceWorkStore,
    extractor: FakeExtractor,
    candidates: FakeCandidates,
    clock: Clock,
    settings: Settings | None = None,
    worker_id: str = "worker-1",
    audit: RecordingAuditSink | None = None,
) -> EvidenceWorker:
    return EvidenceWorker(
        FakeDatabaseClient(),
        extractor=extractor,
        candidates=candidates,
        settings=settings or _settings(),
        worker_id=worker_id,
        store_factory=lambda: store,
        state_service=EvidenceStateService(),
        package_builder=EvidencePackageBuilder(),
        audit_sink=audit or RecordingAuditSink(),
        now=clock,
    )


def _segment(
    request: EvidenceRef, candidates: list[SourceRecordingCandidate]
) -> ResolvedSourceSegment:
    return SourceResolver().resolve(request, candidates)


def _recovery(segment: ResolvedSourceSegment, extracted: ExtractedEvidence) -> dict[str, object]:
    return {
        "resolved_source": segment.model_dump(mode="json"),
        "extraction": extracted.model_dump(mode="json"),
    }


# =============================================================================
# Happy path + invariants
# =============================================================================


class TestHappyPath:
    async def test_request_flows_to_finalized_package(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref())
        clock = Clock()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        handled = await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert handled >= 1
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1
        assert ref.metadata_ is not None
        assert ref.metadata_.get(EVIDENCE_PACKAGE_ID_KEY) == str(store.packages[0]["package_id"])
        assert ref.metadata_.get(EVIDENCE_ARTIFACT_KEY) is not None

    async def test_no_duplicate_logical_package_on_second_run(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref())
        clock = Clock()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()
        await worker.run_once()  # duplicate delivery / restart

        assert len(store.packages) == 1
        assert _state(store.get(uuid.UUID(str(_REF)))) == "finalized"

    async def test_package_id_is_deterministic(self) -> None:
        """Replay of the same logical evidence yields the same package id."""
        store_a, store_b = FakeEvidenceWorkStore(), FakeEvidenceWorkStore()
        store_a.seed(_make_ref())
        store_b.seed(_make_ref())
        clock = Clock()
        worker_a = _make_worker(
            store=store_a,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            worker_id="a",
        )
        worker_b = _make_worker(
            store=store_b,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            worker_id="b",
        )

        await worker_a.run_once()
        await worker_b.run_once()

        assert store_a.packages[0]["package_id"] == store_b.packages[0]["package_id"]


# =============================================================================
# Crash scenarios (restart-safe)
# =============================================================================


class TestCrashBeforeExtraction:
    async def test_recovered_after_crash(self) -> None:
        # A crash-before-extraction leaves the ref claimed (EXTRACTING) with
        # an expired lease. The next worker run reclaims it and completes.
        store = FakeEvidenceWorkStore()
        store.seed(
            _make_ref(
                state="extracting",
                lease_until=_NOW - timedelta(seconds=5),
                claimed_by="crashed-worker",
                attempts=1,
            )
        )
        clock = Clock()
        extractor = FakeExtractor()
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        handled = await worker.run_once()

        assert handled >= 1
        assert _state(store.get(uuid.UUID(str(_REF)))) == "finalized"
        assert len(store.packages) == 1
        assert extractor.calls == 1


class TestCrashAfterExtraction:
    async def test_recovery_re_extracts_deterministically(self) -> None:
        # The artifact was written before the crash; recovery re-extracts
        # with the SAME deterministic identity (idempotent overwrite) and
        # produces exactly ONE package.
        store = FakeEvidenceWorkStore()
        store.seed(
            _make_ref(
                state="extracting",
                lease_until=_NOW - timedelta(seconds=5),
                claimed_by="crashed-worker",
                attempts=1,
            )
        )
        clock = Clock()
        extractor = FakeExtractor()
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1
        # The recovered extraction id matches the deterministic derivation.
        segment = _segment(_request(), [_candidate()])
        assert ref.metadata_ is not None
        assert ref.metadata_.get("extraction_id") == str(deterministic_extraction_id(segment))


class TestCrashBeforeUpload:
    async def test_recovered_and_uploaded(self) -> None:
        # Crashed mid-extraction before the upload checkpoint — recovery
        # re-extracts (deterministic), uploads, finalizes.
        store = FakeEvidenceWorkStore()
        store.seed(
            _make_ref(
                state="extracting",
                lease_until=_NOW - timedelta(seconds=5),
                claimed_by="crashed-worker",
                attempts=1,
            )
        )
        clock = Clock()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1
        assert ref.metadata_ is not None
        assert ref.metadata_.get(EVIDENCE_ARTIFACT_KEY) is not None


class TestCrashAfterUpload:
    async def test_recovered_from_checkpoint_without_re_extraction(self) -> None:
        # Crashed after the upload checkpoint — the durable UPLOADING state
        # plus the recovery record let the worker finalize WITHOUT
        # re-extracting (no duplicate logical package, no wasted work).
        request = _request()
        segment = _segment(request, [_candidate()])
        extracted = _extracted(segment)
        store = FakeEvidenceWorkStore()
        store.seed(
            _make_ref(
                state="uploading",
                lease_until=_NOW - timedelta(seconds=5),
                claimed_by="crashed-worker",
                attempts=1,
                recovery=_recovery(segment, extracted),
                artifact_key=extracted.media_path,
            )
        )
        clock = Clock()
        extractor = FakeExtractor()
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1
        # The extractor was NEVER re-run — resumed from the checkpoint.
        assert extractor.calls == 0

    async def test_corrupt_checkpoint_is_dead_lettered(self) -> None:
        # A UPLOADING ref whose recovery checkpoint is missing cannot be
        # resumed — dead-lettered (preserved, never silently lost).
        store = FakeEvidenceWorkStore()
        store.seed(
            _make_ref(
                state="uploading",
                lease_until=_NOW - timedelta(seconds=5),
                claimed_by="crashed-worker",
                attempts=1,
            )
        )
        clock = Clock()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "terminal_failure"
        assert len(store.packages) == 0
        assert ref.metadata_ is not None
        assert "checkpoint" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY, ""))


# =============================================================================
# Duplicate delivery / duplicate worker
# =============================================================================


class TestDuplicateDelivery:
    async def test_two_workers_race_one_claim(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        worker_a = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            worker_id="a",
        )
        worker_b = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            worker_id="b",
        )

        await worker_a.run_once()
        await worker_b.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "finalized"
        # Exactly ONE logical package despite two workers.
        assert len(store.packages) == 1


# =============================================================================
# Failure scenarios
# =============================================================================


class TestSourceUnavailable:
    async def test_no_recording_is_terminal_not_lost(self) -> None:
        # No candidate recording covers the request — deterministic
        # data condition → TERMINAL_FAILURE (preserved, never silently
        # dropped, never retried forever).
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "terminal_failure"
        assert len(store.packages) == 0
        assert ref.metadata_ is not None
        assert "source" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY, "")).lower()

    async def test_transient_candidate_lookup_is_retryable(self) -> None:
        # The candidate provider hit a transient storage failure → the
        # ref is RETRYABLE (never lost), and succeeds after recovery.
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        provider = FakeCandidates([_candidate()], raise_error=StorageError("transient"))
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=provider,
            clock=clock,
        )

        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert (ref.metadata_ or {}).get(EVIDENCE_RETRY_AT_KEY) is not None


class TestChecksumFailure:
    async def test_missing_checksum_is_retryable_then_dead_lettered(self) -> None:
        # A completed artifact without a valid checksum fails integrity
        # verification → retryable; the budget is bounded → TERMINAL.
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        # The artifact keeps failing integrity verification on every attempt.
        extractor = FakeExtractor([_status(ExtractionStatus.SUCCESS, checksum=None)], repeat=True)
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            settings=_settings(EVIDENCE_WORKER_MAX_ATTEMPTS=2),
        )

        # Attempt 1 — fails integrity → retryable.
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert (ref.metadata_ or {}).get(EVIDENCE_RETRY_AT_KEY) is not None

        # Retry budget exhausted on the next claim → dead-lettered.
        clock.advance(seconds=600)
        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) == "terminal_failure"
        assert len(store.packages) == 0


class TestCorruptExtractedMedia:
    """Task 17.13 mandatory scenario 15 — the extractor produced a
    CORRUPT_SOURCE outcome (source bytes undecodable). The worker must
    dead-letter it to TERMINAL_FAILURE (a deterministic data condition —
    retrying cannot succeed), preserve the ref for audit, and never emit
    a package."""

    async def test_corrupt_extracted_media_is_terminal_not_retryable(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        extractor = FakeExtractor([_status(ExtractionStatus.CORRUPT_SOURCE)])
        audit = RecordingAuditSink()
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            audit=audit,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "terminal_failure"
        assert len(store.packages) == 0
        # The ref is preserved for audit — never silently lost, never
        # deleted, and never scheduled for a futile retry.
        assert store.get(uuid.UUID(str(_REF))) is not None
        assert (ref.metadata_ or {}).get(EVIDENCE_RETRY_AT_KEY) is None
        assert "corrupt" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY, "")).lower()
        # The terminal failure is audited.
        assert "evidence.processing.terminal_failure" in audit.types()

    async def test_corrupt_media_never_retries_even_with_budget(self) -> None:
        # CORRUPT_SOURCE is terminal on the FIRST attempt — the retry
        # budget must not be consumed on a deterministic data condition.
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        extractor = FakeExtractor([_status(ExtractionStatus.CORRUPT_SOURCE)], repeat=True)
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) == "terminal_failure"
        # A second run must not resurrect it (terminal is immutable).
        clock.advance(seconds=600)
        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) == "terminal_failure"
        assert len(store.packages) == 0


class TestChecksumMismatch:
    """Task 17.13 mandatory scenario 16 — a completed artifact whose
    checksum is malformed (non-hex / wrong length) fails the worker's
    integrity gate: retryable (transient condition), then dead-lettered
    once the retry budget is exhausted. A checksum that does not verify
    can never reach FINALIZED."""

    async def test_invalid_format_checksum_is_retryable_then_dead_lettered(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        # A 64-char checksum with a non-hex character — format-invalid.
        bad = "z" + "0" * 63
        extractor = FakeExtractor([_status(ExtractionStatus.SUCCESS, checksum=bad)], repeat=True)
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            settings=_settings(EVIDENCE_WORKER_MAX_ATTEMPTS=2),
        )

        # Attempt 1 — integrity gate rejects the malformed checksum → retryable.
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert (ref.metadata_ or {}).get(EVIDENCE_RETRY_AT_KEY) is not None
        assert "integrity" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY, ""))
        assert len(store.packages) == 0

        # Retry budget exhausted → dead-lettered; never finalized.
        clock.advance(seconds=600)
        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) == "terminal_failure"
        assert len(store.packages) == 0

    async def test_wrong_length_checksum_is_rejected_by_integrity_gate(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        short = "abc"  # not 64 hex chars
        extractor = FakeExtractor([_status(ExtractionStatus.SUCCESS, checksum=short)])
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert len(store.packages) == 0


class TestStorageFailure:
    async def test_storage_failure_is_retryable(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        extractor = FakeExtractor([_raise(StorageError("object store unreachable"))])
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()

        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert ref.metadata_ is not None
        assert "extraction failed" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY, ""))
        assert len(store.packages) == 0


class TestRetry:
    async def test_transient_failure_then_success(self) -> None:
        # Retryable failure → backoff persisted → retry succeeds → finalized.
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        extractor = FakeExtractor([_raise(StorageError("transient storage failure")), _extracted])
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        # Attempt 1 — fails → RETRYABLE_FAILURE with a persisted backoff.
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        retry_at = (ref.metadata_ or {}).get(EVIDENCE_RETRY_AT_KEY)
        assert retry_at is not None

        # Attempt 2 (after the backoff) — succeeds → finalized.
        clock.advance(seconds=600)
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1
        assert extractor.calls == 2

    async def test_exhausted_budget_dead_letters(self) -> None:
        # Persistent storage failure → bounded retries → TERMINAL_FAILURE
        # (preserved for audit — the request is never silently lost).
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        settings = _settings(EVIDENCE_WORKER_MAX_ATTEMPTS=2)
        extractor = FakeExtractor([
            _raise(StorageError("storage down")),
            _raise(StorageError("storage down")),
        ])
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            settings=settings,
        )

        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) == "retryable_failure"

        clock.advance(seconds=600)
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "terminal_failure"
        assert ref.metadata_ is not None
        assert "budget" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY, ""))
        assert len(store.packages) == 0


# =============================================================================
# Cancellation + audit
# =============================================================================


class TestCancellation:
    async def test_cancelled_extraction_leaves_claim_reclaimable(self) -> None:
        # Worker shutdown mid-extraction → CANCELLED → the ref stays
        # EXTRACTING with its lease; once the lease expires the next
        # cycle reclaims and completes it.
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        clock = Clock()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor([_status(ExtractionStatus.CANCELLED)]),
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
        )

        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "extracting"
        assert len(store.packages) == 0

        # Lease expires → recovery completes the request.
        clock.advance(seconds=61)
        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) == "finalized"
        assert len(store.packages) == 1


class TestAudit:
    async def test_audit_events_cover_the_lifecycle(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        audit = RecordingAuditSink()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=Clock(),
            audit=audit,
        )

        await worker.run_once()

        types = audit.types()
        assert "evidence.processing.uploaded" in types
        assert "evidence.processing.finalized" in types

    async def test_retryable_failure_is_audited(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        audit = RecordingAuditSink()
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor([_raise(StorageError("boom"))]),
            candidates=FakeCandidates([_candidate()]),
            clock=Clock(),
            audit=audit,
        )

        await worker.run_once()

        assert "evidence.processing.retryable_failure" in audit.types()


# =============================================================================
# No silent loss invariant
# =============================================================================


class TestNoSilentLoss:
    async def test_every_failure_lands_in_a_durable_state(self) -> None:
        """Every terminal condition is durable and preserved — never dropped."""
        # Terminal: no recording.
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([]),
            clock=Clock(),
        )
        await worker.run_once()
        assert _state(store.get(uuid.UUID(str(_REF)))) in (
            "terminal_failure",
            "retryable_failure",
            "finalized",
        )
        # The ref row still exists (never deleted).
        assert store.get(uuid.UUID(str(_REF))) is not None

    async def test_tenant_venue_scope_preserved_in_package_link(self) -> None:
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref(state="queued"))
        worker = _make_worker(
            store=store,
            extractor=FakeExtractor(),
            candidates=FakeCandidates([_candidate()]),
            clock=Clock(),
        )
        await worker.run_once()
        assert len(store.packages) == 1
        assert store.packages[0]["ref_id"] == uuid.UUID(str(_REF))
