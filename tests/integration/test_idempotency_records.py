"""Integration tests for the Task 7 idempotency records (Phase 11).

Real TimescaleDB + the real IdempotencyService/repository: replay,
payload-conflict rejection, CONCURRENT identical requests (one
execution), tenant isolation, venue scope enforcement, and stale-lease
reclaim (crash recovery).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from backend.app.application.services.idempotency import (
    IdempotencyService,
    canonical_request_hash,
)
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.reliability.exceptions import IdempotencyConflictError
from tests.integration._task7_helpers import (
    make_actor,
    make_database_client,
    query_engine,
    scalar,
    scratch_settings,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not __import__("os").environ.get("INTEGRATION_TESTS"),
        reason="Set INTEGRATION_TESTS=1 and start PostgreSQL",
    ),
]

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VENUE_A = uuid.UUID("00000000-0000-0000-0000-000000000020")
_VENUE_B = uuid.UUID("00000000-0000-0000-0000-000000000021")


def _service_settings(db_name: str):
    return scratch_settings(db_name)


def _service(client: DatabaseClient, db_name: str) -> IdempotencyService:
    return IdempotencyService(
        _service_settings(db_name),
        lease_seconds=30,
        wait_timeout=5.0,
        wait_poll=0.01,
    )


class TestReplayAndConflict:
    async def test_first_executes_and_replay_returns_same_result(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor = make_actor(tenant_id=_TENANT_A)
            executions = 0

            async def handler(session, request) -> dict:
                nonlocal executions
                executions += 1
                return {"doubled": request["value"] * 2}

            async def run(request):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor,
                        operation="calc.double",
                        key="k-replay",
                        request=request,
                        handler=handler,
                    )

            first = await run({"value": 21})
            second = await run({"value": 21})
            assert first.replayed is False and first.result == {"doubled": 42}
            assert second.replayed is True and second.result == {"doubled": 42}
            assert executions == 1, "handler must execute exactly once"
        finally:
            await client.dispose()

    async def test_same_key_different_payload_conflicts(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor = make_actor(tenant_id=_TENANT_A)
            executions = 0

            async def handler(session, request) -> dict:
                nonlocal executions
                executions += 1
                return {"ok": True}

            async def run(request):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor,
                        operation="op",
                        key="k-conflict",
                        request=request,
                        handler=handler,
                    )

            await run({"a": 1})
            with pytest.raises(IdempotencyConflictError):
                await run({"a": 2})
            assert executions == 1, "the conflicting operation must NOT execute"
            # The original result is preserved
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT result->>'ok' FROM idempotency_records "
                    "WHERE idempotency_key = 'k-conflict'",
                )
                == "true"
            )
        finally:
            await client.dispose()


class TestConcurrency:
    async def test_concurrent_identical_requests_execute_once(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor = make_actor(tenant_id=_TENANT_A)
            executions = 0

            async def handler(session, request) -> dict:
                nonlocal executions
                executions += 1
                await asyncio.sleep(0.2)
                return {"executed": request["n"]}

            async def run(i: int):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor,
                        operation="op",
                        key="k-concurrent",
                        # IDENTICAL payloads for every racing request — a
                        # key with a different payload is a 409 conflict,
                        # which is tested separately.
                        request={"n": 0},
                        handler=handler,
                    )

            results = await asyncio.gather(*[run(i) for i in range(3)])
            assert executions == 1, "only one concurrent request may execute"
            assert sum(1 for r in results if r.replayed) == 2
            assert sum(1 for r in results if not r.replayed) == 1
            assert {r.result["executed"] for r in results} == {0}
        finally:
            await client.dispose()

    async def test_concurrent_conflicting_payloads(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor = make_actor(tenant_id=_TENANT_A)
            executions = 0

            async def handler(session, request) -> dict:
                nonlocal executions
                executions += 1
                await asyncio.sleep(0.2)
                return {"payload": request["a"]}

            async def run(payload: int):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor,
                        operation="op",
                        key="k-race",
                        request={"a": payload},
                        handler=handler,
                    )

            outcomes = await asyncio.gather(run(1), run(2), return_exceptions=True)
            successes = [o for o in outcomes if not isinstance(o, Exception)]
            conflicts = [o for o in outcomes if isinstance(o, IdempotencyConflictError)]
            assert len(successes) == 1 and len(conflicts) == 1
            assert executions == 1
        finally:
            await client.dispose()


class TestIsolation:
    async def test_same_key_different_tenants_is_independent(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor_a = make_actor(tenant_id=_TENANT_A)
            actor_b = make_actor(tenant_id=_TENANT_B)

            async def handler(session, request) -> dict:
                return {"tenant": str(request["tenant"])}

            async def run(actor, tenant_label):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor,
                        operation="op",
                        key="shared-key",
                        request={"tenant": tenant_label},
                        handler=handler,
                    )

            a = await run(actor_a, "A")
            b = await run(actor_b, "B")
            assert a.replayed is False and b.replayed is False, (
                "a key in Tenant A is a different unit from the same key in Tenant B"
            )
            assert a.result == {"tenant": "A"} and b.result == {"tenant": "B"}
            assert await scalar(task7_db["url"], "SELECT count(*) FROM idempotency_records") == 2
        finally:
            await client.dispose()

    async def test_venue_scoped_actor_cannot_use_foreign_venue(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor_venue_a = make_actor(tenant_id=_TENANT_A, venue_scope=frozenset({_VENUE_A}))

            async def handler(session, request) -> dict:
                return {"ok": True}

            async def run(venue_id):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor_venue_a,
                        operation="op",
                        key="venue-key",
                        request={},
                        handler=handler,
                        venue_id=venue_id,
                    )

            assert (await run(_VENUE_A)).replayed is False
            with pytest.raises(AuthorizationError):
                await run(_VENUE_B)
            # The out-of-scope attempt must not have created a record
            assert await scalar(task7_db["url"], "SELECT count(*) FROM idempotency_records") == 1
        finally:
            await client.dispose()

    async def test_same_key_different_venue_conflicts(self, task7_db) -> None:
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor = make_actor(tenant_id=_TENANT_A)

            async def handler(session, request) -> dict:
                return {"ok": True}

            async def run(venue_id):
                async with client.session() as session:
                    return await service.execute(
                        session,
                        actor=actor,
                        operation="op",
                        key="venue-key",
                        request={},
                        handler=handler,
                        venue_id=venue_id,
                    )

            await run(_VENUE_A)
            with pytest.raises(IdempotencyConflictError, match="venue"):
                await run(_VENUE_B)
        finally:
            await client.dispose()


class TestCrashRecovery:
    async def test_stale_in_progress_lease_is_reclaimed(self, task7_db) -> None:
        """A claim whose worker died becomes reclaimable after lease expiry."""
        client = make_database_client(task7_db["name"])
        await client.initialize()
        try:
            service = _service(client, task7_db["name"])
            actor = make_actor(tenant_id=_TENANT_A)
            request_hash = canonical_request_hash({"a": 1})

            # Simulate a committed in_progress claim from a crashed worker
            engine = query_engine(task7_db["url"])
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO idempotency_records "
                            "(idempotency_id, tenant_id, operation, idempotency_key, "
                            "request_hash, status, claimed_by, claimed_until, created_at) "
                            "VALUES (:iid, :tid, 'op', 'crashed-key', :hash, 'in_progress', "
                            "'dead-worker', now() - interval '1 minute', now() - interval '2 minutes')"
                        ),
                        {
                            "iid": uuid.uuid4(),
                            "tid": _TENANT_A,
                            "hash": request_hash,
                        },
                    )
            finally:
                await engine.dispose()

            executions = 0

            async def handler(session, request) -> dict:
                nonlocal executions
                executions += 1
                return {"recovered": True}

            async with client.session() as session:
                result = await service.execute(
                    session,
                    actor=actor,
                    operation="op",
                    key="crashed-key",
                    request={"a": 1},
                    handler=handler,
                )
            assert result.replayed is False
            assert executions == 1
            assert (
                await scalar(
                    task7_db["url"],
                    "SELECT status FROM idempotency_records WHERE idempotency_key = 'crashed-key'",
                )
                == "completed"
            )
        finally:
            await client.dispose()
