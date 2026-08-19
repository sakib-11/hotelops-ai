"""Task 7 security tests — tenant/venue isolation (Phase 12).

DB-free service-level attack tests:

  - Tenant A cannot observe or replay Tenant B's idempotency records,
    even when the request payload carries a foreign tenant id (the
    service scopes every lookup/claim by the ActorContext tenant).
  - A venue-scoped actor cannot create/replay records or enqueue events
    for venues outside its scope (AuthorizationError before any write).
  - Outbox events derive tenant/venue identity EXCLUSIVELY from the
    trusted ActorContext — a client cannot smuggle tenant identity into
    the envelope (the contract has no such field, extra=forbid) or into
    the persisted row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.app.application.services.idempotency import (
    IdempotencyService,
)
from backend.app.application.services.outbox import OutboxService, serialize_envelope
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from contracts.audit import AuditActionCategory
from contracts.events import EventEnvelope
from contracts.identity import ActorContext
from tests.unit.fakes import FakeIdempotencyRepository, make_actor

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VENUE_A = uuid.UUID("00000000-0000-0000-0000-000000000020")
_VENUE_B = uuid.UUID("00000000-0000-0000-0000-000000000021")


def _venue_scoped_actor(venue: uuid.UUID) -> ActorContext:
    return make_actor(tenant_id=_TENANT_A, venue_scope=frozenset({venue}))


def _service(repo: FakeIdempotencyRepository) -> IdempotencyService:
    return IdempotencyService(  # type: ignore[call-arg]
        None,
        repository_factory=lambda _session: repo,
        lease_seconds=30,
        wait_timeout=2.0,
        wait_poll=0.001,
    )


class TestIdempotencyTenantIsolation:
    async def test_foreign_tenant_in_payload_cannot_redirect_lookup(self) -> None:
        """The request payload may name ANY tenant — the service still
        scopes the idempotency unit by the ACTOR's tenant."""
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor_a = make_actor(tenant_id=_TENANT_A)

        async def handler(_session, request) -> dict:
            # The handler sees the payload, but the idempotency record
            # is created under the actor's tenant regardless.
            return {"spoofed": request["tenant_id"]}

        await service.execute(
            object(),
            actor=actor_a,
            operation="op",
            key="spoof-key",
            request={"tenant_id": str(_TENANT_B)},  # client spoofs tenant B
            handler=handler,
        )
        assert len(repo.records) == 1
        record = next(iter(repo.records.values()))
        assert record.tenant_id == _TENANT_A, "record must live under the ACTOR's tenant"
        # Every repository call was scoped by the actor's tenant
        for name, kwargs in repo.calls:
            if name in ("get", "create_claim"):
                assert kwargs["tenant_id"] == _TENANT_A

    async def test_tenant_b_cannot_replay_tenant_a_result(self) -> None:
        """Same key in Tenant B is a DIFFERENT idempotency unit — Tenant
        B can never receive Tenant A's stored result."""
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor_a = make_actor(tenant_id=_TENANT_A)
        actor_b = make_actor(tenant_id=_TENANT_B)

        async def handler(_session, request) -> dict:
            return {"owner": str(request["owner"])}

        async def run(actor, owner):
            async def h(session, request):
                return await handler(session, request)

            return await service.execute(
                object(),
                actor=actor,
                operation="op",
                key="same-key",
                request={"owner": owner},
                handler=h,
            )

        a = await run(actor_a, "A")
        b = await run(actor_b, "B")
        assert a.result == {"owner": "A"}
        assert b.result == {"owner": "B"}
        assert b.replayed is False, "Tenant B must execute, never replay Tenant A's result"


class TestVenueIsolation:
    async def test_idempotency_out_of_scope_venue_denied(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor = _venue_scoped_actor(_VENUE_A)

        async def handler(_session, _request) -> dict:
            return {"ok": True}

        with pytest.raises(AuthorizationError, match="No access to venue"):
            await service.execute(
                object(),
                actor=actor,
                operation="op",
                key="k",
                request={},
                handler=handler,
                venue_id=_VENUE_B,
            )
        assert len(repo.records) == 0, "no record may be created for a denied venue"

    async def test_venue_b_cannot_replay_venue_a_record(self) -> None:
        """A key bound to Venue A is a conflict for Venue B — never a
        replay of Venue A's result."""
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor_all = make_actor(tenant_id=_TENANT_A)

        async def handler(_session, _request) -> dict:
            return {"venue_owner": "A"}

        await service.execute(
            object(),
            actor=actor_all,
            operation="op",
            key="venue-key",
            request={},
            handler=handler,
            venue_id=_VENUE_A,
        )
        # A venue-B actor gets a conflict, not Venue A's result
        repo_b = FakeIdempotencyRepository()
        repo_b.records = repo.records  # share state as the real DB would
        service_b = _service(repo_b)

        from backend.app.infrastructure.reliability import IdempotencyConflictError

        actor_b = _venue_scoped_actor(_VENUE_B)
        with pytest.raises(IdempotencyConflictError, match="venue"):
            await service_b.execute(
                object(),
                actor=actor_b,
                operation="op",
                key="venue-key",
                request={},
                handler=handler,
                venue_id=_VENUE_B,
            )


class FakeAsyncSession:
    """A recording session: captures model adds, flush is a no-op."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class TestOutboxTenantVenueDerivation:
    def _envelope(self) -> EventEnvelope[dict]:
        now = datetime.now(UTC)
        return EventEnvelope[dict](
            event_id=uuid.uuid4(),
            event_type="operational.event",
            event_time=now,
            produced_at=now,
            source="test.pipeline",
            payload={"class_name": "person"},
        )

    def _audit(self, actor: ActorContext):
        return AuditEventBuilder.from_actor(
            actor=actor,
            action="event.enqueue",
            action_category=AuditActionCategory.SYSTEM,
        )

    def test_envelope_cannot_carry_tenant_identity(self) -> None:
        """The event contract has no tenant field — a client cannot
        smuggle tenant identity into the event (extra=forbid)."""
        data = serialize_envelope(self._envelope())
        assert "tenant_id" not in data
        assert "venue_id" not in data

    @pytest.mark.asyncio
    async def test_outbox_row_derives_tenant_from_actor_only(self) -> None:
        from backend.app.infrastructure.database.models.audit_outbox_inbox import (
            AuditEventModel,
            OutboxEventModel,
        )

        session = FakeAsyncSession()
        actor = make_actor(tenant_id=_TENANT_A)
        await OutboxService().enqueue_event(
            session,
            actor=actor,
            envelope=self._envelope(),
            audit=self._audit(actor),
        )
        outbox_row = next(o for o in session.added if isinstance(o, OutboxEventModel))
        audit_row = next(o for o in session.added if isinstance(o, AuditEventModel))
        assert outbox_row.tenant_id == _TENANT_A
        assert outbox_row.venue_id is None
        assert audit_row.tenant_id == _TENANT_A
        assert audit_row.actor_id == uuid.UUID(str(actor.actor_id))
        # The stored payload never contains tenant identity
        assert "tenant_id" not in outbox_row.payload

    @pytest.mark.asyncio
    async def test_outbox_venue_is_recorded_from_actor_context(self) -> None:
        from backend.app.infrastructure.database.models.audit_outbox_inbox import (
            OutboxEventModel,
        )

        session = FakeAsyncSession()
        actor = make_actor(tenant_id=_TENANT_A)
        await OutboxService().enqueue_event(
            session,
            actor=actor,
            envelope=self._envelope(),
            audit=self._audit(actor),
            venue_id=_VENUE_A,
        )
        outbox_row = next(o for o in session.added if isinstance(o, OutboxEventModel))
        assert outbox_row.venue_id == _VENUE_A

    @pytest.mark.asyncio
    async def test_out_of_scope_venue_denied_before_any_write(self) -> None:
        session = FakeAsyncSession()
        actor = _venue_scoped_actor(_VENUE_A)
        with pytest.raises(AuthorizationError, match="No access to venue"):
            await OutboxService().enqueue_event(
                session,
                actor=actor,
                envelope=self._envelope(),
                audit=self._audit(actor),
                venue_id=_VENUE_B,
            )
        assert session.added == [], "denied venue writes must persist nothing"
