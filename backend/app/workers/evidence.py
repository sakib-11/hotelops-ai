"""Async evidence processing worker (Task 17.11).

Processes ``EvidenceRef`` requests asynchronously WITHOUT losing or
duplicating evidence:

    EvidenceRequest
        → durable enqueue (REQUESTED → QUEUED)
        → atomic claim (QUEUED → EXTRACTING, lease + owner)
        → source resolution (Task 17.4, caller-scoped candidates)
        → extraction (Task 17.5/17.6: bytes → checksum → object storage)
        → upload checkpoint (EXTRACTING → UPLOADING + durable recovery)
        → EvidencePackage (Task 17.7) → FINALIZED

The queue IS the durable evidence state machine (Task 17.10) persisted
on the ref's JSONB metadata — NO new queue/retry architecture. Task 7
reliability is reused throughout:

- LEASING: ``EvidenceWorkStore.claim_queued`` claims with a lease and a
  claimed_by owner (FOR UPDATE SKIP LOCKED — the outbox pattern). A
  crashed worker's claim is reclaimed after the lease expires.
- CRASH RECOVERY: the durable state machine is the restart point —
  EXTRACTING (crashed mid-extraction) → re-claimed and re-extracted
  (deterministic artifact key + bytes ⇒ idempotent overwrite);
  UPLOADING (crashed after upload) → resumed from the durable recovery
  checkpoint and finalized WITHOUT re-extraction.
- RETRY/BACKOFF: retryable failures → RETRYABLE_FAILURE with the Task 7
  ``compute_backoff_delay`` persisted as retry_at.
- DEAD-LETTER: retries exhausted → TERMINAL_FAILURE (terminal, preserved
  for audit — never silently lost, never deleted).
- IDEMPOTENCY: deterministic ``extraction_id`` (Task 17.5) + ``package_id``
  (Task 17.7); the from-state guards make duplicate delivery a no-op and
  the atomic finalize can never persist a second package.
- OUTBOX: every transition emits a canonical event via the Task 7 outbox
  (evidence_audit), committed atomically with the state change.

The worker performs no business decisions on wall-clock time (event-time
semantics live in the rules); lease/retry timing is wall-clock only, and
``now`` is injectable for deterministic tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.evidence_audit import (
    EVENT_EXPIRED,
    EVENT_FINALIZED,
    EVENT_RECOVERED,
    EVENT_RETRYABLE_FAILURE,
    EVENT_TERMINAL_FAILURE,
    EVENT_UPLOADED,
    enqueue_evidence_audit_event,
)
from backend.app.application.services.evidence_state import EvidenceStateService
from backend.app.domain.evidence.exceptions import EvidenceStateMismatchError
from backend.app.domain.evidence.extraction import (
    EvidenceExtractor,
    ExtractedEvidence,
    ExtractionCancellationToken,
    ExtractionStatus,
)
from backend.app.domain.evidence.package import EvidencePackageBuilder
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceRecordingCandidate,
    SourceResolutionStatus,
    SourceResolver,
)
from backend.app.domain.evidence.state_machine import (
    EvidenceEvent,
    EvidenceProcessingState,
    EvidenceStateMachine,
)
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.database.models.evidence import (
    EvidencePackageModel,
    EvidenceRefModel,
)
from backend.app.infrastructure.database.repositories.evidence_work import (
    EVIDENCE_ARTIFACT_KEY,
    EVIDENCE_ATTEMPTS_KEY,
    EVIDENCE_LAST_ERROR_KEY,
    EVIDENCE_LEASE_UNTIL_KEY,
    EVIDENCE_PACKAGE_ID_KEY,
    EVIDENCE_RECOVERY_KEY,
    EVIDENCE_REQUEST_KEY,
    EVIDENCE_RETRY_AT_KEY,
    EvidenceWorkRepository,
    EvidenceWorkStore,
    iso_timestamp,
)
from backend.app.infrastructure.observability import metrics
from backend.app.infrastructure.observability.evidence import (
    SPAN_EXTRACTION,
    SPAN_FINALIZE,
    SPAN_PROCESS,
    SPAN_SOURCE_RESOLUTION,
    SPAN_UPLOAD,
    capture_telemetry,
    evidence_span,
    log_fields,
    read_telemetry,
    record,
    write_telemetry,
)
from backend.app.infrastructure.reliability import (
    compute_backoff_delay,
)
from backend.app.workers.base import PollingWorker
from contracts.events import EvidenceRef

logger = logging.getLogger(__name__)

_SHA256_HEX = frozenset("0123456789abcdef")


class EvidenceAuditSink(Protocol):
    """Persists an audit + outbox row for an evidence transition.

    The production sink is ``enqueue_evidence_audit_event`` (Task 7
    outbox, atomic with the caller's transaction); tests inject a
    recorder.
    """

    async def __call__(
        self,
        session: AsyncSession,
        *,
        ref: EvidenceRefModel,
        event_type: str,
        reason: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None: ...


@runtime_checkable
class SourceCandidateProvider(Protocol):
    """Supplies the tenant/venue-scoped candidate recordings for a request.

    The caller (worker) passes the canonical evidence request; the
    provider returns only candidates within the request's tenant/venue
    scope (the DB-backed lookup lives behind this port — never
    ``latest``, never outside scope, never a camera substitution).
    """

    async def candidates(self, evidence: EvidenceRef) -> Sequence[SourceRecordingCandidate]: ...


class EvidenceWorker(PollingWorker):
    """Processes EvidenceRefs through the durable pipeline (Task 17.11)."""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        extractor: EvidenceExtractor,
        candidates: SourceCandidateProvider,
        settings: Settings,
        worker_id: str | None = None,
        store_factory: Callable[[], EvidenceWorkStore] = EvidenceWorkRepository,
        state_service: EvidenceStateService | None = None,
        package_builder: EvidencePackageBuilder | None = None,
        audit_sink: EvidenceAuditSink = enqueue_evidence_audit_event,
        rng: random.Random | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            poll_interval=settings.evidence_worker_poll_interval,
            worker_id=worker_id or f"evidence:{uuid.uuid4().hex[:8]}",
        )
        self._database = database
        self._extractor = extractor
        self._candidates = candidates
        self._settings = settings
        self._store_factory = store_factory
        self._state = state_service or EvidenceStateService(machine=EvidenceStateMachine())
        self._package_builder = package_builder or EvidencePackageBuilder()
        self._audit_sink = audit_sink
        self._rng = rng or random.Random()
        self._now = now or (lambda: datetime.now(UTC))
        self._batch_size = settings.evidence_worker_batch_size
        self._lease_seconds = settings.evidence_worker_lease_seconds
        self._max_attempts = settings.evidence_worker_max_attempts
        # The cooperative cancellation token of the in-flight extraction
        # (set by request_stop → the extractor returns CANCELLED).
        self._current_token: ExtractionCancellationToken | None = None

    def request_stop(self) -> None:
        """Graceful stop — also cancel the in-flight extraction."""
        super().request_stop()
        token = self._current_token
        if token is not None:
            token.cancel()

    # =========================================================================
    # Cycle
    # =========================================================================

    async def run_once(self) -> int:
        """One full pipeline cycle; returns the number of refs handled."""
        now = self._now()
        handled = 0

        # 1. Durable enqueue: REQUESTED → QUEUED.
        async with self._database.session() as session:
            queued = await self._store_factory().queue_pending(
                session, now=now, batch_size=self._batch_size
            )
        if queued:
            # One counter per request admitted to the durable queue.
            for _ in queued:
                record(metrics.EVIDENCE_METRIC_REQUESTED)
        handled += len(queued)
        if self._stop_event.is_set():
            return handled

        # 2. Expire abandoned REQUESTED/QUEUED refs (the state machine's
        #    EXPIRED terminal — preserved for audit, never silently dropped).
        cutoff = now - timedelta(seconds=self._settings.evidence_worker_request_timeout_seconds)
        async with self._database.session() as session:
            expired = await self._store_factory().expire_abandoned(
                session,
                cutoff=cutoff,
                batch_size=self._batch_size,
                reason="abandoned evidence request expired",
            )
            for ref in expired:
                record(metrics.EVIDENCE_METRIC_EXPIRED)
                await self._audit(
                    session, ref, EVENT_EXPIRED, reason="abandoned evidence request expired"
                )
        handled += len(expired)
        if self._stop_event.is_set():
            return handled

        # 3. Crash recovery: reclaim stale EXTRACTING/UPLOADING claims
        #    BEFORE promotion so a recovered claim re-queues and completes
        #    within this same cycle (restart-safe in one pass).
        async with self._database.session() as session:
            stale = await self._store_factory().list_stale(session, now=now, limit=self._batch_size)
        for ref in stale:
            if self._stop_event.is_set():
                break
            try:
                if await self._recover_stale(ref, now):
                    handled += 1
            except Exception:
                logger.exception("evidence recovery failed for ref_id=%s", ref.ref_id)
        if self._stop_event.is_set():
            return handled

        # 4. Promote due retries: RETRYABLE_FAILURE (retry_at <= now) → QUEUED
        #    (includes freshly recovered claims from step 3).
        async with self._database.session() as session:
            await self._store_factory().promote_due_retries(
                session, now=now, batch_size=self._batch_size
            )
        if self._stop_event.is_set():
            return handled

        # 5. Claim + process queued refs.
        async with self._database.session() as session:
            claimed = await self._store_factory().claim_queued(
                session,
                worker_id=self.worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
                batch_size=self._batch_size,
            )
        for ref in claimed:
            if self._stop_event.is_set():
                break
            try:
                if await self._process_claimed(ref, now):
                    handled += 1
            except Exception:
                logger.exception("evidence processing failed for ref_id=%s", ref.ref_id)
        return handled

    # =========================================================================
    # Crash recovery (restart-safe)
    # =========================================================================

    async def _recover_stale(self, ref: EvidenceRefModel, now: datetime) -> bool:
        """Recover one stale claim under a row lock (serialized recovery).

        EXTRACTING (crashed mid-extraction) → RETRYABLE_FAILURE with
        retry_at=now, re-queued and re-extracted next cycle (deterministic
        artifact key + bytes ⇒ idempotent overwrite, never a duplicate).
        UPLOADING (crashed after upload) → resumed from the durable
        recovery checkpoint and finalized WITHOUT re-extraction.
        """
        async with self._database.session() as session:
            store = self._store_factory()
            locked = await store.lock_stale(
                session,
                ref.ref_id,
                worker_id=self.worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
            )
            if locked is None:
                return False  # another worker already recovered it
            state = self._state.current_state(locked)
            if state is EvidenceProcessingState.UPLOADING:
                return await self._finalize_from_checkpoint(session, locked, now)
            if state is not EvidenceProcessingState.EXTRACTING:
                return False
            result = self._state.apply(
                locked,
                EvidenceEvent.RETRYABLE_FAILURE,
                expected_state=EvidenceProcessingState.EXTRACTING,
            )
            if not result.applied:
                return False
            ok = await store.persist_transition(
                session,
                locked,
                from_state="extracting",
                to_state="retryable_failure",
                claimed_by=self.worker_id,
                updates={
                    EVIDENCE_RETRY_AT_KEY: iso_timestamp(now),
                    # The recovered ref must be immediately re-claimable —
                    # drop the (renewed) recovery lease with the transition.
                    EVIDENCE_LEASE_UNTIL_KEY: "",
                    EVIDENCE_LAST_ERROR_KEY: "lease expired — crashed claim recovered",
                },
            )
            if ok:
                await self._audit(
                    session,
                    locked,
                    EVENT_RECOVERED,
                    reason="lease expired — crashed claim recovered",
                )
            return ok

    # =========================================================================
    # Processing pipeline
    # =========================================================================

    async def _process_claimed(self, ref: EvidenceRefModel, now: datetime) -> bool:
        """Process one claimed (EXTRACTING) ref through the pipeline.

        Observability (Task 17.12): the telemetry carrier persisted on the
        ref at the async boundary (request_id, correlation_id, trace_id,
        plus the evidence identity) is threaded through every stage. The
        spans continue the ORIGINAL trace (Event → EvidenceRequest → Worker
        → Source → Extraction → Storage → Finalization) and carry only
        bounded identifiers; the pipeline counters fire at their exact
        points.
        """
        telemetry = read_telemetry(ref) or capture_telemetry()
        if any(value is not None for value in telemetry.to_dict().values()):
            write_telemetry(ref, telemetry)

        async with evidence_span(
            SPAN_PROCESS,
            ref=ref,
            telemetry=telemetry,
            parent_trace=telemetry.parent_trace,
        ):
            # --- 1. Rebuild the durable request contract ---
            evidence = self._evidence_request(ref)
            if evidence is None:
                return await self._terminal_failure(
                    ref,
                    now,
                    reason="evidence request contract missing or invalid on the ref",
                )

            # --- 2. Source resolution (Task 17.4 — never latest, outside scope) ---
            async with evidence_span(SPAN_SOURCE_RESOLUTION, ref=ref, telemetry=telemetry):
                try:
                    candidates = await self._candidates.candidates(evidence)
                except Exception as exc:
                    return await self._retryable_failure(
                        ref, now, reason=f"source candidate lookup failed: {exc}"
                    )
                segment = SourceResolver().resolve(evidence, candidates)
            if segment.status is SourceResolutionStatus.AUTHORIZATION_FAILURE:
                return await self._terminal_failure(
                    ref, now, reason=segment.reason or "source authorization failure"
                )
            if segment.status is SourceResolutionStatus.SOURCE_NOT_FOUND:
                return await self._terminal_failure(
                    ref, now, reason=segment.reason or "source not found"
                )

            # --- 3. Extraction (bytes → checksum → object storage, 17.5/17.6) ---
            token = ExtractionCancellationToken()
            self._current_token = token
            try:
                try:
                    async with evidence_span(SPAN_EXTRACTION, ref=ref, telemetry=telemetry):
                        extracted = await self._extractor.extract(
                            evidence, segment, cancellation=token
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    record(metrics.EVIDENCE_METRIC_EXTRACTION_FAILURE)
                    return await self._retryable_failure(
                        ref, now, reason=f"extraction failed: {exc}"
                    )
            finally:
                self._current_token = None

            if extracted.status is ExtractionStatus.CANCELLED:
                # Worker shutdown — lease expires and the claim is reclaimed.
                return False
            if extracted.status is ExtractionStatus.SOURCE_NOT_FOUND:
                record(metrics.EVIDENCE_METRIC_EXTRACTION_FAILURE)
                return await self._terminal_failure(
                    ref, now, reason=extracted.reason or "source not found during extraction"
                )
            if extracted.status is ExtractionStatus.CORRUPT_SOURCE:
                record(metrics.EVIDENCE_METRIC_EXTRACTION_FAILURE)
                return await self._terminal_failure(
                    ref, now, reason=extracted.reason or "source is corrupt — retry cannot succeed"
                )
            if extracted.status is ExtractionStatus.EXTRACTION_FAILED:
                record(metrics.EVIDENCE_METRIC_EXTRACTION_FAILURE)
                return await self._retryable_failure(
                    ref, now, reason=extracted.reason or "extraction failed"
                )

            # SUCCESS / PARTIAL — integrity gate: a completed artifact must
            # carry its storage reference + a valid SHA-256.
            if not self._integrity_ok(extracted):
                record(metrics.EVIDENCE_METRIC_EXTRACTION_FAILURE)
                return await self._retryable_failure(
                    ref,
                    now,
                    reason="artifact integrity verification failed (missing/invalid "
                    "checksum or storage reference)",
                )
            record(metrics.EVIDENCE_METRIC_EXTRACTION_SUCCESS)

            # --- 4. Upload checkpoint: EXTRACTING → UPLOADING + recovery ---
            async with (
                evidence_span(SPAN_UPLOAD, ref=ref, telemetry=telemetry),
                self._database.session() as session,
            ):
                store = self._store_factory()
                result = self._state.apply(
                    ref,
                    EvidenceEvent.EXTRACTION_COMPLETE,
                    expected_state=EvidenceProcessingState.EXTRACTING,
                )
                if not result.applied:
                    return False  # claim lost — another worker owns this ref
                updates: dict[str, Any] = {
                    EVIDENCE_RECOVERY_KEY: {
                        "resolved_source": segment.model_dump(mode="json"),
                        "extraction": extracted.model_dump(mode="json"),
                    },
                    EVIDENCE_ARTIFACT_KEY: extracted.media_path,
                    "extraction_id": str(extracted.extraction_id),
                }
                ok = await store.persist_transition(
                    session,
                    ref,
                    from_state="extracting",
                    to_state="uploading",
                    claimed_by=self.worker_id,
                    updates=updates,
                )
                if not ok:
                    record(metrics.EVIDENCE_METRIC_UPLOAD_FAILURE)
                    return False
                record(metrics.EVIDENCE_METRIC_UPLOAD_SUCCESS)
                await self._audit(
                    session,
                    ref,
                    EVENT_UPLOADED,
                    extra_payload={"media_path": extracted.media_path},
                )

            # --- 5. EvidencePackage (Task 17.7) + atomic finalize ---
            async with self._database.session() as session:
                return await self._finalize_package(session, ref, evidence, segment, extracted, now)

    # =========================================================================
    # Finalization
    # =========================================================================

    async def _finalize_package(
        self,
        session: AsyncSession,
        ref: EvidenceRefModel,
        evidence: EvidenceRef,
        segment: ResolvedSourceSegment,
        extracted: ExtractedEvidence,
        now: datetime,
    ) -> bool:
        """Build the auditable package and persist it atomically with FINALIZED.

        ``package_id`` is content-derived (Task 7) — replaying the same
        extraction produces the same package identity, and the atomic
        finalize guard means a duplicate delivery can never persist a
        second package row.
        """
        try:
            package = self._package_builder.finalize(
                evidence_ref=evidence,
                resolved_source=segment,
                extraction=extracted,
                created_at=None,
            )
        except ValueError as exc:
            # Deterministic provenance contradiction — a data condition,
            # not a transient failure: dead-letter it, preserved for audit.
            return await self._mark_terminal(
                session, ref, now, reason=f"package finalize refused: {exc}"
            )

        package_row = EvidencePackageModel(
            package_id=uuid.UUID(str(package.package_id)),
            tenant_id=uuid.UUID(str(evidence.tenant_id)) if evidence.tenant_id else ref.tenant_id,
            venue_id=uuid.UUID(str(evidence.venue_id)) if evidence.venue_id else ref.venue_id,
            description=f"evidence for event {evidence.event_id}",
            created_at=now,
        )
        telemetry = read_telemetry(ref) or capture_telemetry()
        async with evidence_span(SPAN_FINALIZE, ref=ref, telemetry=telemetry):
            store = self._store_factory()
            ok = await store.save_finalized(
                session,
                ref,
                claimed_by=self.worker_id,
                package=package_row,
                link={
                    "package_id": package_row.package_id,
                    "ref_id": ref.ref_id,
                    "tenant_id": ref.tenant_id,
                },
                updates={
                    EVIDENCE_PACKAGE_ID_KEY: str(package.package_id),
                },
            )
            if ok:
                record(metrics.EVIDENCE_METRIC_FINALIZED)
                await self._audit(
                    session,
                    ref,
                    EVENT_FINALIZED,
                    extra_payload={"package_id": str(package.package_id)},
                )
                logger.info(
                    "evidence finalized: package_id=%s",
                    package.package_id,
                    extra=log_fields(ref, telemetry),
                )
            return ok

    async def _finalize_from_checkpoint(
        self, session: AsyncSession, ref: EvidenceRefModel, now: datetime
    ) -> bool:
        """Resume a crashed-after-upload ref from its durable checkpoint.

        The recovery record (resolved source + extraction) was persisted
        atomically with the UPLOADING transition — the package is rebuilt
        from it and finalized WITHOUT re-extraction (no duplicate logical
        package, no wasted work).
        """
        recovery = (ref.metadata_ or {}).get(EVIDENCE_RECOVERY_KEY)
        if not isinstance(recovery, dict) or not isinstance(recovery.get("extraction"), dict):
            return await self._mark_terminal(
                session,
                ref,
                now,
                reason="UPLOADING recovery checkpoint missing — cannot resume",
            )
        evidence = self._evidence_request(ref)
        if evidence is None:
            return await self._mark_terminal(
                session,
                ref,
                now,
                reason="evidence request contract missing on the ref",
            )
        try:
            segment = ResolvedSourceSegment.model_validate(recovery["resolved_source"])
            extracted = ExtractedEvidence.model_validate(recovery["extraction"])
        except KeyError, ValueError:
            return await self._mark_terminal(
                session,
                ref,
                now,
                reason="invalid UPLOADING recovery checkpoint",
            )
        if not self._integrity_ok(extracted):
            return await self._mark_terminal(
                session,
                ref,
                now,
                reason="recovery checkpoint artifact failed integrity verification",
            )
        return await self._finalize_package(session, ref, evidence, segment, extracted, now)

    # =========================================================================
    # Failure classification (Task 7 retry/backoff/dead-letter)
    # =========================================================================

    async def _retryable_failure(
        self, ref: EvidenceRefModel, now: datetime, *, reason: str
    ) -> bool:
        async with self._database.session() as session:
            return await self._mark_retryable(session, ref, now, reason)

    async def _terminal_failure(self, ref: EvidenceRefModel, now: datetime, *, reason: str) -> bool:
        async with self._database.session() as session:
            return await self._mark_terminal(session, ref, now, reason)

    async def _mark_retryable(
        self, session: AsyncSession, ref: EvidenceRefModel, now: datetime, reason: str
    ) -> bool:
        """EXTRACTING → RETRYABLE_FAILURE with the persisted Task 7 backoff.

        When the retry budget is exhausted the ref is dead-lettered to
        TERMINAL_FAILURE instead (terminal, preserved — never deleted).
        """
        store = self._store_factory()
        attempts = int((ref.metadata_ or {}).get(EVIDENCE_ATTEMPTS_KEY, 0))
        if attempts >= self._max_attempts:
            return await self._mark_terminal(
                session, ref, now, reason=f"retry budget exhausted: {reason}"
            )
        try:
            result = self._state.apply(
                ref,
                EvidenceEvent.RETRYABLE_FAILURE,
                expected_state=EvidenceProcessingState.EXTRACTING,
            )
        except EvidenceStateMismatchError:
            return False
        if not result.applied:
            return False
        delay = compute_backoff_delay(
            attempts,
            base_seconds=self._settings.evidence_worker_backoff_base,
            max_seconds=self._settings.evidence_worker_backoff_max,
            jitter=self._settings.evidence_worker_backoff_jitter,
            rng=self._rng,
        )
        retry_at = now + delay
        updates: dict[str, Any] = {
            EVIDENCE_RETRY_AT_KEY: iso_timestamp(retry_at),
            EVIDENCE_LAST_ERROR_KEY: reason[:512],
        }
        ok = await store.persist_transition(
            session,
            ref,
            from_state="extracting",
            to_state="retryable_failure",
            claimed_by=self.worker_id,
            updates=updates,
        )
        if ok:
            record(metrics.EVIDENCE_METRIC_RETRY)
            await self._audit(
                session,
                ref,
                EVENT_RETRYABLE_FAILURE,
                reason=reason,
                extra_payload={
                    "attempts": attempts,
                    "retry_at": iso_timestamp(retry_at),
                },
            )
            logger.warning(
                "evidence retry scheduled: attempts=%d retry_in=%ss reason=%s",
                attempts,
                delay.total_seconds(),
                reason,
                extra=log_fields(ref, read_telemetry(ref) or capture_telemetry()),
            )
        return ok

    async def _mark_terminal(
        self, session: AsyncSession, ref: EvidenceRefModel, now: datetime, reason: str
    ) -> bool:
        """EXTRACTING/UPLOADING → TERMINAL_FAILURE (dead-letter, preserved)."""
        store = self._store_factory()
        state = self._state.current_state(ref)
        try:
            result = self._state.apply(
                ref,
                EvidenceEvent.TERMINAL_FAILURE,
                expected_state=state,
            )
        except EvidenceStateMismatchError:
            return False
        if not result.applied:
            return False
        from_state = state.value
        ok = await store.persist_transition(
            session,
            ref,
            from_state=from_state,
            to_state="terminal_failure",
            claimed_by=self.worker_id,
            updates={EVIDENCE_LAST_ERROR_KEY: reason[:512]},
        )
        if ok:
            await self._audit(session, ref, EVENT_TERMINAL_FAILURE, reason=reason)
            logger.error(
                "evidence dead-lettered: reason=%s",
                reason,
                extra=log_fields(ref, read_telemetry(ref) or capture_telemetry()),
            )
        return ok

    # =========================================================================
    # Helpers
    # =========================================================================

    def _evidence_request(self, ref: EvidenceRefModel) -> EvidenceRef | None:
        """Rebuild the durable request contract from the ref metadata.

        The request contract is the worker's input (persisted at request
        creation). A missing/invalid contract is a data-quality condition —
        dead-lettered, never silently re-derived from the row.
        """
        raw = (ref.metadata_ or {}).get(EVIDENCE_REQUEST_KEY)
        if raw is None:
            return None
        try:
            payload = raw if isinstance(raw, dict) else json.loads(raw)
            return EvidenceRef.model_validate(payload)
        except ValueError, TypeError, json.JSONDecodeError:
            return None

    @staticmethod
    def _integrity_ok(extracted: ExtractedEvidence) -> bool:
        """A completed artifact must carry its storage reference + valid SHA-256."""
        if not extracted.media_path:
            return False
        checksum = extracted.metadata.get("checksum_sha256")
        if not isinstance(checksum, str) or len(checksum) != 64:
            return False
        return all(c in _SHA256_HEX for c in checksum)

    async def _audit(
        self,
        session: AsyncSession,
        ref: EvidenceRefModel,
        event_type: str,
        reason: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._audit_sink(
                session,
                ref=ref,
                event_type=event_type,
                reason=reason,
                extra_payload=extra_payload,
            )
        except Exception:
            # Audit must never break the pipeline — the state change is
            # already committed; the failure is logged (Task 8).
            logger.exception("evidence audit enqueue failed: ref_id=%s", ref.ref_id)


def _main() -> None:
    """Standalone entrypoint (``python -m backend.app.workers.evidence``).

    The worker depends on three deployment-wired ports that have no
    in-process implementation (by design, Task 17.5/17.6):

    - the DB-backed ``SourceCandidateProvider`` (candidate recordings),
    - the ``RecordingLocator`` (exact recording object),
    - the codec ``MediaProcessor`` (the actual trim).

    The wiring is deployment configuration, not worker code — this
    entrypoint boots the polling loop once the deployment registers
    them. Until then it exits with a clear message instead of silently
    processing nothing.
    """
    from backend.app.infrastructure.logging import configure_logging
    from backend.app.infrastructure.observability import tracing

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings=settings)
    tracing.configure_tracing(settings)
    logger.error(
        "evidence worker ports not wired: the deployment must provide the "
        "SourceCandidateProvider, RecordingLocator, and MediaProcessor "
        "(Task 17.5/17.6 seams) before the worker can run"
    )


if __name__ == "__main__":
    _main()
