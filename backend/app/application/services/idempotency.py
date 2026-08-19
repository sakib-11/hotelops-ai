"""Idempotency service (Task 7 Phase 11).

Guarantees:

  replay     — the same (tenant, operation, idempotency_key) with the
               SAME canonical payload hash returns the previously stored
               logical result WITHOUT executing the operation again.
  conflict   — the same key with a DIFFERENT payload hash raises
               IdempotencyConflictError (HTTP 409 semantics) and the
               second operation MUST NOT execute.
  concurrency — simultaneous identical requests race on
               INSERT ... ON CONFLICT DO NOTHING: exactly one wins the
               claim and executes; the losers replay the stored result.
               A claim whose transaction dies is simply rolled back with
               the operation (no record persists), and a claim that was
               committed as in_progress by a future two-phase flow is
               reclaimable after its lease expires.

Tenant security: lookups are ALWAYS scoped by the ActorContext tenant
(the repository filters on tenant_id from the server-side context —
client payloads can never name a tenant). Venue scope: a SPECIFIC_VENUES
actor may only create/replay records for venues inside its scope, and a
key already bound to a DIFFERENT venue is a conflict, never a replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.auth.scope import require_venue_access
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.repositories.idempotency import (
    IdempotencyRepository,
)
from backend.app.infrastructure.reliability.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyKeyError,
)
from contracts.common import VenueId, utc_now
from contracts.identity import ActorContext

# A handler mutates business state on the session and returns the
# logical result to persist (dict, JSONB-safe).
IdempotencyHandler = Callable[[AsyncSession, Any], Awaitable[dict[str, Any]]]

# Client-supplied key constraints (validated before any DB access).
_KEY_MIN_LENGTH = 1
_KEY_MAX_LENGTH = 128
_KEY_PATTERN = re.compile(r"^[ -~]+$")  # printable ASCII only


@dataclass(frozen=True)
class IdempotencyResult:
    """Outcome of an idempotent operation execution."""

    idempotency_id: uuid.UUID
    replayed: bool
    result: dict[str, Any] | None


def validate_idempotency_key(key: str) -> str:
    """Validate a client-supplied idempotency key.

    Raises:
        IdempotencyKeyError: If the key is empty, too long, or contains
            non-printable characters.
    """
    if not isinstance(key, str) or not (_KEY_MIN_LENGTH <= len(key) <= _KEY_MAX_LENGTH):
        msg = f"idempotency key must be a string of {_KEY_MIN_LENGTH}-{_KEY_MAX_LENGTH} characters"
        raise IdempotencyKeyError(msg)
    if not _KEY_PATTERN.match(key):
        msg = "idempotency key must contain only printable ASCII characters"
        raise IdempotencyKeyError(msg)
    return key


def _normalize(value: Any) -> Any:
    """Recursively normalize a request payload for canonical hashing.

    Pydantic models dump to JSON-safe primitives; datetimes and UUIDs
    serialize to ISO strings; dict keys become strings. The result is a
    pure JSON tree that json.dumps(sort_keys=True) renders identically
    regardless of key insertion order.
    """
    if hasattr(value, "model_dump"):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)  # canonical lowercase-hex form
    return value


def canonical_request_hash(request: Any) -> str:
    """Canonical SHA-256 hex digest of a request payload.

    Two payloads that are logically identical (same data, different key
    order, JSON vs model) produce the same hash; logically different
    payloads produce different hashes.
    """
    normalized = _normalize(request)
    payload = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IdempotencyService:
    """Executes a handler at most once per (tenant, operation, key)."""

    def __init__(
        self,
        settings: Settings,
        *,
        repository_factory: Callable[[AsyncSession], IdempotencyRepository] = IdempotencyRepository,
        lease_seconds: int | None = None,
        wait_timeout: float | None = None,
        wait_poll: float | None = None,
    ) -> None:
        self._settings = settings
        self._repo_factory = repository_factory
        self._lease_seconds = (
            lease_seconds if lease_seconds is not None else settings.idempotency_lease_seconds
        )
        self._wait_timeout = (
            wait_timeout if wait_timeout is not None else settings.idempotency_wait_timeout_seconds
        )
        self._wait_poll = (
            wait_poll if wait_poll is not None else settings.idempotency_wait_poll_seconds
        )

    async def execute(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        operation: str,
        key: str,
        request: Any,
        handler: IdempotencyHandler,
        venue_id: uuid.UUID | None = None,
    ) -> IdempotencyResult:
        """Run ``handler`` at most once for the idempotency unit.

        The handler runs inside the caller's transaction (this service
        never commits): business state, the idempotency record, and any
        outbox/audit rows written by the caller commit or roll back
        together.

        Args:
            session: The caller's transaction-scoped session (the
                transaction owner is the caller).
            actor: The trusted server-side ActorContext.
            operation: The operation/context name (e.g. 'alert.create').
            key: The client-supplied idempotency key.
            request: The request payload (canonically hashed).
            handler: async (session, request) -> logical result dict.
            venue_id: Optional venue context of the operation.

        Returns:
            The IdempotencyResult (replayed=False for the execution,
            replayed=True when the stored result was returned).

        Raises:
            IdempotencyConflictError: Same key, different payload (or a
                different venue context) — the operation is NOT run.
            IdempotencyInProgressError: A concurrent request held the
                claim beyond the bounded wait window.
        """
        validate_idempotency_key(key)
        if venue_id is not None:
            require_venue_access(actor, VenueId(venue_id))

        request_hash = canonical_request_hash(request)
        repo = self._repo_factory(session)
        claim = f"idem:{uuid.uuid4()}"
        deadline = utc_now() + timedelta(seconds=self._wait_timeout)

        while True:
            record = await repo.get(
                tenant_id=uuid.UUID(str(actor.tenant_id)),
                operation=operation,
                key=key,
            )

            if record is None:
                # No record — try to claim the unit. Exactly one
                # concurrent request wins (the others block on the
                # unique key and then replay).
                claimed = await repo.create_claim(
                    tenant_id=uuid.UUID(str(actor.tenant_id)),
                    operation=operation,
                    key=key,
                    request_hash=request_hash,
                    actor_id=uuid.UUID(str(actor.actor_id)),
                    venue_id=venue_id,
                    claimed_by=claim,
                    lease_seconds=self._lease_seconds,
                    now=utc_now(),
                )
                if claimed is None:
                    continue  # lost the race — read the winner's record
                result = await handler(session, request)
                await repo.complete(
                    idempotency_id=claimed.idempotency_id,
                    claimed_by=claim,
                    result=result,
                    now=utc_now(),
                )
                return IdempotencyResult(
                    idempotency_id=claimed.idempotency_id,
                    replayed=False,
                    result=result,
                )

            # Existing record — enforce venue context consistency. A key
            # bound to one venue is a DIFFERENT unit from the same key
            # with no venue or another venue: a None-vs-set mismatch is
            # also a conflict, never a replay (both None is the common
            # non-venue case and matches).
            if record.venue_id != venue_id:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} already used for a different venue "
                    f"context ({record.venue_id} != {venue_id})"
                )

            if record.status == "completed":
                if record.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        f"idempotency key {key!r} was already used with a different request payload"
                    )
                return IdempotencyResult(
                    idempotency_id=record.idempotency_id,
                    replayed=True,
                    result=record.result,
                )

            # in_progress: either reclaim an expired lease or wait
            # (bounded) for the concurrent holder to complete.
            now = utc_now()
            if record.claimed_until is not None and record.claimed_until <= now:
                reclaimed = await repo.reclaim(
                    idempotency_id=record.idempotency_id,
                    request_hash=request_hash,
                    actor_id=uuid.UUID(str(actor.actor_id)),
                    venue_id=venue_id,
                    claimed_by=claim,
                    lease_seconds=self._lease_seconds,
                    now=now,
                )
                if reclaimed:
                    result = await handler(session, request)
                    await repo.complete(
                        idempotency_id=record.idempotency_id,
                        claimed_by=claim,
                        result=result,
                        now=utc_now(),
                    )
                    return IdempotencyResult(
                        idempotency_id=record.idempotency_id,
                        replayed=False,
                        result=result,
                    )
                continue  # another request reclaimed it first

            if utc_now() >= deadline:
                raise IdempotencyInProgressError(
                    f"idempotency key {key!r} is still being processed by a "
                    "concurrent request (waited beyond the bounded window)"
                )
            await asyncio.sleep(self._wait_poll)


__all__ = [
    "IdempotencyHandler",
    "IdempotencyResult",
    "IdempotencyService",
    "canonical_request_hash",
    "validate_idempotency_key",
]
