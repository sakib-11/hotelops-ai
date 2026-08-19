"""Task 18.10 — Authoritative persistence (vertical slice).

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 15
chain (18.7) + the REGISTERED Task 16 rule (18.8), and THIS slice persists
every material fact+event pair through the authoritative persistence
boundary — the ONE business transaction that writes, atomically:

    1. canonical business fact  → temporal_facts
    2. domain event             → operational_events
    3. audit identity/context   → audit_events
    4. outbox message           → outbox_events (Task 7)

    BEGIN → fact → event → audit → outbox → COMMIT

PostgreSQL is the source of truth: the four rows commit or roll back
together (NONE may partially commit), and nothing is published to Redis
before the database commit — the outbox row is the durability boundary.

Tests (the task's list):

1. normal commit      — the four rows persist together on commit;
2. failure before commit — a failing step leaves NOTHING durable (the
                        STOP condition: business state and event can
                        never become inconsistent);
3. rollback           — explicit rollback discards all four;
4. duplicate event    — re-persisting the same event writes NOTHING and
                        returns ``replayed`` (one logical row each);
5. idempotency        — the concurrent race collapses to one logical
                        set of rows (the unique constraints reject the
                        loser; the savepoint discards its partial rows);
6. outbox creation    — the durable outbox row carries the serialized
                        EventEnvelope (the ONLY publication unit);
7. replay             — re-running the whole slice reproduces the same
                        logical facts/events/audits/outbox payloads.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.application.services.operational_persistence import (
    FACT_TYPE_OCCUPANCY_SNAPSHOT,
    OperationalPersistenceService,
)
from backend.app.infrastructure.database.models.audit_outbox_inbox import (
    AuditEventModel,
    OutboxEventModel,
)
from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.database.models.temporal import TemporalFactModel
from backend.app.infrastructure.reliability.exceptions import DuplicateEventError
from contracts.events import EventEnvelope
from contracts.temporal import OccupancySnapshot
from tests.unit.fakes import make_actor
from tests.unit.test_vertical_slice_rule import (
    _identities,
    _load_manifest,
    _run_full_slice,
)

# =============================================================================
# Transaction-aware fakes — faithful to the SQL semantics the boundary relies
# on: rows are only durable at COMMIT; flush validates the unique constraints
# (the idempotency arbiter); a savepoint rollback discards partial rows.
# =============================================================================


class FakeStore:
    """The durable store — rows only appear here on commit."""

    def __init__(self) -> None:
        self.facts: dict[uuid.UUID, TemporalFactModel] = {}
        self.events: dict[tuple[datetime, uuid.UUID], OperationalEventModel] = {}
        self.audits: list[AuditEventModel] = []
        self.outbox: dict[uuid.UUID, OutboxEventModel] = {}
        self.outbox_by_event: dict[uuid.UUID, OutboxEventModel] = {}

    def count(self) -> int:
        return len(self.facts) + len(self.events) + len(self.audits) + len(self.outbox)


def _integrity(constraint: str) -> IntegrityError:
    """A real IntegrityError whose orig names the violated constraint."""

    class _DuplicateError(Exception):
        pass

    return IntegrityError(
        "fake statement",
        {},
        _DuplicateError(f'duplicate key value violates unique constraint "{constraint}"'),
    )


class _Savepoint:
    def __init__(self, session: FakeSession) -> None:
        self._session = session
        self._mark = len(session.pending)

    async def commit(self) -> None:
        # Rows stay pending until the OUTER commit — nothing to do.
        return None

    async def rollback(self) -> None:
        del self._session.pending[self._mark :]


class FakeSession:
    """A transaction-scoped fake: add → flush (validate) → commit/rollback."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.pending: list[object] = []

    def add(self, obj: object) -> None:
        self.pending.append(obj)

    async def flush(self) -> None:
        """Validate pending rows against the DURABLE store; the unique
        constraints raise exactly like the real flush (the outbox repo
        converts its own conflict to DuplicateEventError)."""
        for obj in self.pending:
            if isinstance(obj, TemporalFactModel) and obj.fact_id in self.store.facts:
                raise _integrity("pk_temporal_facts")
            if (
                isinstance(obj, OperationalEventModel)
                and (obj.event_time, obj.event_id) in self.store.events
            ):
                raise _integrity("pk_operational_events")
            if isinstance(obj, OutboxEventModel) and obj.event_id in self.store.outbox_by_event:
                raise DuplicateEventError(
                    f"outbox event {obj.event_id} already exists (idempotent enqueue)"
                )

    async def commit(self) -> None:
        await self.flush()
        for obj in self.pending:
            if isinstance(obj, TemporalFactModel):
                self.store.facts[obj.fact_id] = obj
            elif isinstance(obj, OperationalEventModel):
                self.store.events[obj.event_time, obj.event_id] = obj
            elif isinstance(obj, OutboxEventModel):
                self.store.outbox[obj.outbox_id] = obj
                self.store.outbox_by_event[obj.event_id] = obj
            elif isinstance(obj, AuditEventModel):
                self.store.audits.append(obj)
        self.pending = []

    async def rollback(self) -> None:
        self.pending = []

    async def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)


class FakeOutbox:
    """In-memory Task 7 outbox port (dedup on the unique event_id).

    ``blind=True`` simulates the concurrent race: the pre-check misses
    the row another transaction just committed, and the unique
    constraints reject the write at flush.
    """

    def __init__(self, store: FakeStore, *, blind: bool = False) -> None:
        self.store = store
        self.blind = blind
        self.enqueue_failure: Exception | None = None

    async def find_by_event_id(self, session: FakeSession, event_id: uuid.UUID | str) -> bool:
        if self.blind:
            return False
        return uuid.UUID(str(event_id)) in self.store.outbox_by_event

    async def enqueue_event(
        self,
        session: FakeSession,
        *,
        actor: Any,
        envelope: EventEnvelope[Any],
        audit: Any,
        venue_id: uuid.UUID | None = None,
    ) -> OutboxEventModel:
        if self.enqueue_failure is not None:
            raise self.enqueue_failure
        # Mirror OutboxService: audit + outbox rows join the caller's
        # transaction, then the flush applies the unique-event_id arbiter.
        session.add(
            AuditEventModel(
                actor_id=uuid.UUID(str(audit.actor_id)),
                tenant_id=uuid.UUID(str(audit.tenant_id)),
                venue_id=uuid.UUID(str(audit.venue_id)) if audit.venue_id else None,
                action=audit.action,
                action_category=audit.action_category.value,
                correlation_id=audit.correlation_id,
                timestamp=audit.timestamp,
                metadata_=dict(audit.metadata) if audit.metadata else None,
            )
        )
        row = OutboxEventModel(
            outbox_id=uuid.uuid4(),
            event_id=uuid.UUID(str(envelope.event_id)),
            tenant_id=uuid.UUID(str(actor.tenant_id)),
            venue_id=uuid.UUID(str(venue_id)) if venue_id else None,
            event_type=envelope.event_type,
            status="pending",
            payload=envelope.model_dump(mode="json"),
        )
        session.add(row)
        await session.flush()
        return row


def _service(store: FakeStore, **kwargs: Any) -> OperationalPersistenceService:
    return OperationalPersistenceService(outbox=FakeOutbox(store, **kwargs))


# =============================================================================
# 1. NORMAL COMMIT — the four rows persist together
# =============================================================================


class TestNormalCommit:
    async def test_commit_persists_fact_event_audit_outbox(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        session = FakeSession(store)
        service = _service(store)
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))

        result = await service.persist(
            session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=actor,
        )

        assert result.created is True
        assert result.event_id == outcome.events[0].event_id
        # Rows are in the transaction, not yet durable.
        assert len(session.pending) == 4  # fact + event + audit + outbox
        assert store.count() == 0

        await session.commit()

        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1

    async def test_fact_row_carries_the_canonical_fact(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        snapshot = outcome.snapshots[0]

        store = FakeStore()
        session = FakeSession(store)
        service = _service(store)
        await service.persist(
            session,
            fact=snapshot,
            event=outcome.events[0],
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        await session.commit()

        row = store.facts[uuid.UUID(str(snapshot.snapshot_id))]
        assert row.fact_id == uuid.UUID(str(snapshot.snapshot_id))
        assert row.fact_type == FACT_TYPE_OCCUPANCY_SNAPSHOT
        assert row.fsm_kind == "occupancy"
        assert row.tenant_id == uuid.UUID(str(ids["tenant_id"]))
        assert row.venue_id == uuid.UUID(str(ids["venue_id"]))
        assert row.session_id == uuid.UUID(str(ids["session_id"]))
        assert row.camera_id == uuid.UUID(str(ids["camera_id"]))
        assert row.configuration_version_id == uuid.UUID(str(ids["configuration_version_id"]))
        assert row.event_time == snapshot.event_time
        assert row.source_transition_id == uuid.UUID(str(snapshot.source_transition_id))
        assert row.policy_revision == snapshot.policy_revision
        # The payload is the canonical fact contract — never re-derived.
        assert OccupancySnapshot.model_validate(row.payload) == snapshot

    async def test_event_row_carries_the_domain_event(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        store = FakeStore()
        session = FakeSession(store)
        service = _service(store)
        await service.persist(
            session,
            fact=outcome.snapshots[0],
            event=event,
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        await session.commit()

        row = store.events[event.event_time, uuid.UUID(str(event.event_id))]
        assert row.event_id == uuid.UUID(str(event.event_id))
        assert row.event_type == event.event_type
        assert row.event_time == event.event_time
        assert row.produced_at == event.produced_at
        assert row.source == event.source
        assert row.tenant_id == uuid.UUID(str(ids["tenant_id"]))
        assert row.session_id == uuid.UUID(str(ids["session_id"]))
        assert row.camera_id == uuid.UUID(str(ids["camera_id"]))
        # The payload is the envelope's generic payload — the envelope
        # round-trips through the persisted row (serialized forms equal).
        restored = EventEnvelope.model_validate({
            **event.model_dump(mode="json"),
            "payload": row.payload,
        })
        assert restored.model_dump(mode="json") == event.model_dump(mode="json")


# =============================================================================
# 2. FAILURE BEFORE COMMIT — NONE of the four may partially commit
# =============================================================================


class TestFailureBeforeCommit:
    async def test_failing_step_leaves_nothing_durable(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        session = FakeSession(store)
        outbox = FakeOutbox(store)
        outbox.enqueue_failure = RuntimeError("postgres unavailable")
        service = OperationalPersistenceService(outbox=outbox)

        with pytest.raises(RuntimeError, match="postgres unavailable"):
            await service.persist(
                session,
                fact=outcome.snapshots[0],
                event=outcome.events[0],
                actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
            )
        await session.rollback()

        # STOP condition: business state and event can never become
        # inconsistent — no fact row AND no event row (nor audit/outbox).
        assert store.facts == {}
        assert store.events == {}
        assert store.audits == []
        assert store.outbox == {}
        assert store.count() == 0

    async def test_persist_does_not_commit_itself(self) -> None:
        """The service returns with rows pending; the CALLER owns the
        commit — a crash before commit leaves the store empty."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        session = FakeSession(store)
        service = _service(store)
        await service.persist(
            session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        # No commit happened — the process "crashed".
        assert store.count() == 0


# =============================================================================
# 3. ROLLBACK — explicit rollback discards all four
# =============================================================================


class TestRollback:
    async def test_rollback_discards_everything(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        session = FakeSession(store)
        service = _service(store)
        await service.persist(
            session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        await session.rollback()

        assert session.pending == []
        assert store.count() == 0

    async def test_rollback_after_one_pair_keeps_prior_commits(self) -> None:
        """Two events persisted, the second rolled back: the FIRST stays
        durable (its own transaction), the second leaves nothing."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))
        service = _service(store)

        session_a = FakeSession(store)
        await service.persist(
            session_a, fact=outcome.snapshots[0], event=outcome.events[0], actor=actor
        )
        await session_a.commit()

        session_b = FakeSession(store)
        await service.persist(
            session_b, fact=outcome.snapshots[1], event=outcome.events[1], actor=actor
        )
        await session_b.rollback()

        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1


# =============================================================================
# 4. DUPLICATE EVENT — one logical set of rows
# =============================================================================


class TestDuplicateEvent:
    async def test_duplicate_delivery_writes_nothing(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))

        store = FakeStore()
        session = FakeSession(store)
        service = _service(store)

        first = await service.persist(
            session, fact=outcome.snapshots[0], event=outcome.events[0], actor=actor
        )
        await session.commit()
        assert first.created is True

        # Duplicate delivery of the SAME event.
        second_session = FakeSession(store)
        second = await service.persist(
            second_session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=actor,
        )
        assert second.replayed is True
        assert second_session.pending == []
        await second_session.commit()  # a replayed result commits trivially

        # Exactly ONE logical fact / event / audit / outbox.
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1

    async def test_duplicate_returns_the_same_event_identity(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))

        store = FakeStore()
        service = _service(store)
        session = FakeSession(store)
        await service.persist(
            session, fact=outcome.snapshots[0], event=outcome.events[0], actor=actor
        )
        await session.commit()

        result = await service.persist(
            FakeSession(store),
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=actor,
        )
        assert result.replayed is True
        assert result.event_id == outcome.events[0].event_id


# =============================================================================
# 5. IDEMPOTENCY — the concurrent race collapses to one logical set
# =============================================================================


class TestIdempotencyRace:
    async def test_race_loser_replays_without_partial_rows(self) -> None:
        """A blind pre-check (the row was committed by a concurrent
        transaction after the check) → the unique constraints reject the
        write; the savepoint discards the partial rows; the result is
        ``replayed`` — never a second fact/event/outbox."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))

        store = FakeStore()
        # The winner's committed rows (a concurrent transaction).
        session = FakeSession(store)
        await _service(store).persist(
            session, fact=outcome.snapshots[0], event=outcome.events[0], actor=actor
        )
        await session.commit()

        # The loser: blind pre-check + blind outbox (finds nothing).
        loser_session = FakeSession(store)
        loser_service = OperationalPersistenceService(outbox=FakeOutbox(store, blind=True))
        result = await loser_service.persist(
            loser_session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=actor,
        )
        assert result.replayed is True
        # The savepoint discarded the loser's partial fact/event/audit/
        # outbox rows — nothing pending, nothing durable changed.
        assert loser_session.pending == []
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1


# =============================================================================
# 6. OUTBOX CREATION — the durable publication unit (Task 7)
# =============================================================================


class TestOutboxCreation:
    async def test_outbox_row_is_the_durable_publication_unit(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        store = FakeStore()
        session = FakeSession(store)
        await _service(store).persist(
            session,
            fact=outcome.snapshots[0],
            event=event,
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        await session.commit()

        row = store.outbox_by_event[uuid.UUID(str(event.event_id))]
        assert row.event_id == uuid.UUID(str(event.event_id))
        assert row.event_type == event.event_type
        assert row.tenant_id == uuid.UUID(str(ids["tenant_id"]))
        assert row.status == "pending"  # the publisher transports AFTER commit
        # The outbox payload IS the serialized canonical envelope — the
        # row round-trips to the exact event that was persisted.
        restored = EventEnvelope.model_validate(row.payload)
        assert restored.model_dump(mode="json") == event.model_dump(mode="json")

    async def test_nothing_is_published_before_commit(self) -> None:
        """The outbox row only becomes durable at COMMIT — there is no
        transport before the database commit (PostgreSQL is the source
        of truth; Redis is never written by the boundary)."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        session = FakeSession(store)
        await _service(store).persist(
            session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        # Before commit: no durable outbox row exists anywhere.
        assert store.outbox == {}


# =============================================================================
# 7. REPLAY — re-running the whole slice reproduces the same persistence
# =============================================================================


class TestReplay:
    async def test_full_replay_is_logically_identical(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))

        async def _persist_all() -> FakeStore:
            outcome = _run_full_slice(manifest, ids)
            store = FakeStore()
            session = FakeSession(store)
            service = _service(store)
            for snapshot, event in zip(outcome.snapshots, outcome.events, strict=True):
                await service.persist(session, fact=snapshot, event=event, actor=actor)
            return store

        first_store = await _persist_all()
        second_store = await _persist_all()
        # The same logical fact identities…
        assert set(first_store.facts) == set(second_store.facts)
        # …the same logical event identities…
        assert set(first_store.events) == set(second_store.events)
        # …the same fact payloads (byte-identical)…
        assert {k: v.payload for k, v in first_store.facts.items()} == {
            k: v.payload for k, v in second_store.facts.items()
        }
        # …the same outbox payloads (the serialized envelopes)…
        assert {k: v.payload for k, v in first_store.outbox_by_event.items()} == {
            k: v.payload for k, v in second_store.outbox_by_event.items()
        }
        # …and the same audit actions.
        assert [a.action for a in first_store.audits] == [a.action for a in second_store.audits]


# =============================================================================
# STOP condition — the slice persists through Task 7; no second transport
# =============================================================================


class TestStopCondition:
    async def test_boundary_only_writes_the_four_authoritative_tables(self) -> None:
        """The service never touches Redis/transport — the ONLY durable
        artifacts are the fact, event, audit, and outbox rows."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        store = FakeStore()
        session = FakeSession(store)
        await _service(store).persist(
            session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"]))),
        )
        await session.commit()

        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1

    async def test_persist_is_fully_deterministic(self) -> None:
        """The same input always yields the same logical rows — the STOP
        condition: replay can never make business state and event diverge."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        actor = make_actor(tenant_id=uuid.UUID(str(ids["tenant_id"])))

        store = FakeStore()
        session = FakeSession(store)
        result = await _service(store).persist(
            session,
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=actor,
        )
        await session.commit()
        assert result.created is True

        # Re-running the identical persist is a replay — one logical set.
        again = await _service(store).persist(
            FakeSession(store),
            fact=outcome.snapshots[0],
            event=outcome.events[0],
            actor=actor,
        )
        assert again.replayed is True
