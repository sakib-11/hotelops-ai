"""Tests for the inference execution policy (Task 12, Phase 6).

Covers the explicit policy contract:

- CPU/GPU: explicit configuration, availability validation, deterministic
  single-shot startup, exposed selected device, no silent device change.
- Warmup: explicit invocation (never simulated), measurable duration,
  typed failure handling.
- Batching: bounded batch size, explicit configuration, chunked bounded
  memory, per-input provenance preservation, batching only when
  appropriate.
- Cancellation propagation and resource cleanup / restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from backend.app.intelligence.detectors import (
    DetectorConfig,
    Device,
    InferenceError,
    InferenceExecutionError,
    InferenceExecutionPolicy,
    UnsupportedDeviceError,
)
from backend.app.intelligence.detectors.base import DetectionInput, ModelSpec
from contracts.common import (
    DetectionId,
    FrameId,
    VideoSessionId,
    new_uuid,
    utc_now,
)
from contracts.video import FramePacket
from contracts.vision import BoundingBox, DetectionObservation

# ---------------------------------------------------------------------------
# Helpers / test doubles
# ---------------------------------------------------------------------------


def make_frame(*, frame_index: int = 0) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=VideoSessionId(new_uuid()),
        frame_index=frame_index,
        event_time=utc_now(),
        width=1920,
        height=1080,
    )


def make_input(*, frame: FramePacket | None = None) -> DetectionInput:
    return DetectionInput(frame=frame or make_frame(), image=b"frame-bytes")


def make_spec() -> ModelSpec:
    return ModelSpec(
        model_id="test-detector",
        model_name="test-detector",
        model_version="1.0.0",
        artifact_uri="memory://test",
        artifact_sha256="b" * 64,
        device=Device.CPU,
        class_names=("person",),
    )


def make_detection(frame: FramePacket) -> DetectionObservation:
    """A provenance-valid observation for ``frame``."""
    return DetectionObservation(
        detection_id=DetectionId(new_uuid()),
        frame_id=frame.frame_id,
        session_id=frame.session_id,
        source_ref=frame.source_ref,
        frame_index=frame.frame_index,
        class_name="person",
        confidence=0.9,
        bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
        event_time=frame.event_time,
    )


class RecordingDetector:
    """Sequential detector double: records calls, optional device/drift/errors."""

    def __init__(
        self,
        *,
        device: str | None = "cpu",
        warmup_error: Exception | None = None,
        detect_error: Exception | None = None,
    ) -> None:
        self._spec = make_spec()
        self._device = device
        self._warmup_error = warmup_error
        self._detect_error = detect_error
        self.warmup_calls = 0
        self.detect_calls = 0

    @property
    def model_spec(self) -> ModelSpec:
        return self._spec

    @property
    def device(self) -> str | None:
        return self._device

    async def warmup(self) -> None:
        self.warmup_calls += 1
        if self._warmup_error is not None:
            raise self._warmup_error

    async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
        self.detect_calls += 1
        if self._detect_error is not None:
            raise self._detect_error
        return [make_detection(inp.frame)]


class BatchRecordingDetector(RecordingDetector):
    """Batch-capable detector double: records chunk sizes, can violate the contract."""

    def __init__(
        self,
        *,
        device: str | None = "cpu",
        mismatch: bool = False,
        wrong_frame: bool = False,
    ) -> None:
        super().__init__(device=device)
        self.batch_calls = 0
        self.batch_sizes: list[int] = []
        self._mismatch = mismatch
        self._wrong_frame = wrong_frame

    async def detect_batch(
        self, inputs: Sequence[DetectionInput]
    ) -> list[list[DetectionObservation]]:
        self.batch_calls += 1
        self.batch_sizes.append(len(inputs))
        if self._mismatch:
            return []
        results: list[list[DetectionObservation]] = []
        for inp in inputs:
            frame = (
                make_frame(frame_index=inp.frame.frame_index + 100)
                if self._wrong_frame
                else inp.frame
            )
            results.append([make_detection(frame)])
        return results


class FailSecondDetector(RecordingDetector):
    """Per-frame detector double that fails on its second input."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_on_call = 2

    async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
        self.detect_calls += 1
        if self.detect_calls == self._fail_on_call:
            raise InferenceError("frame 2 failed")
        return [make_detection(inp.frame)]


class CancellableDetector(RecordingDetector):
    """Detector double whose detect() blocks until released (for cancellation)."""

    def __init__(self) -> None:
        super().__init__()
        self._release = asyncio.Event()

    async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
        self.detect_calls += 1
        await self._release.wait()
        return []

    def release(self) -> None:
        self._release.set()


class CancellableWarmupDetector(RecordingDetector):
    """Detector double whose warmup() blocks until released (for cancellation)."""

    def __init__(self) -> None:
        super().__init__()
        self._release = asyncio.Event()

    async def warmup(self) -> None:
        self.warmup_calls += 1
        await self._release.wait()

    def release(self) -> None:
        self._release.set()


class BlockingBatchDetector(RecordingDetector):
    """Batch-capable double whose detect_batch blocks until released."""

    def __init__(self) -> None:
        super().__init__()
        self._release = asyncio.Event()
        self.batch_calls = 0

    async def detect_batch(
        self, inputs: Sequence[DetectionInput]
    ) -> list[list[DetectionObservation]]:
        self.batch_calls += 1
        await self._release.wait()
        return [[] for _ in inputs]

    def release(self) -> None:
        self._release.set()


async def make_started_policy(
    config: DetectorConfig | None = None, *, cuda: bool = False, mps: bool = False
) -> InferenceExecutionPolicy:
    policy = InferenceExecutionPolicy(config=config or DetectorConfig(device=Device.CPU))
    await policy.startup(cuda_available=cuda, mps_available=mps)
    return policy


# ---------------------------------------------------------------------------
# CPU/GPU startup
# ---------------------------------------------------------------------------


class TestStartup:
    async def test_cpu_startup_selects_cpu(self) -> None:
        policy = await make_started_policy()
        assert policy.started is True
        assert policy.selected_device == "cpu"
        assert policy.batch_size == 1

    async def test_gpu_startup_when_available(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CUDA), cuda=True)
        assert policy.selected_device == "cuda:0"

    async def test_auto_prefers_available_gpu(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.AUTO), cuda=False, mps=True)
        assert policy.selected_device == "mps"
        policy2 = await make_started_policy(DetectorConfig(device=Device.AUTO), cuda=True)
        assert policy2.selected_device == "cuda:0"

    async def test_unavailable_gpu_fails_startup_deterministically(self) -> None:
        policy = InferenceExecutionPolicy(config=DetectorConfig(device=Device.CUDA))
        with pytest.raises(UnsupportedDeviceError):
            await policy.startup(cuda_available=False, mps_available=False)
        assert policy.started is False
        assert policy.selected_device is None
        # Retry with the GPU available now selects it.
        await policy.startup(cuda_available=True, mps_available=False)
        assert policy.selected_device == "cuda:0"

    async def test_gpu_to_cpu_fallback_requires_explicit_configuration(self) -> None:
        """The no-silent-fallback rule, proven both ways at the policy level.

        Without ``allow_cpu_fallback`` a requested-but-unavailable GPU
        fails startup; with it explicitly enabled the policy selects CPU.
        The device never falls back on its own.
        """
        policy = InferenceExecutionPolicy(config=DetectorConfig(device=Device.CUDA))
        with pytest.raises(UnsupportedDeviceError):
            await policy.startup(cuda_available=False, mps_available=False)
        explicit = await make_started_policy(
            DetectorConfig(device=Device.CUDA, allow_cpu_fallback=True), cuda=False
        )
        assert explicit.selected_device == "cpu"

    def test_invalid_device_rejected_at_configuration(self) -> None:
        with pytest.raises(ValueError, match="device"):
            DetectorConfig(device="gpu")  # type: ignore[arg-type]

    async def test_startup_is_single_shot_and_never_changes_device(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CUDA), cuda=True)
        assert policy.selected_device == "cuda:0"
        # A later startup with different availability must NOT change it.
        await policy.startup(cuda_available=False, mps_available=False)
        assert policy.selected_device == "cuda:0"
        assert policy.started is True


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------


class TestWarmup:
    async def test_warmup_invokes_detector_and_is_measurable(self) -> None:
        policy = await make_started_policy()
        detector = RecordingDetector()
        await policy.run_warmup(detector)
        assert detector.warmup_calls == 1  # real warmup — never simulated
        assert policy.warmup_duration_seconds is not None
        assert policy.warmup_duration_seconds >= 0

    async def test_warmup_requires_startup(self) -> None:
        policy = InferenceExecutionPolicy()
        with pytest.raises(InferenceExecutionError, match="not started"):
            await policy.run_warmup(RecordingDetector())

    async def test_warmup_failure_is_typed_with_cause(self) -> None:
        policy = await make_started_policy()
        detector = RecordingDetector(warmup_error=RuntimeError("warmup boom"))
        with pytest.raises(InferenceExecutionError) as excinfo:
            await policy.run_warmup(detector)
        assert isinstance(excinfo.value.cause, RuntimeError)
        # Warmup failure prevents readiness: no warmup duration recorded.
        assert policy.warmup_duration_seconds is None

    async def test_warmup_never_produces_detections(self) -> None:
        """Warmup primes the detector only: no business observations."""
        policy = await make_started_policy()
        detector = RecordingDetector()
        await policy.run_warmup(detector)
        assert detector.warmup_calls == 1
        assert detector.detect_calls == 0  # no inference, no observations


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class TestBatching:
    async def test_batch_size_one_is_strictly_sequential(self) -> None:
        policy = await make_started_policy()  # batch_size == 1 (default)
        detector = BatchRecordingDetector()
        inputs = [make_input() for _ in range(3)]
        results = await policy.execute(detector, inputs)
        assert len(results) == 3
        assert detector.detect_calls == 3  # per-frame, even though batch-capable
        assert detector.batch_calls == 0

    async def test_configured_batch_size_chunks_bounded(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=2))
        detector = BatchRecordingDetector()
        inputs = [make_input() for _ in range(5)]
        results = await policy.execute(detector, inputs)
        assert len(results) == 5
        assert detector.batch_sizes == [2, 2]  # two genuine batches
        assert detector.detect_calls == 1  # the leftover single frame
        # Provenance preserved per input.
        assert results[0][0].frame_id == inputs[0].frame.frame_id
        assert results[4][0].frame_id == inputs[4].frame.frame_id

    async def test_batching_not_used_for_incapable_detector(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=4))
        detector = RecordingDetector()  # no detect_batch
        inputs = [make_input() for _ in range(3)]
        assert policy.is_batching_appropriate(detector, len(inputs)) is False
        results = await policy.execute(detector, inputs)
        assert len(results) == 3
        assert detector.detect_calls == 3

    async def test_is_batching_appropriate_requires_all_conditions(self) -> None:
        capable = BatchRecordingDetector()
        sequential = RecordingDetector()
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=2))
        # batch_size == 1 -> never.
        policy_one = await make_started_policy()
        assert policy_one.is_batching_appropriate(capable, 5) is False
        # single input -> never.
        assert policy.is_batching_appropriate(capable, 1) is False
        # not batch-capable -> never.
        assert policy.is_batching_appropriate(sequential, 5) is False
        # fully appropriate.
        assert policy.is_batching_appropriate(capable, 2) is True

    async def test_empty_batch_is_noop(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=4))
        detector = BatchRecordingDetector()
        assert await policy.execute(detector, []) == []
        assert detector.detect_calls == 0
        assert detector.batch_calls == 0

    async def test_oversized_batch_is_chunked_with_bounded_memory(self) -> None:
        batch_size = 3
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=batch_size))
        detector = BatchRecordingDetector()
        inputs = [make_input() for _ in range(10)]
        results = await policy.execute(detector, inputs)
        assert len(results) == 10
        assert detector.batch_sizes == [3, 3, 3]
        assert detector.detect_calls == 1
        # In-flight memory is bounded by the configured batch size.
        assert max(detector.batch_sizes) <= batch_size

    async def test_batch_result_count_mismatch_is_policy_error(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=2))
        detector = BatchRecordingDetector(mismatch=True)
        with pytest.raises(InferenceExecutionError, match="returned 0 results"):
            await policy.execute(detector, [make_input(), make_input()])

    async def test_batch_provenance_violation_is_policy_error(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=2))
        detector = BatchRecordingDetector(wrong_frame=True)
        with pytest.raises(InferenceExecutionError, match="provenance"):
            await policy.execute(detector, [make_input(), make_input()])

    async def test_execute_requires_startup(self) -> None:
        policy = InferenceExecutionPolicy()
        with pytest.raises(InferenceExecutionError, match="not started"):
            await policy.execute(RecordingDetector(), [make_input()])

    async def test_per_frame_inference_failure_propagates_unchanged(self) -> None:
        policy = await make_started_policy()
        detector = RecordingDetector(detect_error=InferenceError("frame failed"))
        with pytest.raises(InferenceError, match="frame failed"):
            await policy.execute(detector, [make_input()])

    async def test_group_failure_returns_no_partial_results(self) -> None:
        """A failure anywhere in the group fails the group atomically.

        No partial business state escapes: the first frame's detections
        are never handed out when the second frame fails.
        """
        policy = await make_started_policy()
        detector = FailSecondDetector()
        with pytest.raises(InferenceError, match="frame 2 failed"):
            await policy.execute(detector, [make_input(), make_input()])
        assert detector.detect_calls == 2  # both frames were attempted
        # The failed frame is identifiable (typed error with context) and
        # the policy remains usable afterwards.
        results = await policy.execute(detector, [make_input()])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Device stability (no silent device change)
# ---------------------------------------------------------------------------


class TestDeviceStability:
    async def test_device_drift_fails_execution_loudly(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CUDA), cuda=True)
        assert policy.selected_device == "cuda:0"
        detector = RecordingDetector(device="cpu")  # drifted
        with pytest.raises(InferenceExecutionError, match="differs from policy-selected"):
            await policy.execute(detector, [make_input()])

    async def test_device_drift_fails_warmup_loudly(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CUDA), cuda=True)
        detector = RecordingDetector(device="cpu")
        with pytest.raises(InferenceExecutionError, match="differs from policy-selected"):
            await policy.run_warmup(detector)

    async def test_matching_device_executes_normally(self) -> None:
        policy = await make_started_policy()
        results = await policy.execute(RecordingDetector(device="cpu"), [make_input()])
        assert len(results) == 1
        assert len(results[0]) == 1

    async def test_detector_without_exposed_device_is_not_rejected(self) -> None:
        policy = await make_started_policy()
        results = await policy.execute(RecordingDetector(device=None), [make_input()])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Cancellation + cleanup
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_cancellation_propagates_and_policy_stays_usable(self) -> None:
        policy = await make_started_policy()
        detector = CancellableDetector()
        task = asyncio.create_task(policy.execute(detector, [make_input()]))
        await asyncio.sleep(0)  # let detect() reach its await point
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The policy remains usable after cancellation.
        detector.release()
        results = await policy.execute(detector, [make_input()])
        assert results == [[]]

    async def test_cancellation_before_inference_calls_no_detector(self) -> None:
        policy = await make_started_policy()
        detector = RecordingDetector()
        task = asyncio.create_task(policy.execute(detector, [make_input()]))
        task.cancel()  # cancelled before the task ever runs
        with pytest.raises(asyncio.CancelledError):
            await task
        # No inference was started; nothing was leaked.
        assert detector.detect_calls == 0
        # The policy remains usable.
        results = await policy.execute(detector, [make_input()])
        assert len(results) == 1

    async def test_cancellation_during_warmup_propagates_and_stays_usable(self) -> None:
        policy = await make_started_policy()
        detector = CancellableWarmupDetector()
        task = asyncio.create_task(policy.run_warmup(detector))
        await asyncio.sleep(0)  # let warmup() reach its await point
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Warmup never completed: the policy is NOT marked ready.
        assert policy.warmup_duration_seconds is None
        # The policy remains usable — a completed warmup records normally.
        detector.release()
        await policy.run_warmup(detector)
        assert policy.warmup_duration_seconds is not None

    async def test_cancellation_during_active_batch_propagates(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CPU, batch_size=4))
        detector = BlockingBatchDetector()
        inputs = [make_input() for _ in range(3)]
        task = asyncio.create_task(policy.execute(detector, inputs))
        await asyncio.sleep(0)  # let detect_batch() reach its await point
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The policy remains usable after cancellation.
        detector.release()
        results = await policy.execute(detector, [make_input()])
        assert len(results) == 1

    async def test_shutdown_resets_and_restart_is_deterministic(self) -> None:
        policy = await make_started_policy(DetectorConfig(device=Device.CUDA), cuda=True)
        assert policy.selected_device == "cuda:0"
        await policy.shutdown()
        assert policy.started is False
        assert policy.selected_device is None
        assert policy.warmup_duration_seconds is None
        with pytest.raises(InferenceExecutionError, match="not started"):
            await policy.execute(RecordingDetector(), [make_input()])
        # Deterministic restart.
        await policy.startup(cuda_available=True, mps_available=False)
        assert policy.selected_device == "cuda:0"
        results = await policy.execute(RecordingDetector(device="cuda:0"), [make_input()])
        assert len(results) == 1
