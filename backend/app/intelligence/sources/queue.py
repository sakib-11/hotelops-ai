"""BoundedFrameQueue — the producer/consumer boundary between a frame
source and the CV pipeline (Task 11, Phase 5).

Carries ``(FramePacket, FrameData)`` pairs: the canonical metadata
envelope plus its in-process decoded payload.  Both are produced by the
frame source and consumed by the pipeline; the pair is never
serialized across a process boundary.

Queue-full policy is EXPLICIT and MANDATORY — the caller must pass a
``QueueFullPolicy``; the queue never silently picks a strategy.  The
choice is grounded in the Task 1 SLOs (docs/product/slo-requirements.md):

- ``BLOCK`` (backpressure, zero frame loss): the producer waits until a
  consumer drains capacity.  Appropriate for RECORDED processing where
  every frame is required for deterministic replay (SLO-007:
  recorded processing is a wall-clock/video-duration ratio, not a
  latency target — bounded waits do not violate it) and where dropping
  frames would silently change analysis results.
- ``DROP_OLDEST`` (bounded latency, counted loss): when the queue is
  full the OLDEST frame is evicted and the newest is admitted; the drop
  is counted for observability.  Appropriate for LIVE detection where
  SLO-006 (end-to-end frame-capture → event latency) dominates: never
  backpressure the source (which would grow latency unboundedly on a
  16-stream instance, SLO-011) — instead process the freshest frames
  and expose ``dropped_frames`` so latency/quality trade-offs are
  measurable rather than silent.

Shutdown semantics:

- ``shutdown()`` is idempotent.  It wakes all blocked producers and
  consumers.
- After shutdown, ``put()`` raises ``QueueClosedError`` immediately.
- A shut-down queue may still be drained: ``get()`` returns remaining
  items, then raises ``QueueClosedError`` once empty (a blocked
  ``get()`` is woken by shutdown and raises).

Cancellation: every wait is a cooperative cancellation point.  A
cancelled producer or consumer propagates ``asyncio.CancelledError``
and leaves the queue unchanged (no partial put/get).

Observability: per-queue counters (``dropped_frames``,
``total_enqueued``, ``total_dequeued``, ``max_observed``, ``qsize``)
are exposed for the existing Task 8 metrics/observability conventions;
``stats()`` returns an atomic snapshot for callers that publish to
Prometheus.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from backend.app.intelligence.sources.base import FrameData
from backend.app.intelligence.sources.exceptions import QueueClosedError
from contracts.video import FramePacket

__all__ = ["BoundedFrameQueue", "QueueFullPolicy", "QueueStats", "QueuedFrame"]


class QueueFullPolicy(StrEnum):
    """How a full queue treats a producer's new frame (must be chosen explicitly)."""

    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"


@dataclass(frozen=True, slots=True)
class QueuedFrame:
    """A (FramePacket, FrameData) pair crossing the queue boundary."""

    packet: FramePacket
    data: FrameData


@dataclass(frozen=True, slots=True)
class QueueStats:
    """Atomic snapshot of a BoundedFrameQueue for observability hooks."""

    qsize: int
    maxsize: int
    full_policy: QueueFullPolicy
    closed: bool
    total_enqueued: int
    total_dequeued: int
    dropped_frames: int
    max_observed: int


class BoundedFrameQueue:
    """An explicit-capacity async queue for (FramePacket, FrameData) pairs."""

    def __init__(self, *, maxsize: int, full_policy: QueueFullPolicy) -> None:
        """Create a bounded queue.

        Args:
            maxsize: Maximum number of queued frames (>= 1).
            full_policy: REQUIRED queue-full strategy — see module docstring.

        Raises:
            ValueError: if ``maxsize`` < 1.
        """
        if maxsize < 1:
            msg = f"maxsize must be >= 1, got {maxsize}"
            raise ValueError(msg)
        self._maxsize = maxsize
        self._full_policy = full_policy
        self._items: deque[QueuedFrame] = deque()
        self._cond = asyncio.Condition()
        self._closed = False
        self._dropped_frames = 0
        self._total_enqueued = 0
        self._total_dequeued = 0
        self._max_observed = 0

    # ------------------------------------------------------------------
    # Immutable configuration
    # ------------------------------------------------------------------

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def full_policy(self) -> QueueFullPolicy:
        return self._full_policy

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    async def put(self, item: QueuedFrame) -> None:
        """Enqueue one frame according to the configured full policy.

        Raises:
            QueueClosedError: after shutdown (new production is rejected).
        """
        async with self._cond:
            if self._closed:
                raise QueueClosedError("cannot put(): frame queue is shut down")
            if self._full_policy is QueueFullPolicy.DROP_OLDEST:
                if len(self._items) >= self._maxsize:
                    self._items.popleft()
                    self._dropped_frames += 1
            else:
                # Wait until capacity frees OR the queue shuts down.  The
                # closed flag is re-checked after the wait: shutdown is
                # authoritative even if capacity was freed concurrently,
                # so a producer never sneaks a frame in after shutdown.
                while not self._closed and len(self._items) >= self._maxsize:
                    await self._cond.wait()
                if self._closed:
                    raise QueueClosedError("cannot put(): frame queue is shut down")
            self._items.append(item)
            self._total_enqueued += 1
            self._max_observed = max(self._max_observed, len(self._items))
            self._cond.notify()

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def get(self) -> QueuedFrame:
        """Dequeue the oldest frame, blocking until one is available.

        Raises:
            QueueClosedError: if the queue is shut down and empty (drained).
        """
        async with self._cond:
            while not self._items:
                if self._closed:
                    raise QueueClosedError("get(): frame queue is shut down and drained")
                await self._cond.wait()
            item = self._items.popleft()
            self._total_dequeued += 1
            self._cond.notify()
            return item

    # ------------------------------------------------------------------
    # Shutdown & lifecycle
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop new production and wake all blocked producers/consumers.

        Idempotent.  Remaining items may still be drained by ``get()``;
        once empty, ``get()`` raises ``QueueClosedError``.
        """
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Observability hooks
    # ------------------------------------------------------------------

    @property
    def qsize(self) -> int:
        """Number of frames currently queued (best-effort read)."""
        return len(self._items)

    @property
    def dropped_frames(self) -> int:
        """Total frames evicted by the DROP_OLDEST policy (lifetime)."""
        return self._dropped_frames

    @property
    def total_enqueued(self) -> int:
        """Total frames admitted by put() (lifetime)."""
        return self._total_enqueued

    @property
    def total_dequeued(self) -> int:
        """Total frames returned by get() (lifetime)."""
        return self._total_dequeued

    @property
    def max_observed(self) -> int:
        """Peak queue occupancy observed (lifetime)."""
        return self._max_observed

    async def stats(self) -> QueueStats:
        """Atomic snapshot of all counters for observability hooks."""
        async with self._cond:
            return QueueStats(
                qsize=len(self._items),
                maxsize=self._maxsize,
                full_policy=self._full_policy,
                closed=self._closed,
                total_enqueued=self._total_enqueued,
                total_dequeued=self._total_dequeued,
                dropped_frames=self._dropped_frames,
                max_observed=self._max_observed,
            )
