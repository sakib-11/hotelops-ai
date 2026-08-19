"""Negative and failure testing for the object-detection boundary (Task 12, Phase 9).

A systematic failure-path matrix for the generic ``ObjectDetector``
abstraction and its ``YOLOv8Adapter``.  No features are added here —
every scenario exercises existing production behavior and asserts the
cross-cutting failure contract:

- **explicit failure**       — failures raise typed ``DetectionError``
                              subtypes (never silent, never generic);
- **useful diagnostics**     — error messages carry the model name,
                              artifact URI, device, frame index, or the
                              offending value;
- **no resource leak**       — ``close()`` releases state even after a
                              failed load; repeated failure cycles stay
                              idempotent;
- **no invalid output**      — a failure NEVER yields a partial or
                              fabricated detection list;
- **no fake provenance**     — every emitted observation copies
                              provenance verbatim from its ``FramePacket``
                              (validated via ``validate_detection_provenance``);
- **no silent fallback**     — an unavailable GPU is an error unless
                              ``allow_cpu_fallback`` is explicitly set;
- **no corrupted state**     — the detector/policy remain usable after
                              every failure class (load, detect, batch,
                              warmup, cancellation).

Production code is NOT weakened to make these tests pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from backend.app.intelligence.detectors import (
    DetectionInput,
    DetectorConfig,
    Device,
    InferenceError,
    InvalidGeometryError,
    ModelArtifactCorruptError,
    ModelLoadError,
    ModelSpec,
    UnsupportedDeviceError,
    validate_detection_provenance,
    yolo_adapter,
)
from backend.app.intelligence.detectors.base import resolve_device
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from contracts.common import (
    FrameId,
    VideoSessionId,
    new_uuid,
    utc_now,
)
from contracts.video import FramePacket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_frame(*, frame_index: int = 0) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=VideoSessionId(new_uuid()),
        source_ref=None,
        frame_index=frame_index,
        event_time=utc_now(),
        width=1920,
        height=1080,
    )


def make_spec(
    *,
    model_name: str = "yolov8n",
    version: str = "8.1.0",
    class_names: tuple[str, ...] = ("person", "bag"),
) -> ModelSpec:
    return ModelSpec(
        model_id="yolov8n",
        model_name=model_name,
        model_version=version,
        artifact_uri="memory://fail-tests/yolov8n.pt",
        artifact_sha256="a" * 64,
        device=Device.CPU,
        class_names=class_names,
    )


def make_input(
    *, frame: FramePacket | None = None, image: bytes = b"\xff\xd8jpg"
) -> DetectionInput:
    return DetectionInput(frame=frame or make_frame(), image=image)


def make_adapter(
    *, spec: ModelSpec | None = None, config: DetectorConfig | None = None
) -> YOLOv8Adapter:
    return YOLOv8Adapter(model_spec=spec or make_spec(), config=config)


# ---------------------------------------------------------------------------
# Controllable fake SDK (same seam pattern as test_yolo_adapter.py)
# ---------------------------------------------------------------------------


class FakeBoxes:
    def __init__(
        self, xyxy: list[list[float]], conf: list[list[float]], cls: list[list[int]]
    ) -> None:
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls

    def __len__(self) -> int:
        return len(self.xyxy)


@dataclass
class FakeResult:
    boxes: FakeBoxes | None = None


def fake_boxes(*rows: tuple[float, float, float, float, float, int]) -> FakeBoxes:
    return FakeBoxes(
        [list(row[:4]) for row in rows],
        [[row[4]] for row in rows],
        [[row[5]] for row in rows],
    )


class FakeYOLO:
    """SDK double with per-test failure injection via class attributes."""

    instances: ClassVar[list[FakeYOLO]] = []
    constructor_error: ClassVar[Exception | None] = None
    predict_results: ClassVar[list[FakeResult]] = []
    predict_error: ClassVar[Exception | None] = None

    def __init__(self, artifact_uri: str) -> None:
        self.artifact_uri = artifact_uri
        self.names: dict[int, str] = {0: "person", 1: "bag"}
        if FakeYOLO.constructor_error is not None:
            raise FakeYOLO.constructor_error
        FakeYOLO.instances.append(self)

    def predict(self, **kwargs: Any) -> list[FakeResult]:
        if FakeYOLO.predict_error is not None:
            raise FakeYOLO.predict_error
        return FakeYOLO.predict_results


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake SDK + deterministic CPU-only seams, reset per test."""
    FakeYOLO.instances = []
    FakeYOLO.constructor_error = None
    FakeYOLO.predict_results = [FakeResult(fake_boxes((10, 20, 330, 470, 0.95, 0)))]
    FakeYOLO.predict_error = None
    module = types.ModuleType("ultralytics")
    module.YOLO = FakeYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_mps_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", lambda image: (object(), 640, 480))


# ===========================================================================
# Model failures: missing / corrupt / invalid / wrong version / load failure
#
# NOTE: a few load-failure cases overlap test_yolo_adapter.py::TestLoading
# (class mismatch, empty class table, missing SDK). The duplication is
# DELIBERATE — this suite is the consolidated negative matrix that also
# asserts diagnostics content and post-failure state integrity; keep it.
# ===========================================================================


class TestModelFailures:
    async def test_missing_sdk_is_typed_with_diagnostics(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # sys.modules["ultralytics"] = None makes import_module raise ImportError.
        monkeypatch.setitem(sys.modules, "ultralytics", None)
        with pytest.raises(ModelLoadError) as excinfo:
            await make_adapter().load()
        # Explicit failure + useful diagnostics.
        assert "SDK" in excinfo.value.message
        assert isinstance(excinfo.value.cause, ImportError)

    async def test_missing_artifact_is_typed_with_uri(self, fake_sdk: None) -> None:
        FakeYOLO.constructor_error = FileNotFoundError("no such artifact")
        spec = make_spec()
        with pytest.raises(ModelLoadError) as excinfo:
            await make_adapter(spec=spec).load()
        # Diagnostics carry the exact artifact URI.
        assert spec.artifact_uri in excinfo.value.message
        assert isinstance(excinfo.value.cause, FileNotFoundError)

    async def test_corrupt_artifact_constructor_failure_is_typed(self, fake_sdk: None) -> None:
        FakeYOLO.constructor_error = RuntimeError("corrupt weights file")
        with pytest.raises(ModelLoadError) as excinfo:
            await make_adapter().load()
        assert isinstance(excinfo.value.cause, RuntimeError)

    async def test_invalid_model_class_mismatch_is_typed(self, fake_sdk: None) -> None:
        # Declared spec classes disagree with the artifact's class table.
        adapter = make_adapter(spec=make_spec(class_names=("dog",)))
        with pytest.raises(ModelLoadError, match="class names") as excinfo:
            await adapter.load()
        assert "dog" in excinfo.value.message

    async def test_invalid_model_empty_class_table_is_typed(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class EmptyNamesYOLO(FakeYOLO):
            def __init__(self, artifact_uri: str) -> None:
                super().__init__(artifact_uri)
                self.names = {}

        module = types.ModuleType("ultralytics")
        module.YOLO = EmptyNamesYOLO
        monkeypatch.setitem(sys.modules, "ultralytics", module)
        with pytest.raises(ModelLoadError, match="class names"):
            await make_adapter().load()

    def test_invalid_model_version_rejected_at_configuration(self) -> None:
        # An empty/whitespace version is an invalid version — rejected
        # explicitly at ModelSpec construction, never silently defaulted.
        with pytest.raises(ValueError, match="model_version"):
            make_spec(version="  ")
        with pytest.raises(ValueError, match="model_version"):
            make_spec(version="")

    async def test_failed_load_leaves_no_corrupted_state(self, fake_sdk: None) -> None:
        # First load fails (corrupt artifact); state must stay clean.
        FakeYOLO.constructor_error = RuntimeError("boom")
        adapter = make_adapter()
        with pytest.raises(ModelLoadError):
            await adapter.load()
        assert adapter.loaded is False
        assert adapter.device is None
        # Retry after the artifact becomes available succeeds — no poisoned state.
        FakeYOLO.constructor_error = None
        await adapter.load()
        assert adapter.loaded is True
        assert adapter.device == "cpu"


# ===========================================================================
# Device failures: unavailable GPU / invalid device / no silent fallback
# ===========================================================================


class TestDeviceFailures:
    def test_unavailable_gpu_is_explicit_without_silent_fallback(self) -> None:
        with pytest.raises(UnsupportedDeviceError) as excinfo:
            resolve_device(
                Device.CUDA, allow_cpu_fallback=False, cuda_available=False, mps_available=False
            )
        # Diagnostics name the device and the fallback policy.
        assert "CUDA" in excinfo.value.message
        assert "allow_cpu_fallback=False" in excinfo.value.message

    def test_unavailable_mps_is_explicit_without_silent_fallback(self) -> None:
        with pytest.raises(UnsupportedDeviceError):
            resolve_device(
                Device.MPS, allow_cpu_fallback=False, cuda_available=False, mps_available=False
            )

    def test_gpu_fallback_only_when_explicitly_configured(self) -> None:
        assert (
            resolve_device(
                Device.CUDA, allow_cpu_fallback=True, cuda_available=False, mps_available=False
            )
            == "cpu"
        )

    def test_invalid_device_rejected_at_configuration(self) -> None:
        with pytest.raises(ValueError, match="device"):
            DetectorConfig(device="gpu")  # type: ignore[arg-type]

    def test_invalid_device_value_is_explicit(self) -> None:
        # Bypass the enum to reach the runtime device policy's guard.
        bogus = cast(Device, "quantum-8")
        with pytest.raises(UnsupportedDeviceError, match="unsupported device"):
            resolve_device(bogus, allow_cpu_fallback=False, cuda_available=True, mps_available=True)

    async def test_gpu_startup_failure_on_cuda_host_is_typed(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CUDA probe says available, but the model fails to load on GPU.
        monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: True)
        FakeYOLO.constructor_error = RuntimeError("cuda OOM during load")
        config = DetectorConfig(device=Device.CUDA)
        with pytest.raises(ModelLoadError) as excinfo:
            await make_adapter(config=config).load()
        assert isinstance(excinfo.value.cause, RuntimeError)

    async def test_cpu_startup_is_deterministic(self, fake_sdk: None) -> None:
        adapter = make_adapter(config=DetectorConfig(device=Device.CPU))
        await adapter.load()
        assert adapter.device == "cpu"
        assert adapter.loaded is True

    async def test_cpu_startup_failure_is_typed_and_state_clean(self, fake_sdk: None) -> None:
        # Even on the always-available CPU device, a corrupt artifact fails
        # startup explicitly — and leaves no half-loaded state behind.
        FakeYOLO.constructor_error = RuntimeError("corrupt cpu artifact")
        adapter = make_adapter(config=DetectorConfig(device=Device.CPU))
        with pytest.raises(ModelLoadError) as excinfo:
            await adapter.load()
        assert isinstance(excinfo.value.cause, RuntimeError)
        assert adapter.loaded is False
        assert adapter.device is None


# ===========================================================================
# Input failures: empty frame / invalid FramePacket / invalid dimensions
# ===========================================================================


class TestInputFailures:
    def test_none_frame_rejected_before_inference(self) -> None:
        # A missing frame is malformed input — rejected at construction,
        # never sent toward the model.
        with pytest.raises(ValueError, match="frame is required"):
            DetectionInput(frame=None, image=b"x")  # type: ignore[arg-type]

    def test_empty_frame_rejected_before_inference(self) -> None:
        with pytest.raises(ValueError, match="image"):
            DetectionInput(frame=make_frame(), image=b"")

    def test_invalid_frame_packet_rejected_by_contract(self) -> None:
        # Negative frame index is an invalid FramePacket — rejected at
        # construction, before it can ever reach a detector.
        with pytest.raises(ValueError, match="frame_index"):
            FramePacket(
                frame_id=FrameId(new_uuid()),
                session_id=VideoSessionId(new_uuid()),
                frame_index=-1,
                event_time=utc_now(),
                width=1920,
                height=1080,
            )
        with pytest.raises(ValueError, match="width"):
            FramePacket(
                frame_id=FrameId(new_uuid()),
                session_id=VideoSessionId(new_uuid()),
                frame_index=0,
                event_time=utc_now(),
                width=0,
                height=1080,
            )

    def test_invalid_frame_dimensions_rejected_before_inference(self) -> None:
        with pytest.raises(ValueError, match="width"):
            DetectionInput(frame=make_frame(), image=b"x", width=0)
        with pytest.raises(ValueError, match="height"):
            DetectionInput(frame=make_frame(), image=b"x", height=-2)

    async def test_undecodable_frame_is_typed_inference_error(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def undecodable(image: bytes) -> tuple[Any, int, int]:
            raise InferenceError("image bytes could not be decoded")

        monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", undecodable)
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="decoded") as excinfo:
            await adapter.detect(make_input())
        # Diagnostics + the failure is recorded, never silent.
        assert "decoded" in excinfo.value.message
        assert adapter.stats().total_failed_calls == 1

    async def test_decodable_blank_frame_is_empty_detection_not_error(self, fake_sdk: None) -> None:
        # A decodable frame with zero predictions is a VALID empty result,
        # not a failure.
        FakeYOLO.predict_results = []
        adapter = make_adapter()
        detections = await adapter.detect(make_input())
        assert detections == []
        assert adapter.stats().total_failed_calls == 0
        assert adapter.stats().total_detections == 0


# ===========================================================================
# Inference failures: exception / no partial output / no corrupted state
# ===========================================================================


class TestInferenceFailures:
    async def test_inference_exception_is_typed_with_cause_and_diagnostics(
        self, fake_sdk: None
    ) -> None:
        FakeYOLO.predict_error = RuntimeError("cuda sync error")
        adapter = make_adapter()
        with pytest.raises(InferenceError) as excinfo:
            await adapter.detect(make_input())
        assert isinstance(excinfo.value.cause, RuntimeError)
        assert "inference" in excinfo.value.message
        # Recorded, and the adapter stays usable (no corrupted state).
        assert adapter.stats().total_failed_calls == 1
        FakeYOLO.predict_error = None
        detections = await adapter.detect(make_input())
        assert len(detections) == 1
        assert adapter.stats().total_failed_calls == 1  # failure not erased

    async def test_out_of_range_class_index_is_typed(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 99)))]
        with pytest.raises(InferenceError, match="class index") as excinfo:
            await make_adapter().detect(make_input())
        assert "99" in excinfo.value.message

    async def test_malformed_geometry_yields_no_partial_output(self, fake_sdk: None) -> None:
        # One VALID box followed by a MALFORMED box: the whole call must
        # fail — the valid box must NEVER leak out as partial output.
        FakeYOLO.predict_results = [
            FakeResult(
                fake_boxes(
                    (10, 20, 330, 470, 0.95, 0),
                    (100, 100, 100, 100, 0.9, 0),  # zero-size -> invalid geometry
                )
            )
        ]
        with pytest.raises(InvalidGeometryError):
            await make_adapter().detect(make_input())

    async def test_out_of_range_confidence_yields_no_output(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0, 0, 10, 10, 1.7, 0)))]
        with pytest.raises(InferenceError, match="confidence"):
            await make_adapter().detect(make_input())

    async def test_success_path_provenance_is_verbatim_and_validated(self, fake_sdk: None) -> None:
        # Every emitted observation carries provenance verbatim from its
        # FramePacket and passes the provenance validator — there is no
        # fabricated-provenance path (failure paths emit nothing at all,
        # see test_malformed_geometry_yields_no_partial_output).
        frame = make_frame(frame_index=3)
        detections = await make_adapter().detect(make_input(frame=frame))
        validate_detection_provenance(frame, detections)  # must not raise
        assert detections[0].frame_id == frame.frame_id
        assert detections[0].event_time == frame.event_time
        assert detections[0].session_id == frame.session_id
        assert detections[0].frame_index == frame.frame_index

    async def test_inference_error_carries_frame_and_model_context(self, fake_sdk: None) -> None:
        # Failures carry the context that actually exists (Step 9 §13):
        # session ID, frame index, model ID, model version — never
        # fabricated identifiers, never secrets.
        FakeYOLO.predict_error = RuntimeError("cuda sync error")
        frame = make_frame(frame_index=11)
        with pytest.raises(InferenceError) as excinfo:
            await make_adapter().detect(make_input(frame=frame))
        assert str(frame.session_id) in excinfo.value.message
        assert "11" in excinfo.value.message
        assert "yolov8n" in excinfo.value.message
        assert "8.1.0" in excinfo.value.message


# ===========================================================================
# Batch failures
# ===========================================================================


class TestBatchFailures:
    async def test_batch_result_count_mismatch_is_typed(self, fake_sdk: None) -> None:
        # One result for two inputs violates the batch contract.
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 0)))]
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="batch inference returned 1 results"):
            await adapter.detect_batch([make_input(), make_input()])
        assert adapter.stats().total_failed_calls == 1
        # No corrupted state: a correct batch afterwards succeeds.
        FakeYOLO.predict_results = [
            FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 0))),
            FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 0))),
        ]
        results = await adapter.detect_batch([make_input(), make_input()])
        assert len(results) == 2

    async def test_batch_decode_failure_yields_no_partial_output(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def undecodable(image: bytes) -> tuple[Any, int, int]:
            raise InferenceError("image bytes could not be decoded")

        monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", undecodable)
        frame = make_frame(frame_index=5)
        with pytest.raises(InferenceError, match="decoded") as excinfo:
            await make_adapter().detect_batch([make_input(frame=frame), make_input()])
        # The failing frame is identifiable: the decode failure carries
        # its session ID + frame index (Step 9 §13), not just a bare error.
        assert str(frame.session_id) in excinfo.value.message
        assert "5" in excinfo.value.message

    async def test_empty_batch_is_a_valid_noop(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        assert await adapter.detect_batch([]) == []
        assert adapter.stats().total_calls == 0


# ===========================================================================
# Cancellation
# ===========================================================================


class TestCancellation:
    async def test_cancellation_propagates_and_detector_is_reusable(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        blocked = asyncio.Event()
        original_load = adapter._load

        async def blocking_load() -> None:
            # A genuine await point inside the real detect() path.
            await blocked.wait()
            await original_load()

        adapter._load = blocking_load  # type: ignore[method-assign]
        task = asyncio.create_task(adapter.detect(make_input()))
        await asyncio.sleep(0)  # let detect() reach the await in _load()
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Reusable after cancellation — no partial state left behind.
        blocked.set()
        adapter._load = original_load  # type: ignore[method-assign]
        detections = await adapter.detect(make_input())
        assert len(detections) == 1


# ===========================================================================
# Warmup failures
# ===========================================================================


class TestWarmupFailures:
    async def test_warmup_failure_is_typed_and_detector_stays_usable(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(yolo_adapter, "_blank_image", lambda config: None)
        FakeYOLO.predict_error = RuntimeError("warmup boom")
        config = DetectorConfig(warmup_frames=1)
        adapter = make_adapter(config=config)
        with pytest.raises(InferenceError) as excinfo:
            await adapter.warmup()
        assert isinstance(excinfo.value.cause, RuntimeError)
        # Warmup failure is not counted as a detection call, and the
        # adapter recovers for real inference.
        assert adapter.stats().total_calls == 0
        FakeYOLO.predict_error = None
        detections = await adapter.detect(make_input())
        assert len(detections) == 1


# ===========================================================================
# Configuration validation: confidence / NMS
# ===========================================================================


class TestConfigurationFailures:
    @pytest.mark.parametrize(
        ("field", "bad", "expected_msg"),
        [
            ("confidence_threshold", -0.01, "confidence_threshold"),
            ("confidence_threshold", 1.01, "confidence_threshold"),
            ("nms_iou_threshold", -0.1, "nms_iou_threshold"),
            ("nms_iou_threshold", 1.5, "nms_iou_threshold"),
        ],
    )
    def test_invalid_threshold_and_nms_rejected_explicitly(
        self, field: str, bad: float, expected_msg: str
    ) -> None:
        with pytest.raises(ValueError, match=expected_msg):
            DetectorConfig(**{field: bad})

    def test_invalid_max_detections_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_detections"):
            DetectorConfig(max_detections=0)

    def test_invalid_batch_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            DetectorConfig(batch_size=0)
        with pytest.raises(ValueError, match="batch_size"):
            DetectorConfig(batch_size=65)


# ===========================================================================
# Resource cleanup: no leak, close works after failures
# ===========================================================================


class TestResourceCleanup:
    async def test_close_releases_state_even_when_load_failed(self, fake_sdk: None) -> None:
        FakeYOLO.constructor_error = RuntimeError("boom")
        adapter = make_adapter()
        with pytest.raises(ModelLoadError):
            await adapter.load()
        # close() on a never-loaded adapter must be safe and idempotent.
        await adapter.close()
        await adapter.close()
        assert adapter.loaded is False
        assert adapter.device is None
        # And a fresh load cycle works — no leak of stale state.
        FakeYOLO.constructor_error = None
        await adapter.load()
        assert adapter.loaded is True

    async def test_close_after_detect_releases_model(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        await adapter.detect(make_input())
        assert adapter.loaded is True
        await adapter.close()
        assert adapter.loaded is False
        assert adapter.device is None
        assert len(FakeYOLO.instances) == 1
        # No resource leak: reload is a fresh acquisition, not stale reuse.
        await adapter.detect(make_input())
        assert len(FakeYOLO.instances) == 2

    async def test_repeated_failure_cycles_do_not_accumulate_state(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        for _ in range(3):
            FakeYOLO.predict_error = RuntimeError("boom")
            with pytest.raises(InferenceError):
                await adapter.detect(make_input())
            FakeYOLO.predict_error = None
            assert len(await adapter.detect(make_input())) == 1
        stats = adapter.stats()
        assert stats.total_calls == 6
        assert stats.total_failed_calls == 3
        assert stats.total_detections == 3

    async def test_reacquisition_after_close_reapplies_checksum_governance(
        self, fake_sdk: None, tmp_path: Path
    ) -> None:
        # A closed detector must NEVER silently reuse stale model state:
        # re-acquisition goes through the full governed load path, which
        # re-verifies the artifact checksum before the SDK is touched.
        artifact = tmp_path / "yolov8n.pt"
        payload = b"governed-model-weights"
        artifact.write_bytes(payload)
        spec = ModelSpec(
            model_id="yolov8n",
            model_name="yolov8n",
            model_version="8.1.0",
            artifact_uri=str(artifact),
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            device=Device.CPU,
            class_names=("person", "bag"),
        )
        adapter = make_adapter(spec=spec)
        await adapter.load()
        assert adapter.loaded is True
        await adapter.detect(make_input())
        # Shutdown releases the model…
        await adapter.close()
        assert adapter.loaded is False
        # …and a tampered artifact is caught on re-acquisition.
        artifact.write_bytes(b"tampered-weights")
        with pytest.raises(ModelArtifactCorruptError, match="checksum mismatch"):
            await adapter.load()
        assert adapter.loaded is False

    async def test_close_releases_cuda_memory_best_effort(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GPU memory is released on close; an empty_cache failure never
        # masks the cleanup path.
        monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: True)
        torch_module = types.ModuleType("torch")

        class _Cuda:
            empty_cache_calls = 0

            @classmethod
            def empty_cache(cls) -> None:
                cls.empty_cache_calls += 1

        torch_module.cuda = _Cuda
        monkeypatch.setitem(sys.modules, "torch", torch_module)

        config = DetectorConfig(device=Device.CUDA)
        adapter = make_adapter(config=config)
        await adapter.load()
        assert adapter.device == "cuda:0"
        await adapter.close()
        assert _Cuda.empty_cache_calls == 1
        assert adapter.loaded is False

        # A failing empty_cache (e.g. lost CUDA context) is suppressed:
        # close() must still complete and release adapter state.
        def boom() -> None:
            msg = "cuda context lost"
            raise RuntimeError(msg)

        _Cuda.empty_cache = boom
        second = make_adapter(config=config)
        await second.load()
        await second.close()  # must not raise
        assert second.loaded is False
        assert second.device is None
