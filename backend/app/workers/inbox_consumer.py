"""Inbox consumer worker (Task 7 Phase 10).

Processes inbox rows (inserted by the ingress bridge / partners) with
idempotent, crash-safe semantics:

    receive (dedup insert) ──▶ claim ──▶ effect + mark processed (atomic)
                                            │
                                            └─fail─▶ failed (available_at = now + backoff)
                                                     │
                                                     └─attempts >= max / no handler──▶ DEAD_LETTER

Dedup: the unique (source, source_message_id) inbox key means duplicate
delivery inserts NOTHING — the business effect runs at most once per
message, even under at-least-once Redis redelivery or publisher
crashes.

Transaction ownership (Phase 13): the effect handler runs INSIDE the
same transaction as mark_processed — an inbox row is NEVER marked
processed before the business effect is safely committed. If the effect
raises, a savepoint rolls back its partial writes, then the row is
marked failed (or dead-lettered) and THAT state commits. A consumer
crash before commit leaves the row claimable after its lease expires.

Handlers are registered per event_type. A message whose event_type has
no handler can never be processed — it is dead-lettered immediately
with a clear reason (better than infinite retries).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.database.models.audit_outbox_inbox import InboxMessageModel
from backend.app.infrastructure.database.repositories.inbox import InboxRepository
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.reliability import (
    NonRetryableError,
    compute_backoff_delay,
)
from backend.app.workers.base import PollingWorker
from contracts.common import utc_now

logger = logging.getLogger(__name__)

# An effect handler runs on the session inside the consumer's transaction
# and must not commit/rollback itself (the worker owns the transaction).
EffectHandler = Callable[[AsyncSession, InboxMessageModel], Awaitable[None]]


class InboxConsumerWorker(PollingWorker):
    """Claims and processes inbox messages with dedup + crash safety."""

    def __init__(
        self,
        database: DatabaseClient,
        settings: Settings,
        *,
        effect_handlers: dict[str, EffectHandler] | None = None,
        worker_id: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(
            poll_interval=settings.inbox_poll_interval,
            worker_id=worker_id or f"inbox:{uuid.uuid4().hex[:8]}",
        )
        self._database = database
        self._settings = settings
        self._effect_handlers = effect_handlers or {}
        self._rng = rng or random.Random()
        self._batch_size = 20
        self._max_error_length = 2000

    def register_handler(self, event_type: str, handler: EffectHandler) -> None:
        """Register an effect handler for an event_type."""
        if event_type in self._effect_handlers:
            logger.warning("replacing inbox effect handler for %s", event_type)
        self._effect_handlers[event_type] = handler

    async def run_once(self) -> int:
        """One claim → effect → transition cycle.

        Returns:
            The number of messages successfully processed.
        """
        now = utc_now()
        async with self._database.session() as session:
            rows = await InboxRepository(session).claim_next(
                worker_id=self.worker_id,
                batch_size=self._batch_size,
                lease_seconds=self._settings.inbox_lease_seconds,
                now=now,
            )

        processed = 0
        for row in rows:
            if self._stop_event.is_set():
                break
            try:
                if await self._process_row(row):
                    processed += 1
            except Exception:
                logger.exception(
                    "inbox consumer transaction failed for message",
                    extra={"inbox_id": str(row.inbox_id), "event_type": row.event_type},
                )
        return processed

    async def _process_row(self, row: InboxMessageModel) -> bool:
        """Run the effect + terminal transition in one transaction.

        Task 8.8: the trace context captured at enqueue time is
        extracted from the inbox row's envelope payload and used as the
        parent of this processing span, so the original trace continues
        all the way to the business effect.

        Returns:
            True if the message reached 'processed'.
        """
        handler = self._effect_handlers.get(row.event_type or "")
        if handler is None:
            await self._dead_letter(
                row,
                error=f"no effect handler registered for event_type={row.event_type!r}",
            )
            return False

        attempts = row.attempts
        # The envelope's own event_id (the canonical event identity) is
        # preferred over the inbox row id so the span is correlated with
        # the original event across the whole pipeline.
        envelope_event_id = row.payload.get("event_id") if isinstance(row.payload, dict) else None
        correlation_id = (
            row.payload.get("correlation_id") if isinstance(row.payload, dict) else None
        )
        parent_trace = tracing.trace_context_from_event_attrs(
            trace_id=row.payload.get("trace_id") if isinstance(row.payload, dict) else None,
            span_id=row.payload.get("span_id") if isinstance(row.payload, dict) else None,
            trace_sampled=(
                row.payload.get("trace_sampled") if isinstance(row.payload, dict) else None
            ),
        )
        # Processing-attempt span for the whole effect+transition. The
        # payload body is never an attribute (Task 8.7). When no parent
        # trace is available (missing telemetry context), a fresh trace
        # is started (requirement 10).
        async with tracing.event_span(
            "inbox.process",
            event_id=str(envelope_event_id or row.inbox_id),
            event_type=row.event_type or "",
            tenant_id=str(row.tenant_id),
            correlation_id=correlation_id if isinstance(correlation_id, str) else None,
            parent_trace=parent_trace,
        ) as _:
            return await self._process_row_with_span(row, handler, attempts)

    async def _process_row_with_span(
        self, row: InboxMessageModel, handler: EffectHandler, attempts: int
    ) -> bool:
        """The original transaction body of _process_row (kept flat)."""
        async with self._database.session() as session:
            # Savepoint: if the effect raises, its partial writes are
            # rolled back WITHOUT discarding the failed/dead-letter
            # transition we commit afterwards — one transaction per
            # row outcome.
            savepoint = await session.begin_nested()
            try:
                await handler(session, row)
                ok = await InboxRepository(session).mark_processed(row.inbox_id, self.worker_id)
                if not ok:
                    # Claim lost (lease expired mid-effect): another
                    # worker already reclaimed the row and owns it.
                    # Roll back the effect's writes so a duplicate
                    # business effect can NEVER be committed — the
                    # new owner handles the message.
                    await savepoint.rollback()
                    logger.warning(
                        "inbox claim lost before processed-marking; effect rolled back",
                        extra={"inbox_id": str(row.inbox_id), "event_type": row.event_type},
                    )
                    return False
                await session.commit()
            except NonRetryableError as exc:
                await savepoint.rollback()
                await InboxRepository(session).mark_dead_letter(
                    row.inbox_id,
                    self.worker_id,
                    error=str(exc)[: self._max_error_length],
                )
                await session.commit()
                self._log_dead_letter(row, type(exc).__name__)
                return False
            except Exception as exc:  # retryable — bounded backoff
                await savepoint.rollback()
                if attempts >= self._settings.inbox_max_attempts:
                    await InboxRepository(session).mark_dead_letter(
                        row.inbox_id,
                        self.worker_id,
                        error=str(exc)[: self._max_error_length],
                    )
                    await session.commit()
                    self._log_dead_letter(row, type(exc).__name__)
                else:
                    delay = compute_backoff_delay(
                        attempts,
                        base_seconds=self._settings.inbox_backoff_base,
                        max_seconds=self._settings.inbox_backoff_max,
                        jitter=self._settings.inbox_backoff_jitter,
                        rng=self._rng,
                    )
                    await InboxRepository(session).mark_failed(
                        row.inbox_id,
                        self.worker_id,
                        error=str(exc)[: self._max_error_length],
                        retry_at=utc_now() + delay,
                    )
                    await session.commit()
                    logger.warning(
                        "inbox effect failed; retry scheduled",
                        extra={
                            "inbox_id": str(row.inbox_id),
                            "event_type": row.event_type,
                            "attempts": attempts,
                            "retry_in_seconds": delay.total_seconds(),
                            "error_category": type(exc).__name__,
                        },
                    )
                return False
        return True

    # =========================================================================
    # Dead-letter
    # =========================================================================

    async def _dead_letter(self, row: InboxMessageModel, error: Exception | str) -> None:
        """Move the message to DEAD_LETTER (terminal — never deleted)."""
        message = error if isinstance(error, str) else str(error)
        async with self._database.session() as session:
            ok = await InboxRepository(session).mark_dead_letter(
                row.inbox_id,
                self.worker_id,
                error=message[: self._max_error_length],
            )
        if ok:
            self._log_dead_letter(row, type(error).__name__)

    @staticmethod
    def _log_dead_letter(row: InboxMessageModel, error_category: str) -> None:
        logger.error(
            "inbox message dead-lettered",
            extra={
                "inbox_id": str(row.inbox_id),
                "event_type": row.event_type,
                "tenant_id": str(row.tenant_id),
                "attempts": row.attempts,
                "error_category": error_category,
            },
        )


async def _main() -> None:
    from backend.app.infrastructure.logging import configure_logging
    from backend.app.infrastructure.observability import tracing
    from backend.app.infrastructure.redis.client import RedisClient

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings=settings)
    tracing.configure_tracing(settings)
    database = DatabaseClient(settings)
    await database.initialize()
    # The consumer does not need Redis directly (the ingress bridge
    # relays stream messages into the inbox), but the process keeps the
    # same dependency shape so operators run one stack per worker.
    redis = RedisClient(settings)
    await redis.initialize()
    try:
        worker = InboxConsumerWorker(database, settings)
        await worker.run_forever()
    finally:
        await redis.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
