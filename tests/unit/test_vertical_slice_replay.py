"""Task 18.15 — VERTICAL SLICE REPLAY / IDEMPOTENCY TEST.

Replay the EXACT same fixture/event multiple times (Replay #1, #2, #3) and
verify the whole vertical slice collapses to ONE logical fact/event/effect:

    full E2E chain (18.14) — the SAME fixture, run 3 times → the SAME
        content-derived event identity, fact identity, and evidence request
        identity (never a second logical event);
    duplicate EventEnvelope — the same (fact, event) pair persisted into the
        SAME store 3 times → exactly ONE fact row, ONE event row, ONE audit
        row, ONE outbox row (Task 7's unique event_id is the arbiter);
    duplicate outbox message — the same envelope reaches the Redis stream
        twice (at-least-once redelivery) → the inbox dedups on
        (source, source_message_id) → ONE inbox row → ONE logical effect;
    duplicate evidence request — the same event linked 3 times → ONE logical
        request (the PK IS the content-derived ref_id);
    duplicate API query — the same retrieval 3 times → the SAME canonical
        DTO (there is only one logical record behind it).

Expected outcomes (the task's list):

- ONE logical occupancy fact/event               → Replay #1/#2/#3 identity
  is byte-identical and a duplicate persist is ``replayed`` (writes nothing);
- ONE logical business effect                    → one inbox row, one effect
  run, one durable evidence request;
- ONE logical evidence request/package           → one ref_id row;
- no duplicate business records                  → facts/events/outbox == 1;
- no duplicate audit side effects                → exactly one audit row (the
  audit is created inside the authoritative transaction; a replayed persist
  writes no audit);
- Task 7 is actually enforcing this              → the unique event_id
  arbiter rejects a duplicate enqueue even when the idempotency PRE-CHECK
  misses (the blind concurrent race) — the constraint, not the check, is
  the arbiter; the inbox dedup key and the ref_id PK are content-derived.

STOP condition: replay can never create a duplicate logical record anywhere
in the slice (the final test counts EVERY boundary).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.application.services.operational_persistence import (
    OperationalPersistenceService,
)
from backend.app.workers.operational_effects import build_operational_effect_handlers
from contracts.common import EventId
from contracts.events import EventEnvelope
from contracts.temporal import OccupancySnapshot
from tests.unit.test_vertical_slice_api import FakeSession as ApiSession
from tests.unit.test_vertical_slice_e2e import (
    E2ERun,
    _install_deterministic_seams,
    _run_e2e,
)
from tests.unit.test_vertical_slice_evidence import FakeEvidenceLinkageRepository
from tests.unit.test_vertical_slice_outbox import FakePipeline
from tests.unit.test_vertical_slice_persistence import (
    FakeOutbox as PersistenceOutbox,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeSession as PersistenceSession,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeStore,
)

pytestmark = pytest.mark.e2e


class _NoopSession:
    """The consumer transaction handed to the effect handler — the fake
    evidence repository owns all dedup semantics and never touches the
    session (same convention as the 18.11 outbox test)."""


@pytest.fixture(autouse=True)
def _deterministic_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SAME deterministic seams as the 18.14 E2E (fake SDKs behind the
    adapters' lazy seams + deterministic frame ids) so every replay of the
    full chain is reproducible under identical conditions."""
    _install_deterministic_seams(monkeypatch)


def _one_event(run: E2ERun) -> EventEnvelope[Any]:
    """The single logical occupancy event of one full-chain run."""
    assert run.event is not None
    assert len(run.events) == 1
    return run.event


def _one_snapshot(run: E2ERun) -> OccupancySnapshot:
    """The single logical occupancy fact (snapshot) of one full-chain run."""
    assert len(run.snapshots) == 1
    return run.snapshots[0]


# =============================================================================
# Replay #1 / #2 / #3 — the same fixture run three times → ONE logical event
# =============================================================================


class TestFullChainReplays:
    """The full deterministic chain is replayed three times with fresh
    engines, stores, and workers. Because every identity is content-derived
    (deterministic frame ids → deterministic transitions → deterministic
    snapshot → deterministic event), all three replays are the SAME logical
    event, fact, and evidence request."""

    async def test_three_full_replays_produce_one_logical_identity(self) -> None:
        first = await _run_e2e()
        second = await _run_e2e()
        third = await _run_e2e()

        # ONE logical event: the same content-derived identity + payload.
        first_event = _one_event(first)
        assert first_event.event_id == _one_event(second).event_id == _one_event(third).event_id
        assert (
            first_event.model_dump_json()
            == _one_event(second).model_dump_json()
            == _one_event(third).model_dump_json()
        )
        # ONE logical fact: the same snapshot identity + payload.
        first_snapshot = _one_snapshot(first)
        assert (
            first_snapshot.snapshot_id
            == _one_snapshot(second).snapshot_id
            == _one_snapshot(third).snapshot_id
        )
        assert (
            first_snapshot.model_dump(mode="json")
            == _one_snapshot(second).model_dump(mode="json")
            == _one_snapshot(third).model_dump(mode="json")
        )
        # ONE logical evidence request: the same content-derived ref_id.
        first_ref = next(iter(first.evidence.rows.values())).ref_id
        second_ref = next(iter(second.evidence.rows.values())).ref_id
        third_ref = next(iter(third.evidence.rows.values())).ref_id
        assert first_ref == second_ref == third_ref
        assert first_ref.version == 5  # the Task 7 content-derived scheme


# =============================================================================
# Duplicate EventEnvelope — the same (fact, event) persisted 3 times
# =============================================================================


class TestReplayPersistence:
    """The same logical event is persisted three times into the SAME store.
    Replay #2 and #3 are ``replayed`` (writes NOTHING), so exactly ONE fact,
    ONE event, ONE audit, and ONE outbox row are durable."""

    async def test_duplicate_persists_are_replayed_and_write_nothing(self) -> None:
        first = await _run_e2e()
        second = await _run_e2e()
        third = await _run_e2e()

        store = first.store  # replay #1's rows are already committed
        event = _one_event(first)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        for run in (second, third):
            session = PersistenceSession(store)
            result = await service.persist(
                session,
                fact=_one_snapshot(run),
                event=_one_event(run),
                actor=first.actor,
            )
            # Replay #2/#3 are replayed — NOTHING was written, the pre-check
            # (the durable outbox row) already proves the event exists.
            assert result.replayed is True
            assert session.pending == []
            await session.commit()

        # ONE logical business record per table — no duplicates anywhere.
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.outbox) == 1
        # The audit is created inside the authoritative transaction; a
        # replayed persist writes no audit — exactly ONE audit row.
        assert len(store.audits) == 1
        assert store.audits[0].action == "operational.event.persisted"
        # The durable rows are still the FIRST replay's logical records.
        assert uuid.UUID(str(event.event_id)) in store.outbox_by_event
        assert len(store.outbox_by_event) == 1

    async def test_task7_unique_event_id_arbiter_wins_even_blind(self) -> None:
        """Task 7 enforcement: the idempotency PRE-CHECK is not the arbiter —
        when it misses (the concurrent race: the winner committed between the
        check and the write), the durable outbox row's UNIQUE event_id
        constraint rejects the loser and its savepoint discards the partial
        rows. Exactly one logical set survives."""
        first = await _run_e2e()
        event = _one_event(first)
        snapshot = _one_snapshot(first)

        store = FakeStore()  # a fresh store — as if the replay lands blind
        # The winner's logical persist commits.
        winner_session = PersistenceSession(store)
        winner = await OperationalPersistenceService(outbox=PersistenceOutbox(store)).persist(
            winner_session,
            fact=snapshot,
            event=event,
            actor=first.actor,
        )
        assert winner.created is True
        await winner_session.commit()

        # The loser: blind pre-check (finds nothing) → the unique event_id
        # constraint rejects the duplicate; the savepoint discards its
        # partial fact/event/audit/outbox rows.
        loser_session = PersistenceSession(store)
        loser_service = OperationalPersistenceService(outbox=PersistenceOutbox(store, blind=True))
        result = await loser_service.persist(
            loser_session,
            fact=snapshot,
            event=event,
            actor=first.actor,
        )
        assert result.replayed is True
        assert loser_session.pending == []
        await loser_session.commit()

        # ONE logical set survived the race — no duplicates, no partial rows.
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1


# =============================================================================
# Duplicate outbox message — at-least-once redelivery → ONE logical effect
# =============================================================================


class TestDuplicateOutboxMessage:
    """The same envelope reaches the Redis stream TWICE (the publisher
    crashes after publishing, the lease expires, and it re-publishes — the
    Task 7 at-least-once guarantee). The ingress bridge dedups on
    (source, source_message_id), so ONE inbox row and ONE logical effect
    survive."""

    async def test_at_least_once_redelivery_is_one_logical_effect(self) -> None:
        run = await _run_e2e()
        event = _one_event(run)

        # Re-persist the SAME logical event into a FRESH store — a second
        # delivery location — to obtain fresh PENDING outbox rows (the
        # E2E's own pipeline already marked its rows published).
        delivery_store = FakeStore()
        session = PersistenceSession(delivery_store)
        await OperationalPersistenceService(outbox=PersistenceOutbox(delivery_store)).persist(
            session,
            fact=_one_snapshot(run),
            event=event,
            actor=run.actor,
        )
        await session.commit()

        pipeline = FakePipeline()
        pipeline.seed_outbox(delivery_store)
        evidence = FakeEvidenceLinkageRepository()
        handlers = build_operational_effect_handlers(
            evidence_linkage=EvidenceLinkageService(repository=evidence),
        )

        # Publish, crash before marking published, lease expires, re-publish:
        # the SAME envelope reaches the stream twice (at-least-once).
        assert pipeline.publish_once("publisher-a", crash_after_publish=True) == 0
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-a") == 1
        assert len(pipeline.stream) == 2

        # The bridge dedups on (source, event_id): TWO stream messages →
        # ONE inbox row, both acknowledged.
        assert pipeline.relay_once() == 2
        assert len(pipeline.inbox) == 1

        # The consumer runs the effect once → ONE logical evidence request.
        assert await pipeline.consume_once("consumer-a", handlers=handlers) == 1
        assert len(evidence.rows) == 1
        (ref_row,) = evidence.rows.values()
        assert ref_row.event_id == uuid.UUID(str(event.event_id))
        # The outbox row was processed exactly once end to end.
        assert {row.status for row in pipeline.outbox.values()} == {"published"}
        assert {row.status for row in pipeline.inbox.values()} == {"processed"}


# =============================================================================
# Duplicate evidence request — the same event linked 3 times
# =============================================================================


class TestDuplicateEvidenceRequest:
    """Linking the SAME event three times yields ONE logical request: the
    request's primary key IS the content-derived ref_id, so the second and
    third links collapse to the existing row (Task 7 idempotency)."""

    async def test_three_links_are_one_logical_request(self) -> None:
        run = await _run_e2e()
        event = _one_event(run)

        repository = FakeEvidenceLinkageRepository()
        service = EvidenceLinkageService(repository=repository)
        rows = []
        for _ in range(3):
            row = await service.link_event(_NoopSession(), event)
            assert row is not None
            rows.append(row)

        # ONE logical request: the same row identity, never a duplicate.
        assert len(repository.rows) == 1
        assert rows[0].ref_id == rows[1].ref_id == rows[2].ref_id
        assert rows[0] is rows[1] is rows[2]
        # The request carries the full provenance of the logical event.
        assert rows[0].event_id == uuid.UUID(str(event.event_id))
        assert rows[0].metadata_["processing_state"] == "requested"


# =============================================================================
# Duplicate API query — the same retrieval 3 times → the same canonical DTO
# =============================================================================


class TestDuplicateApiQuery:
    """Querying the retrieval surface three times returns the SAME canonical
    result — there is only one logical record behind it (the desktop can
    never observe a duplicate)."""

    async def test_three_queries_return_the_same_canonical_result(self) -> None:
        run = await _run_e2e()
        event = _one_event(run)

        api_session = ApiSession(
            events={row.event_id: row for row in run.store.events.values()},
            facts={row.fact_id: row for row in run.store.facts.values()},
            evidence={ref.event_id: ref for ref in run.evidence.rows.values()},
        )
        event_id = EventId(event.event_id)

        from backend.app.api.routes.operational import (
            get_operational_event,
            get_operational_event_evidence,
        )

        responses = [
            await get_operational_event(
                event_id=event_id, actor=run.actor, _perm=None, session=api_session
            )
            for _ in range(3)
        ]
        assert (
            responses[0].model_dump(mode="json")
            == responses[1].model_dump(mode="json")
            == responses[2].model_dump(mode="json")
        )
        assert responses[0].event_id == event_id
        assert responses[0].payload.occupancy_count == 1

        availability = [
            await get_operational_event_evidence(
                event_id=event_id, actor=run.actor, _perm=None, session=api_session
            )
            for _ in range(3)
        ]
        assert availability[0].available is True
        assert (
            availability[0].evidence_ref_id
            == availability[1].evidence_ref_id
            == availability[2].evidence_ref_id
        )

        # Exactly ONE logical record behind every query.
        assert len(run.store.events) == 1
        assert len(run.evidence.rows) == 1


# =============================================================================
# STOP condition — replay never creates a duplicate anywhere
# =============================================================================


class TestStopCondition:
    async def test_replay_never_creates_duplicates_anywhere(self) -> None:
        """The full sweep: three full-chain replays + duplicate persists +
        duplicate outbox delivery + duplicate evidence linkage collapse to
        exactly ONE of every logical record across EVERY boundary."""
        first = await _run_e2e()
        second = await _run_e2e()
        third = await _run_e2e()

        # Replays #2/#3 persisted into replay #1's store → replayed.
        store = first.store
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        for run in (second, third):
            session = PersistenceSession(store)
            result = await service.persist(
                session,
                fact=_one_snapshot(run),
                event=_one_event(run),
                actor=first.actor,
            )
            assert result.replayed is True
            await session.commit()

        # The duplicate outbox delivery is modelled against a FRESH store:
        # the same logical event re-persisted (a second delivery location)
        # gives fresh PENDING outbox rows to crash/re-deliver.
        delivery_store = FakeStore()
        session = PersistenceSession(delivery_store)
        await OperationalPersistenceService(outbox=PersistenceOutbox(delivery_store)).persist(
            session,
            fact=_one_snapshot(first),
            event=_one_event(first),
            actor=first.actor,
        )
        await session.commit()

        # Duplicate outbox delivery (at-least-once) → one inbox row → one
        # effect → one evidence request.
        pipeline = FakePipeline()
        pipeline.seed_outbox(delivery_store)
        evidence = FakeEvidenceLinkageRepository()
        handlers = build_operational_effect_handlers(
            evidence_linkage=EvidenceLinkageService(repository=evidence),
        )
        assert pipeline.publish_once("publisher-x", crash_after_publish=True) == 0
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-x") == 1
        assert pipeline.relay_once() == 2
        assert await pipeline.consume_once("consumer-x", handlers=handlers) == 1

        # Duplicate evidence linkage of the same event.
        service_link = EvidenceLinkageService(repository=evidence)
        for _ in range(2):
            await service_link.link_event(_NoopSession(), _one_event(first))

        # STOP: ONE of every logical record — no duplicates anywhere, on
        # the replay store AND the delivery store.
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1
        assert len(delivery_store.facts) == 1
        assert len(delivery_store.events) == 1
        assert len(delivery_store.audits) == 1
        assert len(delivery_store.outbox) == 1
        assert len(pipeline.inbox) == 1
        assert len(evidence.rows) == 1
