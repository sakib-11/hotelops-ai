"""Redis Streams transport (ADR-004: Redis is transport, not source of truth).

A thin adapter over the application's RedisClient connection that speaks
Redis Streams. The canonical EventEnvelope (contracts/events/envelope.py)
is the wire format: the envelope JSON is published under the ``event``
field, with ``tenant_id``/``venue_id`` carried as sibling fields so the
inbox ingress can scope the message WITHOUT touching the envelope contract.

Failure policy: every Redis interaction failure is wrapped in
:class:`PublishError` (a RetryableError) so the outbox publisher can
schedule a bounded backoff retry — the outbox row is the durability
boundary, Redis being unavailable must never lose the event.

Consumer-group recovery: the ingress bridge reads new messages via
XREADGROUP (``>``) and reclaims messages stuck in the group's pending
entries list (PEL) via XAUTOCLAIM with a minimum idle time — the Redis
mirror of the database lease-expiry recovery.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.redis.client import RedisClient
from backend.app.infrastructure.reliability.exceptions import PublishError

logger = logging.getLogger(__name__)

# Redis stream message fields (sibling to `event`, not part of the
# EventEnvelope contract).
_FIELD_EVENT = "event"
_FIELD_EVENT_ID = "event_id"
_FIELD_EVENT_TYPE = "event_type"
_FIELD_TENANT_ID = "tenant_id"
_FIELD_VENUE_ID = "venue_id"
_FIELD_SCHEMA_VERSION = "schema_version"
_FIELD_CORRELATION_ID = "correlation_id"


class RedisStreamTransport:
    """Redis Streams producer/consumer-group adapter over RedisClient."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def publish(
        self,
        stream: str,
        *,
        event_id: str,
        event_type: str,
        envelope_json: str,
        tenant_id: str,
        venue_id: str | None,
        schema_version: str,
        correlation_id: str | None,
    ) -> str:
        """Append a serialized EventEnvelope to a stream (XADD).

        Returns:
            The Redis stream message ID.

        Raises:
            PublishError: If Redis is unreachable or the write fails —
                the caller schedules a retry (the outbox row is durable).
        """
        fields: dict[str, str] = {
            _FIELD_EVENT: envelope_json,
            _FIELD_EVENT_ID: str(event_id),
            _FIELD_EVENT_TYPE: event_type,
            _FIELD_TENANT_ID: tenant_id,
            _FIELD_SCHEMA_VERSION: schema_version,
        }
        if venue_id is not None:
            fields[_FIELD_VENUE_ID] = venue_id
        if correlation_id is not None:
            fields[_FIELD_CORRELATION_ID] = correlation_id
        async with tracing.redis_span(
            "redis.xadd",
            event_id=event_id,
            event_type=event_type,
            tenant_id=tenant_id,
            venue_id=venue_id,
        ) as _:
            try:
                message_id = await self._redis.client.xadd(stream, cast(Any, fields))
            except Exception as exc:
                logger.warning("Redis XADD failed on stream %s: %s", stream, exc)
                raise PublishError(f"Redis XADD failed on stream {stream}: {exc}") from exc
        if isinstance(message_id, bytes):
            return message_id.decode("utf-8")
        return str(message_id)

    async def ensure_group(self, stream: str, group: str) -> None:
        """Create the consumer group idempotently (XGROUP CREATE MKSTREAM).

        Raises:
            PublishError: If Redis is unreachable.
        """
        try:
            try:
                await self._redis.client.xgroup_create(stream, group, id="0", mkstream=True)
            except Exception as exc:
                # BUSYGROUP — the group already exists (or a transient
                # failure). Re-check by reading the group's info.
                err = str(exc).lower()
                if "busygroup" in err:
                    return
                raise PublishError(
                    f"Redis XGROUP CREATE failed on {stream}:{group}: {exc}"
                ) from exc
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"Redis XGROUP CREATE failed on {stream}:{group}: {exc}") from exc

    async def read_group(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 8,
        block_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """Read new messages for the consumer (XREADGROUP GROUP ... >).

        Returns:
            A list of message dicts:
            {id, event, event_id, event_type, tenant_id, venue_id,
             schema_version, correlation_id}.

        Raises:
            PublishError: If Redis is unreachable.
        """
        async with tracing.redis_span("redis.xreadgroup") as _:
            try:
                entries: Any = await self._redis.client.xreadgroup(
                    group, consumer, {stream: ">"}, count=count, block=block_ms
                )
            except Exception as exc:
                raise PublishError(f"Redis XREADGROUP failed on {stream}:{group}: {exc}") from exc
        return self._parse_entries(entries)

    async def claim_stuck(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        min_idle_seconds: int,
        count: int = 8,
    ) -> list[dict[str, Any]]:
        """Reclaim messages stuck in the group PEL (XAUTOCLAIM).

        Mirrors database lease expiry: a message delivered to a crashed
        consumer becomes claimable once it has been idle for
        ``min_idle_seconds``.

        Raises:
            PublishError: If Redis is unreachable.
        """
        async with tracing.redis_span("redis.xautoclaim") as _:
            try:
                _, entries, _ = await self._redis.client.xautoclaim(
                    stream,
                    group,
                    consumer,
                    min_idle_time=min_idle_seconds * 1000,
                    start_id="0",
                    count=count,
                )
            except Exception as exc:
                raise PublishError(f"Redis XAUTOCLAIM failed on {stream}:{group}: {exc}") from exc
        return self._parse_entries(cast(Any, entries))

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message (XACK).

        Raises:
            PublishError: If Redis is unreachable.
        """
        async with tracing.redis_span("redis.xack") as _:
            try:
                await self._redis.client.xack(stream, group, message_id)
            except Exception as exc:
                raise PublishError(f"Redis XACK failed on {stream}:{group}: {exc}") from exc

    async def pending_count(self, stream: str, group: str) -> int:
        """Number of unacknowledged messages in the group (XPENDING).

        Raises:
            PublishError: If Redis is unreachable.
        """
        try:
            summary = await self._redis.client.xpending(stream, group)
        except Exception as exc:
            raise PublishError(f"Redis XPENDING failed on {stream}:{group}: {exc}") from exc
        if not summary:
            return 0
        # redis-py returns a PendingSummary-like object with a .pending
        # attribute (or a dict when decode config differs) — handle both.
        pending = getattr(summary, "pending", None)
        if pending is None and isinstance(summary, dict):
            pending = summary.get("pending", 0)
        return int(pending or 0)

    async def stream_length(self, stream: str) -> int:
        """Current stream length (XLEN) for observability.

        Raises:
            PublishError: If Redis is unreachable.
        """
        try:
            return int(await self._redis.client.xlen(stream))
        except Exception as exc:
            raise PublishError(f"Redis XLEN failed on {stream}: {exc}") from exc

    @staticmethod
    def _parse_entries(entries: Any) -> list[dict[str, Any]]:
        """Normalize raw stream entries into message dicts.

        redis-py returns entries as
        [(stream_name, [(message_id, {field: value}), ...]), ...] with
        decode_responses=True. This adapter normalizes to the shape the
        ingress bridge consumes.
        """
        messages: list[dict[str, Any]] = []
        for _stream_name, stream_entries in entries:
            for message_id, fields in stream_entries:
                item: dict[str, Any] = {"id": message_id}
                if isinstance(fields, dict):
                    item.update({str(k): v for k, v in fields.items()})
                messages.append(item)
        return messages
