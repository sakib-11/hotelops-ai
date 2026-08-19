"""Tests for YOLOv8Adapter (Task 12, Phase 4).

The adapter is tested WITHOUT the detection SDK installed: a fake
``ultralytics`` module is injected into ``sys.modules`` and the lazy
SDK seams (``_decode_image_bytes``, ``_blank_image``,
``_cuda_available``, ``_mps_available``) are monkeypatched.  This
proves the adapter is fully testable behind the boundary and that no
SDK type ever crosses it.

Covered behavior: device-selection matrix (incl. no-silent-fallback),
artifact loading + validation, translation + normalization +
provenance, confidence/NMS/max-det forwarding, empty results, decode
and inference failures, warmup, stats recording, and cleanup.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar

import pytest

from backend.app.intelligence.detectors import (
    BatchDetector,
    DetectionInput,
    DetectionStats,
    DetectorConfig,
    Device,
    FakeDetector,
    InferenceError,
    InvalidGeometryError,
    ModelLoadError,
    ModelSpec,
    ObjectDetector,
    UnsupportedDeviceError,
    yolo_adapter,
)
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter, resolve_device
from contracts.common import (
    SCHEMA_VERSION,
    FrameId,
    VideoAssetId,
    VideoSessionId,
    new_uuid,
    utc_now,
)
from contracts.video import FramePacket
from contracts.vision import DetectionObservation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_frame(*, frame_index: int = 0, width: int = 1920, height: int = 1080) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=VideoSessionId(new_uuid()),
        frame_index=frame_index,
        event_time=utc_now(),
        width=width,
        height=height,
    )


def make_spec(
    *, device: Device = Device.CPU, class_names: tuple[str, ...] = ("person", "bag")
) -> ModelSpec:
    return ModelSpec(
        model_id="yolov8n",
        model_name="yolov8n",
        model_version="8.1.0",
        artifact_uri="memory://yolov8n.pt",
        artifact_sha256="a" * 64,
        device=device,
        class_names=class_names,
    )


def make_input(
    *,
    frame: FramePacket | None = None,
    config: DetectorConfig | None = None,
    image: bytes = b"\xff\xd8fake-jpeg-bytes",
) -> DetectionInput:
    return DetectionInput(frame=frame or make_frame(), image=image, config=config)


def make_adapter(
    *,
    device: Device = Device.CPU,
    config: DetectorConfig | None = None,
    spec: ModelSpec | None = None,
) -> YOLOv8Adapter:
    """Build an adapter with a device-consistent spec + config."""
    return YOLOv8Adapter(
        model_spec=spec or make_spec(device=device),
        config=config or DetectorConfig(device=device),
    )


# ---------------------------------------------------------------------------
# Fake SDK fixture
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
    """Build boxes from (x1, y1, x2, y2, conf, cls) rows."""
    return FakeBoxes(
        [list(row[:4]) for row in rows],
        [[row[4]] for row in rows],
        [[row[5]] for row in rows],
    )


class FakeYOLO:
    instances: ClassVar[list[FakeYOLO]] = []
    predict_results: ClassVar[list[Any]] = []
    predict_error: ClassVar[Exception | None] = None
    constructor_error: ClassVar[Exception | None] = None

    def __init__(self, artifact_uri: str) -> None:
        self.artifact_uri = artifact_uri
        self.names: Any = {0: "person", 1: "bag"}
        self.predict_kwargs: list[dict[str, Any]] = []
        if FakeYOLO.constructor_error is not None:
            raise FakeYOLO.constructor_error
        FakeYOLO.instances.append(self)

    def predict(self, **kwargs: Any) -> Any:
        self.predict_kwargs.append(kwargs)
        if FakeYOLO.predict_error is not None:
            raise FakeYOLO.predict_error
        return FakeYOLO.predict_results


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake 'ultralytics' module and CPU-only device probes."""
    FakeYOLO.instances = []
    FakeYOLO.predict_results = [FakeResult(fake_boxes((10, 20, 330, 470, 0.95, 0)))]
    FakeYOLO.predict_error = None
    FakeYOLO.constructor_error = None
    module = types.ModuleType("ultralytics")
    module.YOLO = FakeYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_mps_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", lambda image: (object(), 640, 480))


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------


class TestResolveDevice:
    def test_cpu_is_explicit(self) -> None:
        assert (
            resolve_device(
                Device.CPU, allow_cpu_fallback=False, cuda_available=True, mps_available=True
            )
            == "cpu"
        )

    @pytest.mark.parametrize(
        ("cuda", "mps", "expected"),
        [
            (True, True, "cuda:0"),
            (False, True, "mps"),
            (False, False, "cpu"),
        ],
    )
    def test_auto_prefers_available(self, cuda: bool, mps: bool, expected: str) -> None:
        assert (
            resolve_device(
                Device.AUTO, allow_cpu_fallback=False, cuda_available=cuda, mps_available=mps
            )
            == expected
        )

    def test_cuda_available(self) -> None:
        assert (
            resolve_device(
                Device.CUDA, allow_cpu_fallback=False, cuda_available=True, mps_available=False
            )
            == "cuda:0"
        )

    def test_cuda_unavailable_refuses_silent_fallback(self) -> None:
        with pytest.raises(UnsupportedDeviceError):
            resolve_device(
                Device.CUDA, allow_cpu_fallback=False, cuda_available=False, mps_available=False
            )

    def test_cuda_unavailable_with_explicit_fallback(self) -> None:
        assert (
            resolve_device(
                Device.CUDA, allow_cpu_fallback=True, cuda_available=False, mps_available=False
            )
            == "cpu"
        )

    def test_mps_unavailable_refuses_silent_fallback(self) -> None:
        with pytest.raises(UnsupportedDeviceError):
            resolve_device(
                Device.MPS, allow_cpu_fallback=False, cuda_available=False, mps_available=False
            )

    def test_mps_unavailable_with_explicit_fallback(self) -> None:
        assert (
            resolve_device(
                Device.MPS, allow_cpu_fallback=True, cuda_available=False, mps_available=False
            )
            == "cpu"
        )


# ---------------------------------------------------------------------------
# Loading + model validation
# ---------------------------------------------------------------------------


class TestLoading:
    async def test_adapter_satisfies_object_detector_protocol(self) -> None:
        assert isinstance(make_adapter(), ObjectDetector)

    async def test_load_uses_configured_artifact_uri(self, fake_sdk: None) -> None:
        adapter = make_adapter(spec=make_spec(class_names=("person", "bag")))
        await adapter.load()
        assert FakeYOLO.instances[0].artifact_uri == "memory://yolov8n.pt"
        assert adapter.loaded is True
        assert adapter.device == "cpu"
        assert adapter.model_spec.model_name == "yolov8n"
        assert adapter.model_spec.model_version == "8.1.0"

    async def test_load_is_idempotent(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        await adapter.load()
        await adapter.load()
        assert len(FakeYOLO.instances) == 1

    async def test_load_without_sdk_raises_typed_error(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # sys.modules["ultralytics"] = None makes import_module raise ImportError.
        monkeypatch.setitem(sys.modules, "ultralytics", None)
        adapter = make_adapter()
        with pytest.raises(ModelLoadError):
            await adapter.load()

    async def test_artifact_constructor_failure_is_typed(self, fake_sdk: None) -> None:
        FakeYOLO.constructor_error = RuntimeError("corrupt artifact")
        adapter = make_adapter()
        with pytest.raises(ModelLoadError) as excinfo:
            await adapter.load()
        assert isinstance(excinfo.value.cause, RuntimeError)

    async def test_class_name_mismatch_rejected(self, fake_sdk: None) -> None:
        adapter = make_adapter(spec=make_spec(class_names=("dog",)))
        with pytest.raises(ModelLoadError, match="class names"):
            await adapter.load()

    async def test_empty_model_names_rejected(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class EmptyNamesYOLO(FakeYOLO):
            def __init__(self, artifact_uri: str) -> None:
                super().__init__(artifact_uri)
                self.names = {}

        module = types.ModuleType("ultralytics")
        module.YOLO = EmptyNamesYOLO
        monkeypatch.setitem(sys.modules, "ultralytics", module)
        adapter = make_adapter()
        with pytest.raises(ModelLoadError, match="class names"):
            await adapter.load()


# ---------------------------------------------------------------------------
# Inference + translation
# ---------------------------------------------------------------------------


class TestDetect:
    async def test_successful_detection_is_normalized_and_provenance_complete(
        self, fake_sdk: None
    ) -> None:
        frame = make_frame(frame_index=7)
        adapter = make_adapter()
        detections = await adapter.detect(make_input(frame=frame))
        assert len(detections) == 1
        det = detections[0]
        # Normalized coordinates relative to the decoded frame (640x480).
        assert det.bounding_box.x_min == pytest.approx(10 / 640)
        assert det.bounding_box.y_min == pytest.approx(20 / 480)
        assert det.bounding_box.x_max == pytest.approx(330 / 640)
        assert det.bounding_box.y_max == pytest.approx(470 / 480)
        assert det.confidence == pytest.approx(0.95)
        assert det.class_name == "person"
        assert det.class_id == 0
        assert det.image_width == 640
        assert det.image_height == 480
        # Provenance copied verbatim from the FramePacket.
        assert det.frame_id == frame.frame_id
        assert det.session_id == frame.session_id
        assert det.source_ref == frame.source_ref
        assert det.frame_index == frame.frame_index
        assert det.event_time == frame.event_time
        assert det.schema_version == SCHEMA_VERSION
        # Canonical metadata (model identity + artifact provenance + device).
        assert det.detector_metadata is not None
        assert det.detector_metadata["model"] == "yolov8n"
        assert det.detector_metadata["model_version"] == "8.1.0"
        assert det.detector_metadata["artifact_sha256"] == "a" * 64
        assert det.detector_metadata["device"] == "cpu"

    async def test_inference_knobs_forwarded(self, fake_sdk: None) -> None:
        config = DetectorConfig(
            confidence_threshold=0.7,
            nms_iou_threshold=0.4,
            max_detections=2,
            input_width=640,
            input_height=480,
        )
        adapter = make_adapter(config=config)
        await adapter.detect(make_input(config=config))
        kwargs = FakeYOLO.instances[0].predict_kwargs[0]
        assert kwargs["conf"] == pytest.approx(0.7)
        assert kwargs["iou"] == pytest.approx(0.4)
        assert kwargs["max_det"] == 2
        assert kwargs["imgsz"] == (480, 640)  # (height, width)
        assert kwargs["device"] == "cpu"
        assert kwargs["half"] is False
        assert kwargs["verbose"] is False

    async def test_empty_results_return_empty_list(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = []
        detections = await make_adapter().detect(make_input())
        assert detections == []

    async def test_result_without_boxes_is_skipped(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = [FakeResult(boxes=None)]
        assert await make_adapter().detect(make_input()) == []

    async def test_max_detections_caps_translation(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = [
            FakeResult(
                fake_boxes(
                    (0, 0, 10, 10, 0.9, 0),
                    (20, 20, 30, 30, 0.8, 0),
                    (40, 40, 50, 50, 0.7, 0),
                )
            )
        ]
        config = DetectorConfig(max_detections=2)
        detections = await make_adapter(config=config).detect(make_input())
        assert len(detections) == 2

    async def test_out_of_range_geometry_is_explicit_error(self, fake_sdk: None) -> None:
        # Phase 7: malformed model geometry is NEVER silently clamped.
        FakeYOLO.predict_results = [
            FakeResult(fake_boxes((-50.0, -50.0, 10000.0, 10000.0, 0.9, 0)))
        ]
        adapter = make_adapter()
        with pytest.raises(InvalidGeometryError, match=r"outside \[0, 1\]"):
            await adapter.detect(make_input())
        assert adapter.stats().total_failed_calls == 1

    async def test_out_of_range_confidence_is_explicit_error(self, fake_sdk: None) -> None:
        # Phase 7: malformed model confidence is NEVER silently clamped.
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0, 0, 10, 10, 1.5, 0)))]
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="confidence"):
            await adapter.detect(make_input())
        assert adapter.stats().total_failed_calls == 1

    async def test_zero_size_box_is_explicit_error(self, fake_sdk: None) -> None:
        # Zero-size boxes have no spatial area — rejected explicitly.
        FakeYOLO.predict_results = [FakeResult(fake_boxes((100, 100, 100, 100, 0.9, 0)))]
        with pytest.raises(InvalidGeometryError, match="zero size"):
            await make_adapter().detect(make_input())

    async def test_boundary_boxes_are_exact(self, fake_sdk: None) -> None:
        # A box exactly on the frame edges normalizes to exactly [0, 1].
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0.0, 0.0, 640.0, 480.0, 0.9, 0)))]
        det = (await make_adapter().detect(make_input()))[0]
        assert det.bounding_box.x_min == pytest.approx(0.0, abs=1e-12)
        assert det.bounding_box.y_min == pytest.approx(0.0, abs=1e-12)
        assert det.bounding_box.x_max == pytest.approx(1.0, abs=1e-12)
        assert det.bounding_box.y_max == pytest.approx(1.0, abs=1e-12)
        assert det.image_width == 640
        assert det.image_height == 480

    async def test_out_of_range_class_index_is_inference_error(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 5)))]
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="class index"):
            await adapter.detect(make_input())
        stats = adapter.stats()
        assert stats.total_failed_calls == 1

    async def test_inference_failure_is_typed_and_recorded(self, fake_sdk: None) -> None:
        FakeYOLO.predict_error = RuntimeError("boom")
        adapter = make_adapter()
        with pytest.raises(InferenceError) as excinfo:
            await adapter.detect(make_input())
        assert isinstance(excinfo.value.cause, RuntimeError)
        stats = adapter.stats()
        assert stats.total_calls == 1
        assert stats.total_failed_calls == 1
        assert stats.total_detections == 0

    async def test_decode_failure_is_inference_error(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def undecodable(image: bytes) -> tuple[Any, int, int]:
            raise InferenceError("image bytes could not be decoded")

        monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", undecodable)
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="decoded"):
            await adapter.detect(make_input())
        assert adapter.stats().total_failed_calls == 1

    async def test_stats_record_duration_device_and_yield(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        detections = await adapter.detect(make_input())
        stats: DetectionStats = adapter.stats()
        assert stats.model_name == "yolov8n"
        assert stats.model_version == "8.1.0"
        assert stats.device == "cpu"
        assert stats.total_calls == 1
        assert stats.total_detections == len(detections)
        assert stats.total_failed_calls == 0
        assert stats.last_inference_seconds is not None and stats.last_inference_seconds >= 0
        assert stats.total_inference_seconds >= stats.last_inference_seconds


# ---------------------------------------------------------------------------
# Bounded batch inference (adapter capability)
# ---------------------------------------------------------------------------


class TestDetectBatch:
    async def test_adapter_is_batch_capable(self, fake_sdk: None) -> None:
        assert isinstance(make_adapter(), BatchDetector)

    async def test_batch_returns_provenance_complete_results_per_input(
        self, fake_sdk: None
    ) -> None:
        FakeYOLO.predict_results = [
            FakeResult(fake_boxes((10, 20, 330, 470, 0.95, 0))),
            FakeResult(fake_boxes((100, 100, 200, 200, 0.8, 1))),
        ]
        frames = [make_frame(frame_index=1), make_frame(frame_index=2)]
        adapter = make_adapter()
        results = await adapter.detect_batch([make_input(frame=f) for f in frames])
        assert len(results) == 2
        assert len(results[0]) == 1
        assert len(results[1]) == 1
        # Provenance per input preserved.
        assert results[0][0].frame_id == frames[0].frame_id
        assert results[0][0].session_id == frames[0].session_id
        assert results[0][0].frame_index == frames[0].frame_index
        assert results[1][0].frame_id == frames[1].frame_id
        assert results[1][0].class_name == "bag"
        # One SDK call for the whole batch, yield recorded.
        stats = adapter.stats()
        assert stats.total_calls == 1
        assert stats.total_detections == 2

    async def test_batch_forwards_list_source(self, fake_sdk: None) -> None:
        FakeYOLO.predict_results = [
            FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 0))),
            FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 0))),
        ]
        adapter = make_adapter()
        await adapter.detect_batch([make_input(), make_input()])
        kwargs = FakeYOLO.instances[0].predict_kwargs[0]
        assert isinstance(kwargs["source"], list)
        assert len(kwargs["source"]) == 2

    async def test_empty_batch_is_noop(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        assert await adapter.detect_batch([]) == []
        assert adapter.stats().total_calls == 0

    async def test_result_count_mismatch_is_inference_error(self, fake_sdk: None) -> None:
        # One result for two inputs — the batch contract is violated.
        FakeYOLO.predict_results = [FakeResult(fake_boxes((0, 0, 10, 10, 0.9, 0)))]
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="batch inference returned 1 results"):
            await adapter.detect_batch([make_input(), make_input()])
        assert adapter.stats().total_failed_calls == 1

    async def test_batch_decode_failure_is_typed(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def undecodable(image: bytes) -> tuple[Any, int, int]:
            raise InferenceError("image bytes could not be decoded")

        monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", undecodable)
        adapter = make_adapter()
        with pytest.raises(InferenceError, match="decoded"):
            await adapter.detect_batch([make_input(), make_input()])
        assert adapter.stats().total_failed_calls == 1

    async def test_mixed_session_source_batch_has_no_cross_frame_leakage(
        self, fake_sdk: None
    ) -> None:
        """Three frames from DIFFERENT sessions, sources and timestamps.

        The deterministic one-to-one mapping must hold: every result
        carries ITS OWN frame's provenance — never a sibling's.
        """
        base_time = utc_now()
        frames = [
            FramePacket(
                frame_id=FrameId(new_uuid()),
                session_id=VideoSessionId(new_uuid()),
                source_ref=VideoAssetId(new_uuid()),
                frame_index=index,
                event_time=base_time + timedelta(seconds=index),
            )
            for index in range(3)
        ]
        FakeYOLO.predict_results = [
            FakeResult(fake_boxes((10, 20, 330, 470, 0.95, 0))) for _ in range(3)
        ]
        adapter = make_adapter()
        results = await adapter.detect_batch([make_input(frame=f) for f in frames])
        assert len(results) == 3
        for detections, frame in zip(results, frames, strict=True):
            assert len(detections) == 1
            det = detections[0]
            assert det.frame_id == frame.frame_id
            assert det.session_id == frame.session_id
            assert det.source_ref == frame.source_ref
            assert det.frame_index == frame.frame_index
            assert det.event_time == frame.event_time
        # Deterministic 1:1 mapping — no cross-frame leakage at all.
        assert [r[0].frame_id for r in results] == [f.frame_id for f in frames]
        assert len({r[0].session_id for r in results}) == 3
        assert len({r[0].source_ref for r in results}) == 3
        assert len({r[0].event_time for r in results}) == 3


# ---------------------------------------------------------------------------
# No silent GPU -> CPU fallback
# ---------------------------------------------------------------------------


class TestDeviceSelection:
    async def test_requested_cuda_unavailable_fails_startup(self, fake_sdk: None) -> None:
        adapter = make_adapter(device=Device.CUDA)
        with pytest.raises(UnsupportedDeviceError):
            await adapter.load()

    async def test_requested_cuda_with_explicit_fallback_loads_cpu(self, fake_sdk: None) -> None:
        config = DetectorConfig(device=Device.CUDA, allow_cpu_fallback=True)
        adapter = make_adapter(config=config)
        await adapter.load()
        assert adapter.device == "cpu"

    async def test_requested_mps_unavailable_fails_startup(self, fake_sdk: None) -> None:
        adapter = make_adapter(device=Device.MPS)
        with pytest.raises(UnsupportedDeviceError):
            await adapter.load()

    async def test_auto_on_cpu_only_host_resolves_cpu(self, fake_sdk: None) -> None:
        adapter = make_adapter(device=Device.AUTO)
        await adapter.load()
        assert adapter.device == "cpu"

    async def test_auto_on_cuda_host_resolves_cuda(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: True)
        adapter = make_adapter(device=Device.AUTO)
        await adapter.load()
        assert adapter.device == "cuda:0"
        # Device is recorded in the emitted metadata too.
        det = (await adapter.detect(make_input()))[0]
        assert det.detector_metadata is not None
        assert det.detector_metadata["device"] == "cuda:0"


# ---------------------------------------------------------------------------
# Warmup + cleanup
# ---------------------------------------------------------------------------


class TestWarmupAndCleanup:
    async def test_warmup_runs_configured_passes_and_is_excluded_from_stats(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(yolo_adapter, "_blank_image", lambda config: None)
        config = DetectorConfig(warmup_frames=3)
        adapter = make_adapter(config=config)
        await adapter.warmup()
        assert len(FakeYOLO.instances[0].predict_kwargs) == 3
        assert adapter.stats().total_calls == 0  # warmup not counted

    async def test_warmup_zero_frames_still_loads(self, fake_sdk: None) -> None:
        adapter = make_adapter()  # warmup_frames=0 by default
        await adapter.warmup()
        assert adapter.loaded is True
        assert len(FakeYOLO.instances[0].predict_kwargs) == 0

    async def test_warmup_failure_is_typed(
        self, fake_sdk: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(yolo_adapter, "_blank_image", lambda config: None)
        FakeYOLO.predict_error = RuntimeError("warmup boom")
        config = DetectorConfig(warmup_frames=1)
        adapter = make_adapter(config=config)
        with pytest.raises(InferenceError):
            await adapter.warmup()

    async def test_close_releases_and_next_use_reloads(self, fake_sdk: None) -> None:
        adapter = make_adapter()
        await adapter.detect(make_input())
        assert len(FakeYOLO.instances) == 1
        await adapter.close()
        assert adapter.loaded is False
        assert adapter.device is None
        # Idempotent.
        await adapter.close()
        # Next detect re-acquires the model.
        await adapter.detect(make_input())
        assert len(FakeYOLO.instances) == 2


# ---------------------------------------------------------------------------
# Port conformance of the concrete adapter
# ---------------------------------------------------------------------------


class TestAdapterConformance:
    async def test_adapter_is_interchangeable_with_fake_detector(self, fake_sdk: None) -> None:
        """YOLOv8Adapter and FakeDetector both satisfy the same port."""
        fake = FakeDetector(model_spec=make_spec())
        yolo = make_adapter()

        async def run(detector: ObjectDetector, inp: DetectionInput) -> list[DetectionObservation]:
            await detector.warmup()
            return await detector.detect(inp)

        for detector in (fake, yolo):
            result = await run(detector, make_input())
            assert all(isinstance(d, DetectionObservation) for d in result)
            assert all(d.class_name in ("person", "bag") for d in result)
