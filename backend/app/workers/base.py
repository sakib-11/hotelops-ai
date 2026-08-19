"""Shared polling worker base (Task 7 Phase 6/10).

A worker runs ``run_once()`` cycles on a poll interval until ``stop()``
is called. Graceful shutdown is cooperative: the loop checks the stop
event between cycles and between items inside a cycle, so an in-flight
item is finished (its database transaction committed or rolled back)
before the worker exits — never mid-transaction.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class PollingWorker:
    """Base class for database-polling workers."""

    def __init__(self, *, poll_interval: float, worker_id: str) -> None:
        self._poll_interval = poll_interval
        self.worker_id = worker_id
        self._stop_event = asyncio.Event()

    async def run_once(self) -> int:
        """Run one work cycle; returns the number of items handled.

        Must be implemented by subclasses.
        """
        raise NotImplementedError

    async def run_forever(self) -> None:
        """Run cycles until stop() is requested."""
        logger.info("worker %s starting (poll interval %.2fs)", self.worker_id, self._poll_interval)
        while not self._stop_event.is_set():
            try:
                handled = await self.run_once()
                if handled:
                    logger.debug("worker %s handled %d item(s)", self.worker_id, handled)
            except Exception:
                # A worker must never die from a single bad cycle —
                # log and continue after the poll interval.
                logger.exception("worker %s cycle failed; continuing", self.worker_id)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue
        logger.info("worker %s stopped", self.worker_id)

    def request_stop(self) -> None:
        """Request a graceful stop (safe from any thread)."""
        self._stop_event.set()

    async def stop(self) -> None:
        """Request a graceful stop (async convenience)."""
        self._stop_event.set()
