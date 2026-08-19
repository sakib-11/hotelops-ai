"""Inbox ingress bridge (Task 7 — Redis transport → inbox).

Relays EventEnvelope messages from the Redis stream (published by the
outbox publisher / partners) into the transactional inbox. The inbox's
unique (source, source_message_id) key makes this relay idempotent:

  - a message relayed twice (bridge crash between read and ack, or
    publisher crash before marking published) inserts exactly one row;
  - a duplicate delivery is acknowledged and dropped (the dedup key
    already rejected the insert) — never re-processed.

Consumer-group semantics (ADR-004): the bridge reads with XREADGROUP
and acknowledges with XACK. A message is acknowledged ONLY after its
inbox insert committed, so a crash between read and ack leaves the
message pending in the group; XAUTOCLAIM reclaims messages that have
been idle longer than the configured claim window (the Redis mirror of
database lease expiry).

Malformed envelopes are acknowledged and logged (the outbox validates
every envelope before enqueue, so a malformed stream message is
corruption, not a transient failure).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from backend.app.application.services.inbox import InboxService
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.transport import RedisStreamTransport
from backend.app.workers.base import PollingWorker
from contracts.events import EventEnvelope

logger = logging.getLogger(__name__)

_SOURCE_OUTBOX = "outbox"


class InboxIngressBridge(PollingWorker):
    """Reads the Redis stream and inserts deduplicated inbox rows."""

    def __init__(
        self,
        database: DatabaseClient,
        transport: RedisStreamTransport,
        settings: Settings,
        *,
        consumer: str | None = None,
    ) -> None:
        super().__init__(
            poll_interval=settings.outbox_poll_interval,
            worker_id=consumer or f"bridge:{uuid.uuid4().hex[:8]}",
        )
        self._database = database
        self._transport = transport
        self._settings = settings
        self._consumer = self.worker_id
        self._group = settings.redis_consumer_group
        self._count = 8

    async def run_once(self) -> int:
        """One read → receive → ack cycle.

        Returns:
            The number of stream messages handled (inserted or deduped).
        """
        stream = self._settings.redis_stream_events
        await self._transport.ensure_group(stream, self._group)

        # Crash recovery first: reclaim messages stuck in the PEL, then
        # read new messages.
        messages = await self._transport.claim_stuck(
            stream,
            self._group,
            self._consumer,
            min_idle_seconds=self._settings.redis_stream_claim_idle_seconds,
            count=self._count,
        )
        messages += await self._transport.read_group(
            stream,
            self._group,
            self._consumer,
            count=self._count,
            block_ms=0,
        )

        handled = 0
        for msg in messages:
            if self._stop_event.is_set():
                break
            if await self._handle_message(stream, msg):
                handled += 1
        return handled

    # =========================================================================
    # Per-message handling
    # =========================================================================

    async def _handle_message(self, stream: str, msg: dict[str, Any]) -> bool:
        """Relay one stream message into the inbox.

        Task 8.8: the original trace context is extracted from the
        envelope (captured at enqueue time) and used as the parent of
        the ingress span, preserving the end-to-end trace.

        Returns:
            True if the message was handled and acknowledged.
        """
        message_id = msg.get("id", "?")
        try:
            envelope = self._parse_envelope(msg)
            tenant_id = uuid.UUID(msg["tenant_id"])
            venue_id = uuid.UUID(msg["venue_id"]) if msg.get("venue_id") else None

            parent_trace = tracing.trace_context_from_event_attrs(
                trace_id=envelope.trace_id,
                span_id=envelope.span_id,
                trace_sampled=envelope.trace_sampled,
            )
            async with tracing.event_span(
                "ingress.relay",
                event_id=str(envelope.event_id),
                event_type=envelope.event_type,
                tenant_id=str(tenant_id),
                venue_id=str(venue_id) if venue_id else None,
                correlation_id=envelope.correlation_id,
                parent_trace=parent_trace,
            ):
                async with self._database.session() as session:
                    await InboxService().receive(
                        session,
                        source=_SOURCE_OUTBOX,
                        envelope=envelope,
                        tenant_id=tenant_id,
                        venue_id=venue_id,
                    )
                # Insert committed (or dedup rejected it) — safe to ack.
                await self._transport.ack(stream, self._group, message_id)
            return True
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # Structurally corrupt message — ack and log (the outbox
            # validates envelopes, so this cannot self-heal).
            logger.error(
                "malformed stream message acknowledged and dropped",
                extra={"message_id": message_id, "error_category": type(exc).__name__},
            )
            try:
                await self._transport.ack(stream, self._group, message_id)
            except Exception:
                logger.exception("failed to ack malformed message %s", message_id)
            return True
        except Exception:
            # Transient failure (DB down, Redis blip) — do NOT ack; the
            # message stays in the PEL and is reclaimed/redelivered.
            logger.exception(
                "ingress relay failed; message left pending for redelivery",
                extra={"message_id": message_id},
            )
            return False

    @staticmethod
    def _parse_envelope(msg: dict[str, Any]) -> EventEnvelope[dict[str, Any]]:
        """Parse + validate the canonical EventEnvelope from the message.

        Raises:
            ValidationError: If the envelope is structurally invalid.
        """
        raw = msg["event"]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        return EventEnvelope[dict[str, Any]].model_validate(payload)


async def _main() -> None:
    from backend.app.infrastructure.logging import configure_logging
    from backend.app.infrastructure.observability import tracing
    from backend.app.infrastructure.redis.client import RedisClient

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings=settings)
    tracing.configure_tracing(settings)
    database = DatabaseClient(settings)
    await database.initialize()
    redis = RedisClient(settings)
    await redis.initialize()
    transport = RedisStreamTransport(redis)
    try:
        bridge = InboxIngressBridge(database, transport, settings)
        await bridge.run_forever()
    finally:
        await redis.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
