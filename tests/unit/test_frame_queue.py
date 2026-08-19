"""Unit tests for BoundedFrameQueue (Task 11, Phase 5).

Covers the 10 required scenarios: normal enqueue, normal dequeue,
capacity reached, queue-full behavior (both BLOCK and DROP_OLDEST),
consumer cancellation, producer cancellation, shutdown, blocked
producer, blocked consumer, and resource cleanup.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.intelligence.sources.base import FrameData
from backend.app.intelligence.sources.exceptions import QueueClosedError
from backend.app.intelligence.sources.queue import (
    BoundedFrameQueue,
    QueuedFrame,
    QueueFullPolicy,
    QueueStats,
)
from contracts.common import FrameId, VideoSessionId
from contracts.video import FramePacket


def make_session_id() -> VideoSessionId:
    return VideoSessionId(uuid4())


def make_queued_frame(index: int) -> QueuedFrame:
    packet = FramePacket(
        frame_id=FrameId(uuid4()),
        session_id=make_session_id(),
        frame_index=index,
        event_time=datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        width=1920,
        height=1080,
    )
    data = FrameData(
        frame_index=index,
        width=1920,
        height=1080,
        data=bytes([index % 256]) * 16,
        pts_seconds=float(index),
    )
    return QueuedFrame(packet=packet, data=data)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_explicit_full_policy(self) -> None:
        # full_policy is a required keyword argument — the queue never
        # silently chooses a queue-full strategy.
        with pytest.raises(TypeError):
            BoundedFrameQueue(maxsize=10)  # type: ignore[call-arg]

    def test_rejects_zero_capacity(self) -> None:
        with pytest.raises(ValueError, match="maxsize"):
            BoundedFrameQueue(maxsize=0, full_policy=QueueFullPolicy.BLOCK)

    def test_rejects_negative_capacity(self) -> None:
        with pytest.raises(ValueError, match="maxsize"):
            BoundedFrameQueue(maxsize=-1, full_policy=QueueFullPolicy.DROP_OLDEST)


# ---------------------------------------------------------------------------
# Normal enqueue / dequeue
# ---------------------------------------------------------------------------


class TestNormalFlow:
    async def test_normal_enqueue(self) -> None:
        queue = BoundedFrameQueue(maxsize=10, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))
        assert queue.qsize == 2
        assert queue.total_enqueued == 2

    async def test_normal_dequeue_fifo(self) -> None:
        queue = BoundedFrameQueue(maxsize=10, full_policy=QueueFullPolicy.BLOCK)
        first = make_queued_frame(0)
        second = make_queued_frame(1)
        await queue.put(first)
        await queue.put(second)

        got_first = await queue.get()
        got_second = await queue.get()

        assert got_first.packet.frame_index == 0
        assert got_second.packet.frame_index == 1
        assert got_first.packet is first.packet
        assert got_second.data is second.data
        assert queue.qsize == 0
        assert queue.total_dequeued == 2

    async def test_queue_reaches_capacity(self) -> None:
        queue = BoundedFrameQueue(maxsize=3, full_policy=QueueFullPolicy.BLOCK)
        for i in range(3):
            await queue.put(make_queued_frame(i))
        assert queue.qsize == 3
        assert queue.max_observed == 3

    async def test_roundtrip_preserves_pair(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        original = make_queued_frame(7)
        await queue.put(original)
        got = await queue.get()
        assert got.packet.frame_id == original.packet.frame_id
        assert got.data.data == original.data.data
        assert got.data.pts_seconds == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Queue-full behavior
# ---------------------------------------------------------------------------


class TestQueueFull:
    async def test_block_policy_blocks_producer_at_capacity(self) -> None:
        queue = BoundedFrameQueue(maxsize=2, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))

        # A third put must block (queue is full, policy is BLOCK).
        producer = asyncio.create_task(queue.put(make_queued_frame(2)))
        await asyncio.sleep(0.01)
        assert not producer.done()
        assert queue.qsize == 2

        # Unblock it by draining.
        await queue.get()
        await producer
        assert queue.qsize == 2  # one new item took the freed slot
        assert queue.total_enqueued == 3

    async def test_drop_oldest_policy_evicts_oldest_and_counts(self) -> None:
        queue = BoundedFrameQueue(maxsize=2, full_policy=QueueFullPolicy.DROP_OLDEST)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))
        # Never blocks; evicts frame 0 and admits frame 2.
        await queue.put(make_queued_frame(2))

        assert queue.dropped_frames == 1
        assert queue.qsize == 2
        assert queue.total_enqueued == 3
        assert (await queue.get()).packet.frame_index == 1
        assert (await queue.get()).packet.frame_index == 2

    async def test_drop_oldest_never_blocks(self) -> None:
        queue = BoundedFrameQueue(maxsize=1, full_policy=QueueFullPolicy.DROP_OLDEST)
        for i in range(50):
            await queue.put(make_queued_frame(i))
        assert queue.qsize == 1
        assert queue.dropped_frames == 49
        assert (await queue.get()).packet.frame_index == 49


# ---------------------------------------------------------------------------
# Blocked producer / consumer
# ---------------------------------------------------------------------------


class TestBlocked:
    async def test_blocked_producer_unblocks_on_consume(self) -> None:
        queue = BoundedFrameQueue(maxsize=1, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        producer = asyncio.create_task(queue.put(make_queued_frame(1)))
        await asyncio.sleep(0.01)
        assert not producer.done()

        item = await queue.get()
        assert item.packet.frame_index == 0
        await producer  # completes after capacity freed
        assert queue.qsize == 1

    async def test_one_get_wakes_exactly_one_blocked_producer(self) -> None:
        """Regression: get() must notify exactly one waiter (no lost wakeups)."""
        queue = BoundedFrameQueue(maxsize=2, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))

        producer_a = asyncio.create_task(queue.put(make_queued_frame(2)))
        producer_b = asyncio.create_task(queue.put(make_queued_frame(3)))
        await asyncio.sleep(0.01)
        assert not producer_a.done() and not producer_b.done()

        await queue.get()  # frees exactly one slot
        await asyncio.sleep(0.01)
        completed = sum(1 for p in (producer_a, producer_b) if p.done())
        assert completed == 1  # exactly one producer proceeded
        assert queue.qsize == 2

        await queue.get()  # frees the second slot
        await asyncio.sleep(0.01)
        assert producer_a.done() and producer_b.done()
        assert queue.qsize == 2

    async def test_blocked_consumer_unblocks_on_put(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0.01)
        assert not consumer.done()

        await queue.put(make_queued_frame(0))
        item = await consumer
        assert item.packet.frame_index == 0

    async def test_blocked_consumer_without_timeout_wait(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0.01)
        assert not consumer.done()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_consumer_cancellation_leaves_queue_intact(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0.01)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        # The queue still works normally afterwards.
        await queue.put(make_queued_frame(0))
        assert (await queue.get()).packet.frame_index == 0

    async def test_producer_cancellation_leaves_queue_intact(self) -> None:
        queue = BoundedFrameQueue(maxsize=1, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        producer = asyncio.create_task(queue.put(make_queued_frame(1)))
        await asyncio.sleep(0.01)
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer

        # Nothing was enqueued by the cancelled producer; queue is intact.
        assert queue.qsize == 1
        assert queue.total_enqueued == 1
        assert (await queue.get()).packet.frame_index == 0


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_put_after_shutdown_rejected(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        await queue.shutdown()
        with pytest.raises(QueueClosedError, match="shut down"):
            await queue.put(make_queued_frame(0))
        assert queue.closed is True

    async def test_drop_oldest_put_after_shutdown_rejected_without_evicting(self) -> None:
        """DROP_OLDEST must never evict a frame on a shut-down queue."""
        queue = BoundedFrameQueue(maxsize=2, full_policy=QueueFullPolicy.DROP_OLDEST)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))
        await queue.shutdown()

        with pytest.raises(QueueClosedError, match="shut down"):
            await queue.put(make_queued_frame(2))

        # No eviction and no admission happened on the shut-down queue.
        assert queue.dropped_frames == 0
        assert queue.total_enqueued == 2
        assert queue.qsize == 2
        assert (await queue.get()).packet.frame_index == 0
        assert (await queue.get()).packet.frame_index == 1

    async def test_shutdown_wakes_blocked_producer(self) -> None:
        queue = BoundedFrameQueue(maxsize=1, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        producer = asyncio.create_task(queue.put(make_queued_frame(1)))
        await asyncio.sleep(0.01)
        assert not producer.done()

        await queue.shutdown()
        with pytest.raises(QueueClosedError, match="shut down"):
            await producer

    async def test_shutdown_wakes_blocked_consumer(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0.01)
        assert not consumer.done()

        await queue.shutdown()
        with pytest.raises(QueueClosedError, match="shut down"):
            await consumer

    async def test_shutdown_allows_drain_then_closed(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))
        await queue.shutdown()

        assert (await queue.get()).packet.frame_index == 0
        assert (await queue.get()).packet.frame_index == 1
        with pytest.raises(QueueClosedError, match="shut down"):
            await queue.get()

    async def test_shutdown_is_idempotent(self) -> None:
        queue = BoundedFrameQueue(maxsize=5, full_policy=QueueFullPolicy.BLOCK)
        await queue.shutdown()
        await queue.shutdown()  # second shutdown is a no-op
        with pytest.raises(QueueClosedError):
            await queue.put(make_queued_frame(0))


# ---------------------------------------------------------------------------
# Resource cleanup
# ---------------------------------------------------------------------------


class TestResourceCleanup:
    async def test_no_leaked_waiters_after_shutdown(self) -> None:
        queue = BoundedFrameQueue(maxsize=1, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        producer = asyncio.create_task(queue.put(make_queued_frame(1)))
        await asyncio.sleep(0.01)
        assert not producer.done()  # genuinely blocked on the full queue

        await queue.shutdown()
        with pytest.raises(QueueClosedError):
            await producer
        assert producer.done()  # waiter resolved; nothing dangling

        # The remaining item is still drainable after shutdown.
        assert (await queue.get()).packet.frame_index == 0
        with pytest.raises(QueueClosedError):
            await queue.get()

    async def test_producer_unblocked_by_consumer_before_shutdown(self) -> None:
        """Capacity freed by a consumer before shutdown legitimately unblocks a producer."""
        queue = BoundedFrameQueue(maxsize=1, full_policy=QueueFullPolicy.BLOCK)
        await queue.put(make_queued_frame(0))
        producer = asyncio.create_task(queue.put(make_queued_frame(1)))
        await asyncio.sleep(0.01)
        assert not producer.done()

        assert (await queue.get()).packet.frame_index == 0  # frees capacity
        await producer  # producer completes with a real enqueue
        assert queue.qsize == 1
        assert (await queue.get()).packet.frame_index == 1

    async def test_stats_snapshot(self) -> None:
        queue = BoundedFrameQueue(maxsize=2, full_policy=QueueFullPolicy.DROP_OLDEST)
        await queue.put(make_queued_frame(0))
        await queue.put(make_queued_frame(1))
        await queue.put(make_queued_frame(2))  # drops frame 0
        await queue.get()

        stats: QueueStats = await queue.stats()
        assert stats.qsize == 1
        assert stats.maxsize == 2
        assert stats.full_policy is QueueFullPolicy.DROP_OLDEST
        assert stats.closed is False
        assert stats.total_enqueued == 3
        assert stats.total_dequeued == 1
        assert stats.dropped_frames == 1
        assert stats.max_observed == 2

    async def test_observability_properties(self) -> None:
        queue = BoundedFrameQueue(maxsize=3, full_policy=QueueFullPolicy.BLOCK)
        assert queue.maxsize == 3
        assert queue.full_policy is QueueFullPolicy.BLOCK
        assert queue.closed is False
        assert queue.qsize == 0
        assert queue.dropped_frames == 0
        assert queue.total_enqueued == 0
        assert queue.total_dequeued == 0
        assert queue.max_observed == 0
