"""FramePipeline — the source-agnostic ingestion → CV boundary (Task 11, Phase 7).

Wires ANY ``FrameSource`` (live ``RTSPFrameSource`` or recorded
``FileFrameSource``) through a ``BoundedFrameQueue`` into the downstream
CV processing boundary:

    FileFrameSource ──┐
                      ├──> FrameSource ──> BoundedFrameQueue ──> FrameConsumer
    RTSPFrameSource ──┘

The downstream ``FrameConsumer`` consumes canonical ``(FramePacket,
FrameData)`` pairs and MUST NOT branch on source type — the
``FramePacket`` schema carries no live/recorded discriminator, and this
module never exposes a concrete source class to the consumer
(ADR-005: shared live/recorded pipeline).

``FramePipeline`` depends only on the abstract ``FrameSource`` contract
and the canonical ``FramePacket`` (``contracts.video``) — it has no
knowledge of storage, RTSP transports, or decoders.  Queue-full policy
is the caller's explicit choice (``BoundedFrameQueue`` requires it; see
``queue.py`` for the Task 1 SLO grounding of BLOCK vs DROP_OLDEST).
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from backend.app.intelligence.sources.base import FrameSource
from backend.app.intelligence.sources.exceptions import FrameSourceError, QueueClosedError
from backend.app.intelligence.sources.queue import BoundedFrameQueue, QueuedFrame

__all__ = ["FrameConsumer", "FramePipeline"]


@runtime_checkable
class FrameConsumer(Protocol):
    """Downstream CV processing boundary.

    Consumes canonical ``(FramePacket, FrameData)`` pairs exactly as they
    cross the bounded queue.  Implementations MUST NOT branch on the
    source type: live and recorded ingestion are indistinguishable here
    by design (ADR-005).
    """

    async def consume(self, frame: QueuedFrame) -> None:
        """Process one (FramePacket, FrameData) pair from the pipeline."""
        ...


class FramePipeline:
    """Runs any ``FrameSource`` through a bounded queue into a ``FrameConsumer``.

    Owns the producer (source → queue) and consumer (queue → CV) tasks,
    their coordination, and guaranteed cleanup on EOF, failure, and
    cancellation.  The source lifecycle is managed via ``async with``
    (open on entry, aclose on every exit path).
    """

    def __init__(
        self,
        *,
        queue: BoundedFrameQueue,
        consumer: FrameConsumer,
    ) -> None:
        self._queue = queue
        self._consumer = consumer

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def queue(self) -> BoundedFrameQueue:
        """The pipeline's bounded frame queue (stats/drop observability)."""
        return self._queue

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def run(self, source: FrameSource) -> None:
        """Stream frames from any ``FrameSource`` into the CV consumer.

        Lifecycle:

        - opens the source, pumps ``(FramePacket, FrameData)`` pairs into
          the queue (producer task) and forwards them to the consumer
          (consumer task);
        - at EOF shuts the queue down and lets the consumer drain the
          remaining frames before returning;
        - on a source failure (e.g. ``SourceTerminatedError``) or a
          consumer failure, cancels the sibling task, shuts the queue
          down, and re-raises the original error;
        - on cancellation, releases the source and the queue, and lets
          ``asyncio.CancelledError`` propagate.

        Raises:
            QueueClosedError: if the queue was already shut down.
            FrameSourceError: if the source produced a packet without
                companion ``FrameData``.
            SourceTerminatedError / consumer errors: propagated unchanged.
        """
        if self._queue.closed:
            raise QueueClosedError("cannot run pipeline: frame queue is shut down")
        async with source:  # open + guaranteed aclose on every path
            producer = asyncio.create_task(self._produce(source))
            consumer = asyncio.create_task(self._consume())
            try:
                await asyncio.wait({producer, consumer}, return_when=asyncio.FIRST_COMPLETED)
                # The producer finished (EOF) or a side failed: stop new
                # production and let the consumer drain what remains.
                await self._queue.shutdown()
                await asyncio.wait({producer, consumer}, return_when=asyncio.ALL_COMPLETED)
            finally:
                # Guaranteed cleanup on failure/cancellation: cancel any
                # straggler and suppress its exit, then close the queue.
                for task in (producer, consumer):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(producer, consumer, return_exceptions=True)
                await self._queue.shutdown()
            # Re-raise the first real failure (skipping the expected
            # QueueClosedError that signals a clean drain).
            failure = self._first_real_failure(producer, consumer)
            if failure is not None:
                raise failure

    async def _produce(self, source: FrameSource) -> None:
        """Producer: iterate the source and enqueue (packet, data) pairs."""
        try:
            async for packet in source:
                data = source.last_frame_data
                if data is None:
                    raise FrameSourceError(
                        "source produced a FramePacket without companion FrameData"
                    )
                dropped_before = self._queue.dropped_frames
                await self._queue.put(QueuedFrame(packet=packet, data=data))
                # Keep the source's drop counter in sync with the queue's
                # (DROP_OLDEST evictions are observable on both sides).
                dropped = self._queue.dropped_frames - dropped_before
                for _ in range(dropped):
                    source.note_dropped()
        except QueueClosedError:
            # Only the pipeline's own shutdown is benign; a consumer that
            # raises QueueClosedError itself (it is a public queue
            # exception) must still surface as a failure.
            if self._queue.closed:
                return
            raise

    async def _consume(self) -> None:
        """Consumer: forward queued frames to the CV boundary."""
        try:
            while True:
                frame = await self._queue.get()
                await self._consumer.consume(frame)
        except QueueClosedError:
            if self._queue.closed:
                return  # drained — normal completion
            raise

    @staticmethod
    def _first_real_failure(
        producer: asyncio.Task[None], consumer: asyncio.Task[None]
    ) -> BaseException | None:
        """Return the first non-Cancelled, non-QueueClosed task failure.

        Deterministic preference: the producer (source) failure wins over
        a simultaneous consumer failure — the ingestion side is the
        boundary the caller is most likely to need to act on.  Only
        reached on the normal path (both tasks done, neither cancelled),
        so ``task.exception()`` is safe to call.
        """
        for task in (producer, consumer):
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, QueueClosedError):
                continue  # expected during a clean drain/shutdown
            return exc
        return None
