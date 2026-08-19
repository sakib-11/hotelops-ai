"""Unit tests for the Task 7 idempotency service (Phase 11).

Covers the pure logic (canonical request hashing, key validation) and
the full decision loop against an in-memory repository that emulates the
PostgreSQL unique-key serialization — including CONCURRENT identical
requests (one execution) and concurrent conflicting payloads (one
execution + one conflict).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from backend.app.application.services.idempotency import (
    IdempotencyService,
    canonical_request_hash,
    validate_idempotency_key,
)
from backend.app.infrastructure.reliability.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyKeyError,
)
from tests.unit.fakes import FakeIdempotencyRepository, make_actor

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
_VENUE_A = uuid.UUID("00000000-0000-0000-0000-000000000020")
_VENUE_B = uuid.UUID("00000000-0000-0000-0000-000000000021")


def _service(
    repo: FakeIdempotencyRepository,
    *,
    wait_poll: float = 0.001,
    wait_timeout: float = 2.0,
) -> IdempotencyService:
    return IdempotencyService(  # type: ignore[call-arg]
        None,  # settings not used when explicit kwargs are given
        repository_factory=lambda _session: repo,
        lease_seconds=30,
        wait_timeout=wait_timeout,
        wait_poll=wait_poll,
    )


class TestCanonicalRequestHash:
    def test_same_content_different_key_order_same_hash(self) -> None:
        a = {"venue_id": "v1", "payload": {"count": 2, "name": "x"}, "tags": ["a", "b"]}
        b = {"tags": ["a", "b"], "payload": {"name": "x", "count": 2}, "venue_id": "v1"}
        assert canonical_request_hash(a) == canonical_request_hash(b)

    def test_different_payload_different_hash(self) -> None:
        assert canonical_request_hash({"count": 1}) != canonical_request_hash({"count": 2})

    def test_nested_structures_deterministic(self) -> None:
        a = {"level": {"deep": [1, 2, {"k": "v"}]}}
        b = {"level": {"deep": [1, 2, {"k": "v"}]}}
        assert canonical_request_hash(a) == canonical_request_hash(b)

    def test_pydantic_model_equals_dict(self) -> None:
        class Req(BaseModel):
            venue_id: str
            count: int

        model = Req(venue_id="v1", count=3)
        assert canonical_request_hash(model) == canonical_request_hash({
            "venue_id": "v1",
            "count": 3,
        })

    def test_returns_sha256_hex(self) -> None:
        digest = canonical_request_hash({"a": 1})
        assert len(digest) == 64
        int(digest, 16)  # valid hex


class TestIdempotencyKeyValidation:
    def test_valid_key_accepted(self) -> None:
        assert validate_idempotency_key("ABC123") == "ABC123"

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(IdempotencyKeyError):
            validate_idempotency_key("")

    def test_too_long_key_rejected(self) -> None:
        with pytest.raises(IdempotencyKeyError):
            validate_idempotency_key("x" * 129)

    def test_control_characters_rejected(self) -> None:
        with pytest.raises(IdempotencyKeyError):
            validate_idempotency_key("abc\nxyz")


class TestIdempotencyServiceExecute:
    async def test_first_call_executes_and_stores_result(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor = make_actor(tenant_id=_TENANT_A)
        executed: list[int] = []

        async def handler(_session, request) -> dict:
            executed.append(request["value"])
            return {"stored": request["value"] * 2}

        session = object()
        result = await service.execute(
            session,
            actor=actor,
            operation="calc.double",
            key="key-1",
            request={"value": 21},
            handler=handler,
        )
        assert result.replayed is False
        assert result.result == {"stored": 42}
        assert executed == [21]

    async def test_replay_returns_stored_result_without_rerunning(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor = make_actor(tenant_id=_TENANT_A)
        executed: list[int] = []

        async def handler(_session, request) -> dict:
            executed.append(1)
            return {"stored": request["value"]}

        first = await service.execute(
            object(), actor=actor, operation="op", key="k", request={"value": 5}, handler=handler
        )
        second = await service.execute(
            object(), actor=actor, operation="op", key="k", request={"value": 5}, handler=handler
        )
        assert first.replayed is False and second.replayed is True
        assert first.result == second.result == {"stored": 5}
        assert executed == [1], "handler must run exactly once"

    async def test_same_key_different_payload_conflicts(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor = make_actor(tenant_id=_TENANT_A)
        executed: list[int] = []

        async def handler(_session, request) -> dict:
            executed.append(1)
            return {"ok": True}

        await service.execute(
            object(), actor=actor, operation="op", key="k", request={"a": 1}, handler=handler
        )
        with pytest.raises(IdempotencyConflictError, match="different"):
            await service.execute(
                object(), actor=actor, operation="op", key="k", request={"a": 2}, handler=handler
            )
        assert executed == [1], "conflicting request must NOT execute the operation"

    async def test_concurrent_identical_requests_execute_once(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo, wait_timeout=5.0)
        actor = make_actor(tenant_id=_TENANT_A)
        executions = 0

        async def handler(_session, request) -> dict:
            nonlocal executions
            executions += 1
            await asyncio.sleep(0.02)
            return {"executed": request["n"]}

        results = await asyncio.gather(*[
            service.execute(
                object(),
                actor=actor,
                operation="op",
                key="concurrent",
                request={"n": 0},  # IDENTICAL payloads
                handler=handler,
            )
            for _ in range(3)
        ])
        assert executions == 1, "only one of the concurrent requests may execute"
        assert sum(1 for r in results if r.replayed) == 2
        assert sum(1 for r in results if not r.replayed) == 1
        assert {r.result["executed"] for r in results} == {0}

    async def test_concurrent_conflicting_payloads_one_executes_one_conflicts(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo, wait_timeout=5.0)
        actor = make_actor(tenant_id=_TENANT_A)
        executions = 0

        async def handler(_session, request) -> dict:
            nonlocal executions
            executions += 1
            await asyncio.sleep(0.02)
            return {"payload": request["a"]}

        outcomes = await asyncio.gather(
            service.execute(
                object(), actor=actor, operation="op", key="race", request={"a": 1}, handler=handler
            ),
            service.execute(
                object(), actor=actor, operation="op", key="race", request={"a": 2}, handler=handler
            ),
            return_exceptions=True,
        )
        assert executions == 1
        successes = [o for o in outcomes if not isinstance(o, Exception)]
        conflicts = [o for o in outcomes if isinstance(o, IdempotencyConflictError)]
        assert len(successes) == 1 and len(conflicts) == 1

    async def test_in_progress_waits_then_replays(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor = make_actor(tenant_id=_TENANT_A)
        # A live in_progress claim committed by another request.
        await repo.seed_in_progress(
            tenant_id=_TENANT_A,
            operation="op",
            key="busy",
            request_hash=canonical_request_hash({"a": 1}),
            claimed_until=datetime.now(UTC) + timedelta(seconds=60),
        )

        async def finish_later() -> None:
            await asyncio.sleep(0.05)
            for record in repo.records.values():
                record.status = "completed"
                record.result = {"stored": True}

        async def handler(_session, _request) -> dict:
            raise AssertionError("handler must not run — the other request owns the claim")

        waiter = asyncio.create_task(
            service.execute(
                object(),
                actor=actor,
                operation="op",
                key="busy",
                request={"a": 1},
                handler=handler,
            )
        )
        finisher = asyncio.create_task(finish_later())
        result, _ = await asyncio.gather(waiter, finisher)
        assert result.replayed is True
        assert result.result == {"stored": True}

    async def test_in_progress_timeout_raises(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo, wait_poll=0.001, wait_timeout=0.05)
        actor = make_actor(tenant_id=_TENANT_A)
        await repo.seed_in_progress(
            tenant_id=_TENANT_A,
            operation="op",
            key="stuck",
            request_hash=canonical_request_hash({"a": 1}),
            claimed_until=datetime.now(UTC) + timedelta(seconds=60),
        )

        async def handler(_session, _request) -> dict:
            raise AssertionError("must not run")

        with pytest.raises(IdempotencyInProgressError):
            await service.execute(
                object(),
                actor=actor,
                operation="op",
                key="stuck",
                request={"a": 1},
                handler=handler,
            )

    async def test_expired_lease_is_reclaimed_and_executed(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor = make_actor(tenant_id=_TENANT_A)
        await repo.seed_in_progress(
            tenant_id=_TENANT_A,
            operation="op",
            key="crashed",
            request_hash=canonical_request_hash({"a": 9}),
            claimed_until=datetime.now(UTC) - timedelta(seconds=10),  # expired
        )
        executed: list[int] = []

        async def handler(_session, request) -> dict:
            executed.append(1)
            return {"recovered": request["a"]}

        result = await service.execute(
            object(),
            actor=actor,
            operation="op",
            key="crashed",
            request={"a": 9},
            handler=handler,
        )
        assert result.replayed is False
        assert executed == [1]
        assert result.result == {"recovered": 9}

    async def test_same_key_different_venue_conflicts(self) -> None:
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor_all_venues = make_actor(tenant_id=_TENANT_A)

        async def handler(_session, _request) -> dict:
            return {"ok": True}

        await service.execute(
            object(),
            actor=actor_all_venues,
            operation="op",
            key="venue-key",
            request={"x": 1},
            handler=handler,
            venue_id=_VENUE_A,
        )
        # The same key with a DIFFERENT venue context is a conflict — the
        # venue-A result is never replayed for venue B.
        with pytest.raises(IdempotencyConflictError, match="venue"):
            await service.execute(
                object(),
                actor=actor_all_venues,
                operation="op",
                key="venue-key",
                request={"x": 1},
                handler=handler,
                venue_id=_VENUE_B,
            )

    async def test_tenant_scope_always_actor_derived(self) -> None:
        """Lookups are scoped by the ActorContext tenant — never payload."""
        repo = FakeIdempotencyRepository()
        service = _service(repo)
        actor_a = make_actor(tenant_id=_TENANT_A)
        actor_b = make_actor(tenant_id=uuid.uuid4())

        async def handler(_session, _request) -> dict:
            return {"ok": True}

        await service.execute(
            object(), actor=actor_a, operation="op", key="shared", request={}, handler=handler
        )
        await service.execute(
            object(), actor=actor_b, operation="op", key="shared", request={}, handler=handler
        )
        assert len(repo.records) == 2, "same key in different tenants is a different unit"
        for call_name, call_kwargs in repo.calls:
            if call_name in ("get", "create_claim"):
                assert call_kwargs["tenant_id"] in {_TENANT_A, actor_b.tenant_id}
