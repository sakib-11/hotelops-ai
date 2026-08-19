"""Task 18.11 — Async outbox processing (vertical slice).

Connects the OUTBOX EVENT persisted by the authoritative persistence
boundary (Task 18.10) to the EXISTING Task 7 worker — the outbox
publisher (lease → publish), the ingress bridge (Redis → inbox, dedup),
and the inbox consumer running the slice's registered effect handler
(Task 18.11 production wiring):

    BEGIN (18.10)
      fact → event → audit → outbox (COMMIT)
        → OutboxPublisherWorker   (lease → publish → published)
        → Redis stream            (transport, not truth)
        → InboxIngressBridge      (dedup on (source, event_id))
        → InboxConsumerWorker     (claim → EFFECT + processed, atomic)
        → durable evidence request (Task 18.9 — one per event)

The pipeline stages are modeled in-memory, faithful to the documented
Task 7 semantics (the same lease/claim/backoff/dead-letter transitions
the integration suite verifies against real TimescaleDB + Redis): a row
is claimable only when its lease is expired; a live lease blocks other
workers; publish/effect failures persist a bounded backoff as
``available_at``; the retry budget is enforced per row; dead-letter is
terminal and NEVER deletes the row. The EFFECT handler itself is the
real production code (``build_operational_effect_handlers``), driving
the real Task 18.9 ``EvidenceLinkageService`` (idempotent — the request
PK is the content-derived ref_id, so at-least-once delivery yields ONE
logical evidence request per event).

Tests (the task's list):

1. normal delivery  — commit → lease → publish → inbox → effect; one
                      logical evidence request per event;
2. duplicate delivery — publisher crash after publish re-delivers
                      (at-least-once); the inbox dedup + idempotent
                      effect yield ONE logical effect per event;
3. worker crash     — crash after claim (outbox) and crash after the
                      effect (inbox): the lease expires and recovery
                      completes delivery — no event loss after commit;
4. worker restart   — fresh processes (new worker ids) reclaim the
                      crashed workers' rows and finish the pipeline;
5. lease expiry     — a live lease blocks a second worker; an expired
                      lease is reclaimable and the effect runs once;
6. retry            — a transient effect failure persists a bounded
                      backoff; the retry succeeds — one effect;
7. dead-letter      — a permanent effect failure dead-letters after the
                      budget; the row (payload, attempts, error) is
                      preserved and NO effect was ever committed.

STOP condition: an event committed by 18.10 is NEVER lost (the outbox
row is the durability boundary — every failure path keeps it and
retries or dead-letters, never deletes); and duplicate delivery can
never produce a second logical business effect.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.application.services.operational_persistence import (
    OperationalPersistenceService,
)
from backend.app.infrastructure.database.models.audit_outbox_inbox import (
    InboxMessageModel,
    OutboxEventModel,
)
from backend.app.infrastructure.reliability import compute_backoff_delay
from backend.app.workers.operational_effects import build_operational_effect_handlers
from contracts.common import utc_now
from contracts.events import EventEnvelope
from tests.unit.fakes import make_actor
from tests.unit.test_vertical_slice_evidence import FakeEvidenceLinkageRepository
from tests.unit.test_vertical_slice_persistence import (
    FakeOutbox as PersistenceOutbox,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeSession as PersistenceSession,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeStore,
)
from tests.unit.test_vertical_slice_rule import (
    _identities,
    _load_manifest,
    _run_full_slice,
)

SOURCE_OUTBOX = "outbox"


class _NoopSession:
    """The consumer transaction handed to the effect handler.

    The fake evidence repository owns the dedup semantics and never
    touches the session (the real repository would write through it);
    the handler's contract only requires a transaction-scoped session.
    """


# =============================================================================
# Faithful in-memory Task 7 pipeline
# =============================================================================


class FakePipeline:
    """In-memory model of the Task 7 pipeline, faithful to its semantics.

    Mirrors the state transitions of ``OutboxPublisherWorker`` /
    ``InboxIngressBridge`` / ``InboxConsumerWorker``: lease-based claims
    (a live lease blocks other workers; expired leases are reclaimable),
    at-least-once publish (a crash after publish leaves the row
    processing so it is re-published), inbox dedup on (source,
    source_message_id), the effect + ``mark_processed`` atomic pair, and
    persisted bounded backoff → dead-letter for failures.

    ``failing_event_ids`` models a transient/permanent effect failure:
    the effect is skipped (its writes are rolled back by the consumer's
    savepoint, so NOTHING is committed) and the row is failed or
    dead-lettered exactly like the real worker.
    """

    def __init__(
        self,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
    ) -> None:
        self.outbox: dict[uuid.UUID, OutboxEventModel] = {}
        self.outbox_by_event: dict[uuid.UUID, OutboxEventModel] = {}
        self.inbox: dict[uuid.UUID, InboxMessageModel] = {}
        self.inbox_by_key: dict[tuple[str, str], InboxMessageModel] = {}
        self.stream: list[dict[str, Any]] = []
        self.acked: set[str] = set()
        self.now: datetime = utc_now()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.failing_event_ids: set[str] = set()
        self._message_counter = itertools.count(1)

    # ------------------------------------------------------------------
    # Clock + seeding
    # ------------------------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Advance the pipeline clock (lease expiry / backoff elapse)."""
        self.now += timedelta(seconds=seconds)

    def seed_outbox(self, store: FakeStore) -> None:
        """Seed the durable outbox from a committed 18.10 store."""
        for row in store.outbox.values():
            row.available_at = row.available_at or self.now
            row.attempts = row.attempts or 0
            self.outbox[row.outbox_id] = row
            self.outbox_by_event[row.event_id] = row

    # ------------------------------------------------------------------
    # Publisher stage (OutboxPublisherWorker.run_once)
    # ------------------------------------------------------------------

    def claim_outbox(self, worker_id: str, batch: int = 20) -> list[OutboxEventModel]:
        return self._claim(list(self.outbox.values()), worker_id, batch)

    def publish_once(
        self,
        worker_id: str,
        *,
        transport_failing: bool = False,
        crash_after_publish: bool = False,
    ) -> int:
        """One claim → publish → mark cycle.

        ``crash_after_publish`` models the publisher dying between the
        Redis write and the published transition: the row stays
        processing and is re-published after its lease expires
        (at-least-once). ``transport_failing`` models Redis being down:
        the row is failed with a persisted backoff (or dead-lettered
        once the budget is exhausted) — never lost.
        """
        claimed = self.claim_outbox(worker_id)
        published = 0
        for row in claimed:
            if transport_failing:
                self._fail_outbox(row, worker_id)
                continue
            self._append_stream(row)
            if crash_after_publish:
                continue  # crash before mark — the lease must expire first
            if self._mark_published(row, worker_id):
                published += 1
        return published

    def republish(self, row: OutboxEventModel) -> None:
        """Re-deliver an event's envelope to the stream (crash recovery)."""
        self._append_stream(row)

    # ------------------------------------------------------------------
    # Ingress stage (InboxIngressBridge.run_once)
    # ------------------------------------------------------------------

    def relay_once(self, *, source: str = SOURCE_OUTBOX) -> int:
        """Read the stream and insert deduplicated inbox rows (then ack).

        A duplicate delivery (the same event_id twice) inserts NOTHING —
        the unique (source, source_message_id) key rejected it — but the
        message is still acknowledged (the real bridge acks after the
        insert or the dedup reject).
        """
        handled = 0
        for msg in self.stream:
            message_id = msg["id"]
            if message_id in self.acked:
                continue
            envelope = EventEnvelope[Any].model_validate(json.loads(msg["event"]))
            # Tenant/venue scope are sibling fields on the wire (ADR-004),
            # never part of the envelope contract — same as the bridge.
            tenant_id = uuid.UUID(msg["tenant_id"])
            venue_id = uuid.UUID(msg["venue_id"]) if msg.get("venue_id") else None
            key = (source, str(envelope.event_id))
            if key not in self.inbox_by_key:
                row = InboxMessageModel(
                    inbox_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    venue_id=venue_id,
                    source=source,
                    source_message_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                    payload=envelope.model_dump(mode="json"),
                    status="pending",
                    available_at=self.now,
                    attempts=0,
                )
                self.inbox[row.inbox_id] = row
                self.inbox_by_key[key] = row
            self.acked.add(message_id)
            handled += 1
        return handled

    # ------------------------------------------------------------------
    # Consumer stage (InboxConsumerWorker.run_once)
    # ------------------------------------------------------------------

    def claim_inbox(self, worker_id: str, batch: int = 20) -> list[InboxMessageModel]:
        return self._claim(list(self.inbox.values()), worker_id, batch)

    async def consume_once(
        self,
        worker_id: str,
        *,
        handlers: dict[str, Any] | None = None,
        crash_after_effect: bool = False,
    ) -> int:
        """One claim → effect (+ processed) → transition cycle.

        The effect and ``mark_processed`` are atomic: when the effect
        fails, the consumer's savepoint discards its writes and the row
        is failed with a persisted backoff (or dead-lettered once the
        retry budget is exhausted) — the processed transition never
        commits. ``crash_after_effect`` models the worker dying after
        the effect ran but before the processed transition committed.
        """
        handlers = handlers or {}
        claimed = self.claim_inbox(worker_id)
        processed = 0
        for row in claimed:
            handler = handlers.get(row.event_type or "")
            if handler is None:
                self._dead_letter_inbox(
                    row,
                    worker_id,
                    f"no effect handler registered for event_type={row.event_type!r}",
                )
                continue
            if row.source_message_id in self.failing_event_ids:
                # The effect raised inside the consumer transaction: the
                # savepoint rolled back its writes (nothing committed) and
                # the row is retried with backoff or dead-lettered.
                if (row.attempts or 0) >= self.max_attempts:
                    self._dead_letter_inbox(row, worker_id, "simulated effect failure")
                else:
                    self._fail_inbox(row, worker_id)
                continue
            await handler(_NoopSession(), row)
            if crash_after_effect:
                continue  # crash before mark — the lease must expire first
            if self._mark_processed(row, worker_id):
                processed += 1
        return processed

    # ------------------------------------------------------------------
    # Shared claim/transition primitives
    # ------------------------------------------------------------------

    def _claim(self, rows: list[Any], worker_id: str, batch: int) -> list[Any]:
        claimable = [
            r
            for r in rows
            if r.status in ("pending", "failed", "processing")
            and (r.available_at is None or r.available_at <= self.now)
            and (r.claimed_until is None or r.claimed_until <= self.now)
        ]
        claimable.sort(key=lambda r: r.available_at or self.now)
        selected = claimable[:batch]
        for row in selected:
            row.status = "processing"
            row.claimed_by = worker_id
            row.claimed_until = self.now + timedelta(seconds=self.lease_seconds)
            row.attempts = (row.attempts or 0) + 1
        return selected

    def _append_stream(self, row: OutboxEventModel) -> str:
        message_id = f"{self.now.timestamp():016.6f}-{next(self._message_counter)}"
        self.stream.append({
            "id": message_id,
            "event": json.dumps(row.payload, separators=(",", ":"), ensure_ascii=False),
            "event_id": str(row.event_id),
            "event_type": row.event_type,
            "tenant_id": str(row.tenant_id),
            "venue_id": str(row.venue_id) if row.venue_id else None,
            "schema_version": row.schema_version or "",
        })
        return message_id

    def _mark_published(self, row: OutboxEventModel, worker_id: str) -> bool:
        if row.status != "processing" or row.claimed_by != worker_id:
            return False  # lease lost — never override the new owner
        row.status = "published"
        row.claimed_by = None
        row.claimed_until = None
        row.published_at = self.now
        return True

    def _mark_processed(self, row: InboxMessageModel, worker_id: str) -> bool:
        if row.status != "processing" or row.claimed_by != worker_id:
            return False
        row.status = "processed"
        row.claimed_by = None
        row.claimed_until = None
        row.processed_at = self.now
        return True

    def _fail_outbox(self, row: OutboxEventModel, worker_id: str) -> None:
        if row.status != "processing" or row.claimed_by != worker_id:
            return
        if (row.attempts or 0) >= self.max_attempts:
            self._dead_letter_outbox(row, worker_id)
            return
        delay = compute_backoff_delay(
            row.attempts or 0,
            base_seconds=self.backoff_base,
            max_seconds=self.backoff_max,
            jitter=0.0,
        )
        row.status = "failed"
        row.claimed_by = None
        row.claimed_until = None
        row.available_at = self.now + delay
        row.last_error = "Redis is unavailable (simulated)"

    def _dead_letter_outbox(self, row: OutboxEventModel, worker_id: str) -> None:
        if row.status != "processing" or row.claimed_by != worker_id:
            return
        row.status = "dead_letter"
        row.claimed_by = None
        row.claimed_until = None
        row.last_error = "Redis is unavailable (simulated)"

    def _fail_inbox(self, row: InboxMessageModel, worker_id: str) -> None:
        if row.status != "processing" or row.claimed_by != worker_id:
            return
        delay = compute_backoff_delay(
            row.attempts or 0,
            base_seconds=self.backoff_base,
            max_seconds=self.backoff_max,
            jitter=0.0,
        )
        row.status = "failed"
        row.claimed_by = None
        row.claimed_until = None
        row.available_at = self.now + delay
        row.last_error = "simulated effect failure"

    def _dead_letter_inbox(self, row: InboxMessageModel, worker_id: str, error: str) -> None:
        if row.status != "processing" or row.claimed_by != worker_id:
            return
        row.status = "dead_letter"
        row.claimed_by = None
        row.claimed_until = None
        row.last_error = error


# =============================================================================
# Slice helpers
# =============================================================================


async def _persist_slice(store: FakeStore | None = None) -> tuple[FakeStore, Any]:
    """Drive the 18.8 slice and persist every fact+event via 18.10 (commit).

    Returns the committed store and the slice outcome (events).
    """
    manifest = _load_manifest()
    ids = _identities(manifest)
    outcome = _run_full_slice(manifest, ids)
    store = store or FakeStore()
    session = PersistenceSession(store)
    service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
    actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))
    for snapshot, event in zip(outcome.snapshots, outcome.events, strict=True):
        result = await service.persist(session, fact=snapshot, event=event, actor=actor)
        assert result.created is True
    await session.commit()
    return store, outcome


def _effect_handlers(repository: FakeEvidenceLinkageRepository) -> dict[str, Any]:
    """The REAL 18.11 effect handlers over the REAL 18.9 linkage service."""
    return build_operational_effect_handlers(
        evidence_linkage=EvidenceLinkageService(repository=repository),
    )


def _events_of(repository: FakeEvidenceLinkageRepository) -> set[str]:
    """The event_ids that reached a durable evidence request (the effect)."""
    return {str(row.event_id) for row in repository.rows.values()}


# =============================================================================
# 1. NORMAL DELIVERY — commit → lease → publish → inbox → effect
# =============================================================================


class TestNormalDelivery:
    async def test_full_async_delivery_one_effect_per_event(self) -> None:
        store, outcome = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        # The 18.10 COMMIT made the outbox rows durable — the durability
        # boundary (nothing published to Redis before this point).
        assert len(pipeline.outbox) == 2
        assert all(row.status == "pending" for row in pipeline.outbox.values())

        # 1. lease → publish → published
        assert pipeline.publish_once("publisher-1") == 2
        assert {row.status for row in pipeline.outbox.values()} == {"published"}
        assert len(pipeline.stream) == 2

        # 2. Redis → inbox (dedup insert + ack)
        assert pipeline.relay_once() == 2
        assert len(pipeline.inbox) == 2
        assert len(pipeline.acked) == 2

        # 3. claim → effect + processed (atomic)
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 2
        assert {row.status for row in pipeline.inbox.values()} == {"processed"}

        # The effect: ONE durable evidence request per event (the 18.9
        # linkage of the delivered envelope).
        assert _events_of(repository) == {str(e.event_id) for e in outcome.events}
        assert len(repository.rows) == 2

    async def test_effect_runs_inside_the_consumer_transaction(self) -> None:
        """The effect and the processed transition commit together: before
        the consumer runs, NO evidence request exists; after processing,
        exactly one per event."""
        store, _ = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        pipeline.publish_once("publisher-1")
        pipeline.relay_once()
        assert repository.rows == {}  # the effect has not run yet

        await pipeline.consume_once("consumer-1", handlers=handlers)
        assert len(repository.rows) == 2
        assert all(row.status == "processed" for row in pipeline.inbox.values())


# =============================================================================
# 2. DUPLICATE DELIVERY — at-least-once redelivery → ONE logical effect
# =============================================================================


class TestDuplicateDelivery:
    async def test_publisher_crash_redelivery_is_one_logical_effect(self) -> None:
        store, outcome = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        # The publisher publishes BOTH events, then crashes before marking
        # them published (the rows stay processing under a live lease).
        assert pipeline.publish_once("publisher-a", crash_after_publish=True) == 0
        assert len(pipeline.stream) == 2
        assert {row.status for row in pipeline.outbox.values()} == {"processing"}

        # The lease expires; the SAME worker restarts and re-publishes.
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-a") == 2
        assert len(pipeline.stream) == 4  # at-least-once: 2 messages per event
        assert {row.status for row in pipeline.outbox.values()} == {"published"}

        # The bridge dedups on (source, event_id): exactly TWO inbox rows,
        # all four stream messages acknowledged.
        assert pipeline.relay_once() == 4
        assert len(pipeline.inbox) == 2
        assert len(pipeline.acked) == 4

        # The consumer runs the effect once per INBOX ROW — the duplicate
        # delivery collapsed to one logical effect per event.
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 2
        assert len(repository.rows) == 2
        assert _events_of(repository) == {str(e.event_id) for e in outcome.events}

    async def test_direct_redelivery_is_deduped(self) -> None:
        """A second delivery of the same event (a re-publish) inserts no
        second inbox row and produces no second effect."""
        store, _ = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        pipeline.publish_once("publisher-1")
        pipeline.relay_once()
        await pipeline.consume_once("consumer-1", handlers=handlers)
        assert len(repository.rows) == 2

        # The same events are delivered again (at-least-once redelivery).
        for row in pipeline.outbox_by_event.values():
            pipeline.republish(row)
        assert len(pipeline.stream) == 4

        # Deduped at ingress: no new inbox rows; the consumer has nothing.
        assert pipeline.relay_once() == 2
        assert len(pipeline.inbox) == 2
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 0

        # ONE logical business effect per event, still.
        assert len(repository.rows) == 2


# =============================================================================
# 3. WORKER CRASH — a crash never loses an event committed by 18.10
# =============================================================================


class TestWorkerCrash:
    async def test_crash_after_claim_is_recovered_without_loss(self) -> None:
        store, _ = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        # The publisher claims both events, then the PROCESS dies before
        # publishing anything.
        assert len(pipeline.claim_outbox("crashy-publisher")) == 2
        assert {row.status for row in pipeline.outbox.values()} == {"processing"}
        assert len(pipeline.stream) == 0

        # The lease expires; a fresh worker reclaims and completes.
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-2") == 2
        assert len(pipeline.stream) == 2
        assert pipeline.relay_once() == 2
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 2

        # No event loss after the successful 18.10 commit: both events
        # delivered, exactly one effect each.
        assert len(pipeline.outbox) == 2
        assert len(repository.rows) == 2

    async def test_crash_after_effect_never_double_counts(self) -> None:
        """The consumer runs the effect, then crashes before the processed
        transition commits: the effect's writes were never committed, and
        the recovery run collapses to the SAME logical request (the
        idempotent effect — one logical business effect per event)."""
        store, outcome = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        pipeline.publish_once("publisher-1")
        pipeline.relay_once()

        # The consumer claims the inbox rows and runs the effect, then
        # dies before the processed transition commits.
        assert (
            await pipeline.consume_once("consumer-a", handlers=handlers, crash_after_effect=True)
            == 0
        )
        assert {row.status for row in pipeline.inbox.values()} == {"processing"}
        assert len(repository.rows) == 2  # the effect ran (uncommitted)

        # The lease expires; a fresh consumer reclaims and completes —
        # the effect runs again but the deterministic ref_id collapses to
        # the existing requests (Task 7 idempotency).
        pipeline.advance(pipeline.lease_seconds + 1)
        assert await pipeline.consume_once("consumer-b", handlers=handlers) == 2
        assert {row.status for row in pipeline.inbox.values()} == {"processed"}
        assert all(row.attempts == 2 for row in pipeline.inbox.values())

        # ONE logical evidence request per event — never a second.
        assert len(repository.rows) == 2
        assert _events_of(repository) == {str(e.event_id) for e in outcome.events}


# =============================================================================
# 4. WORKER RESTART — fresh processes finish the crashed workers' work
# =============================================================================


class TestWorkerRestart:
    async def test_restart_picks_up_mid_pipeline_without_loss(self) -> None:
        store, _ = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        # Worker generation A: publishes, relays one side, claims inbox —
        # then the whole process tree dies at every boundary.
        assert pipeline.publish_once("gen-a-pub", crash_after_publish=True) == 0
        assert pipeline.relay_once() == 2
        assert len(pipeline.claim_inbox("gen-a-con")) == 2
        assert {row.status for row in pipeline.inbox.values()} == {"processing"}

        # Restart: brand-new processes (fresh worker ids) reclaim every
        # expired lease and complete the pipeline.
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("gen-b-pub") == 2  # re-publish (deduped later)
        assert pipeline.relay_once() == 2  # acked, no new rows
        assert await pipeline.consume_once("gen-b-con", handlers=handlers) == 2

        # Every committed event reached its effect exactly once.
        assert len(pipeline.outbox) == 2
        assert all(row.status == "published" for row in pipeline.outbox.values())
        assert all(row.status == "processed" for row in pipeline.inbox.values())
        assert len(repository.rows) == 2

    async def test_outbox_rows_survive_restart_cycles(self) -> None:
        """Rows are never deleted across crash/restart — the durable outbox
        remains the source of truth until each event is published."""
        store, _ = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        for generation in range(3):  # three crash + restart generations
            pipeline.publish_once(f"pub-gen-{generation}", crash_after_publish=True)
            pipeline.advance(pipeline.lease_seconds + 1)
        assert len(pipeline.outbox) == 2  # never deleted

        assert pipeline.publish_once("pub-final") == 2
        # All messages relayed (each redelivery acked; dedup keeps TWO rows).
        assert pipeline.relay_once() == 8
        assert len(pipeline.inbox) == 2
        assert await pipeline.consume_once("con-final", handlers=handlers) == 2
        assert len(repository.rows) == 2


# =============================================================================
# 5. LEASE EXPIRY — a live lease blocks; an expired lease is reclaimable
# =============================================================================


class TestLeaseExpiry:
    async def test_live_lease_blocks_other_workers_then_expires(self) -> None:
        store, _ = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        # Worker A claims both events (lease valid for lease_seconds).
        claimed = pipeline.claim_outbox("worker-a")
        assert len(claimed) == 2
        assert all(row.claimed_by == "worker-a" for row in claimed)

        # Worker B CANNOT claim while the lease is live.
        assert pipeline.claim_outbox("worker-b") == []

        # A crashes; the lease expires; B reclaims (attempts advance on
        # every claim — crash recovery) and completes the delivery.
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("worker-b") == 2
        assert all(row.attempts == 2 for row in pipeline.outbox.values())
        assert all(row.claimed_by is None for row in pipeline.outbox.values())
        assert pipeline.relay_once() == 2
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 2

        # The effect ran exactly once (by B's recovery delivery).
        assert len(repository.rows) == 2
        assert all(row.status == "published" for row in pipeline.outbox.values())


# =============================================================================
# 6. RETRY — a transient effect failure persists a backoff, then succeeds
# =============================================================================


class TestRetry:
    async def test_transient_failure_retries_with_backoff_then_succeeds(self) -> None:
        store, outcome = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        pipeline.publish_once("publisher-1")
        pipeline.relay_once()

        # The effect fails transiently for ONE event (e.g. the extraction
        # store is briefly unavailable).
        failing = outcome.events[0]
        pipeline.failing_event_ids.add(str(failing.event_id))

        # First attempt: the failing row is failed with a persisted
        # backoff; the healthy row processes normally.
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 1
        failed = next(
            r for r in pipeline.inbox.values() if r.source_message_id == str(failing.event_id)
        )
        assert failed.status == "failed"
        assert failed.attempts == 1
        assert failed.last_error is not None
        assert failed.available_at > pipeline.now  # persisted backoff
        assert len(repository.rows) == 1  # only the healthy event's effect

        # The backoff elapses, the transient failure clears, the retry
        # succeeds — the row is processed and its effect committed.
        pipeline.advance((failed.available_at - pipeline.now).total_seconds() + 1)
        pipeline.failing_event_ids.discard(str(failing.event_id))
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 1
        assert failed.status == "processed"
        assert len(repository.rows) == 2  # one logical effect per event
        assert len(pipeline.inbox) == 2  # never a second inbox row


# =============================================================================
# 7. DEAD-LETTER — a permanent failure is terminal, preserved, never an effect
# =============================================================================


class TestDeadLetter:
    async def test_permanent_failure_dead_letters_with_payload_preserved(self) -> None:
        store, outcome = await _persist_slice()
        pipeline = FakePipeline(max_attempts=3)
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        pipeline.publish_once("publisher-1")
        pipeline.relay_once()

        # The effect fails PERMANENTLY for one event.
        failing = outcome.events[0]
        pipeline.failing_event_ids.add(str(failing.event_id))

        # Attempts 1 and 2 → failed with a persisted backoff each time.
        for _ in range(2):
            await pipeline.consume_once("consumer-1", handlers=handlers)
            row = next(
                r for r in pipeline.inbox.values() if r.source_message_id == str(failing.event_id)
            )
            assert row.status == "failed"
            pipeline.advance((row.available_at - pipeline.now).total_seconds() + 1)

        # Attempt 3 → DEAD_LETTER (terminal, preserved, never deleted).
        await pipeline.consume_once("consumer-1", handlers=handlers)
        row = next(
            r for r in pipeline.inbox.values() if r.source_message_id == str(failing.event_id)
        )
        assert row.status == "dead_letter"
        assert row.attempts == 3
        assert row.last_error is not None
        # The payload (the canonical envelope) is preserved for recovery.
        assert EventEnvelope.model_validate(row.payload).event_id == failing.event_id
        # The healthy event completed normally; the dead-lettered event's
        # effect NEVER committed.
        assert len(repository.rows) == 1
        assert str(failing.event_id) not in _events_of(repository)
        assert len(pipeline.inbox) == 2  # rows are never deleted

    async def test_unknown_event_type_dead_letters_with_reason(self) -> None:
        """A delivered message whose event_type has no registered effect is
        dead-lettered immediately with a clear reason (never retried)."""
        store, outcome = await _persist_slice()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        repository = FakeEvidenceLinkageRepository()
        handlers = _effect_handlers(repository)

        # A message for an event type the slice does not own.
        unknown = EventEnvelope(
            event_id=uuid.uuid5(uuid.UUID(int=0), "unknown-event"),
            event_type="frame.detected",
            event_time=utc_now(),
            produced_at=utc_now(),
            source="cv.detector",
            payload={"count": 1},
        )
        pipeline.stream.append({
            "id": "unknown-1",
            "event": unknown.model_dump_json(),
            "event_id": str(unknown.event_id),
            "event_type": unknown.event_type,
            "tenant_id": str(outcome.events[0].payload.tenant_id),
        })
        assert pipeline.relay_once() == 1

        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 0
        row = next(iter(pipeline.inbox.values()))
        assert row.status == "dead_letter"
        assert "no effect handler registered" in (row.last_error or "")
        assert repository.rows == {}  # no effect, no evidence
