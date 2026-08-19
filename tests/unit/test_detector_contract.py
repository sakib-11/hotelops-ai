"""Tests for the generic ObjectDetector abstraction (Task 12, Phase 3).

Covers the explicit contract behaviors:

- successful detection (normalized boxes, provenance, metadata, round-trip)
- empty detection
- invalid input (rejected before inference)
- inference failure (typed, non-fatal-at-frame semantics)
- cancellation (propagates; detector stays reusable)
- provenance + metadata helpers
- protocol substitutability (mock/test detector swap without downstream change)
- SDK isolation guard (no vendor imports/mentions inside the boundary)
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.app.intelligence.detectors import (
    DEFAULT_DETECTOR_CONFIG,
    DetectionError,
    DetectionInput,
    DetectorConfig,
    Device,
    FakeDetector,
    InferenceError,
    ModelSpec,
    ObjectDetector,
    detection_metadata,
    validate_detection_provenance,
)
from contracts.common import (
    SCHEMA_VERSION,
    DetectionId,
    FrameId,
    VideoSessionId,
    new_uuid,
    utc_now,
)
from contracts.video import FramePacket
from contracts.vision import BoundingBox, DetectionObservation

# The full production surface the SDK must never leak into: application
# code (domain, services, repositories, API, infrastructure, workers)
# plus the canonical cross-module contracts.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = (PROJECT_ROOT / "backend" / "app", PROJECT_ROOT / "contracts")


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def make_spec(*, device: Device = Device.CPU) -> ModelSpec:
    return ModelSpec(
        model_id="fake-detector",
        model_name="fake-detector",
        model_version="1.0.0",
        artifact_uri="memory://fake-detector",
        artifact_sha256="0" * 64,
        device=device,
        class_names=("person", "bag"),
    )


def make_input(
    *,
    frame: FramePacket | None = None,
    config: DetectorConfig | None = None,
    image: bytes = b"fake-frame-bytes",
    width: int = 1920,
    height: int = 1080,
) -> DetectionInput:
    return DetectionInput(
        frame=frame or make_frame(),
        image=image,
        width=width,
        height=height,
        config=config,
    )


# ---------------------------------------------------------------------------
# Input / configuration / identity validation
# ---------------------------------------------------------------------------


class TestDetectorConfigValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("confidence_threshold", 1.5),
            ("confidence_threshold", -0.1),
            ("nms_iou_threshold", 1.1),
            ("max_detections", 0),
            ("input_width", 0),
            ("input_height", -4),
            ("warmup_frames", -1),
            ("batch_size", 0),
            ("batch_size", 65),
            ("device", "gpu"),
        ],
    )
    def test_invalid_values_rejected(self, field: str, value: Any) -> None:
        with pytest.raises(ValueError):
            DetectorConfig(**{field: value})

    def test_defaults_are_sane(self) -> None:
        config = DEFAULT_DETECTOR_CONFIG
        assert config.confidence_threshold == pytest.approx(0.5)
        assert config.nms_iou_threshold == pytest.approx(0.45)
        assert config.max_detections == 300
        assert config.warmup_frames == 0
        assert config.half_precision is False
        assert config.batch_size == 1  # batching disabled by default
        assert config.allow_cpu_fallback is False


class TestModelSpecValidation:
    def test_empty_model_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="model_id"):
            ModelSpec(
                model_id="  ",
                model_name="m",
                model_version="1.0.0",
                artifact_uri="memory://x",
                artifact_sha256="0" * 64,
                device=Device.CPU,
                class_names=("person",),
            )

    def test_empty_model_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="model_name"):
            ModelSpec(
                model_id="m",
                model_name="  ",
                model_version="1.0.0",
                artifact_uri="memory://x",
                artifact_sha256="0" * 64,
                device=Device.CPU,
                class_names=("person",),
            )

    def test_empty_class_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="class_names"):
            ModelSpec(
                model_id="m",
                model_name="m",
                model_version="1.0.0",
                artifact_uri="memory://x",
                artifact_sha256="0" * 64,
                device=Device.CPU,
                class_names=(),
            )

    def test_duplicate_class_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            ModelSpec(
                model_id="m",
                model_name="m",
                model_version="1.0.0",
                artifact_uri="memory://x",
                artifact_sha256="0" * 64,
                device=Device.CPU,
                class_names=("person", "person"),
            )

    @pytest.mark.parametrize("digest", ["abc", "0" * 63, "g" + "0" * 63, "0" * 64 + "z"])
    def test_invalid_sha256_rejected(self, digest: str) -> None:
        with pytest.raises(ValueError, match="sha256"):
            ModelSpec(
                model_id="m",
                model_name="m",
                model_version="1.0.0",
                artifact_uri="memory://x",
                artifact_sha256=digest,
                device=Device.CPU,
                class_names=("person",),
            )

    def test_valid_spec_accepted(self) -> None:
        spec = make_spec()
        assert spec.device == Device.CPU
        assert spec.artifact_sha256 == "0" * 64


class TestDetectionInputValidation:
    def test_empty_image_rejected(self) -> None:
        with pytest.raises(ValueError, match="image"):
            DetectionInput(frame=make_frame(), image=b"")

    def test_invalid_dimensions_rejected(self) -> None:
        with pytest.raises(ValueError, match="width"):
            DetectionInput(frame=make_frame(), image=b"x", width=0)
        with pytest.raises(ValueError, match="height"):
            DetectionInput(frame=make_frame(), image=b"x", height=-1)

    def test_input_consumes_canonical_frame_packet(self) -> None:
        frame = make_frame()
        inp = make_input(frame=frame)
        assert inp.frame is frame
        assert isinstance(inp.frame, FramePacket)


# ---------------------------------------------------------------------------
# Protocol boundary
# ---------------------------------------------------------------------------


class TestObjectDetectorProtocol:
    def test_fake_detector_satisfies_protocol(self) -> None:
        assert isinstance(FakeDetector(model_spec=make_spec()), ObjectDetector)

    def test_unrelated_object_is_not_a_detector(self) -> None:
        assert not isinstance("not a detector", ObjectDetector)
        assert not isinstance(42, ObjectDetector)

    async def test_detector_swappable_without_downstream_change(self) -> None:
        """A structurally-conforming test detector replaces FakeDetector."""

        class TestDetector:
            def __init__(self) -> None:
                self._spec = make_spec()

            @property
            def model_spec(self) -> ModelSpec:
                return self._spec

            async def warmup(self) -> None:
                return None

            async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
                return []

        async def run_detection(
            detector: ObjectDetector, inp: DetectionInput
        ) -> list[DetectionObservation]:
            await detector.warmup()
            return await detector.detect(inp)

        assert isinstance(TestDetector(), ObjectDetector)
        inp = make_input()
        for detector in (FakeDetector(model_spec=make_spec()), TestDetector()):
            result = await run_detection(detector, inp)
            assert isinstance(result, list)
            assert all(isinstance(d, DetectionObservation) for d in result)


# ---------------------------------------------------------------------------
# Explicit behaviors
# ---------------------------------------------------------------------------


class TestSuccessfulDetection:
    async def test_emits_expected_detections_with_provenance(self) -> None:
        frame = make_frame(frame_index=3)
        detector = FakeDetector(model_spec=make_spec(), detections_per_frame=2)
        detections = await detector.detect(make_input(frame=frame))

        assert len(detections) == 2
        for det in detections:
            # Provenance: frame/session/source identity + event time copied
            # verbatim from the consumed FramePacket.
            assert det.frame_id == frame.frame_id
            assert det.session_id == frame.session_id
            assert det.source_ref == frame.source_ref
            assert det.frame_index == frame.frame_index
            assert det.event_time == frame.event_time
            assert det.schema_version == SCHEMA_VERSION
            # Normalized coordinates inside [0, 1] with valid ordering.
            box = det.bounding_box
            assert 0.0 <= box.x_min < box.x_max <= 1.0
            assert 0.0 <= box.y_min < box.y_max <= 1.0
            assert 0.0 <= det.confidence <= 1.0
            assert det.class_name in ("person", "bag")
            assert det.detection_id is not None

    async def test_output_round_trips_the_task4_contract(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), detections_per_frame=1)
        det = (await detector.detect(make_input()))[0]
        restored = DetectionObservation.model_validate(det.model_dump())
        assert restored == det

    async def test_deterministic_boxes_for_same_frame(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), detections_per_frame=2)
        first = await detector.detect(make_input(frame=make_frame(frame_index=7)))
        second = await detector.detect(make_input(frame=make_frame(frame_index=7)))
        assert [d.bounding_box for d in first] == [d.bounding_box for d in second]


class TestEmptyDetection:
    async def test_no_detections_returns_empty_list(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), detections_per_frame=0)
        assert await detector.detect(make_input()) == []

    async def test_below_confidence_threshold_returns_empty_list(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), confidence=0.3)
        config = DetectorConfig(confidence_threshold=0.5)
        assert await detector.detect(make_input(config=config)) == []

    async def test_max_detections_caps_emissions(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), detections_per_frame=5)
        config = DetectorConfig(max_detections=2)
        assert len(await detector.detect(make_input(config=config))) == 2


class TestInferenceFailure:
    async def test_typed_failure_after_configured_calls(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), fail_after_calls=2)
        assert await detector.detect(make_input())
        assert await detector.detect(make_input())
        with pytest.raises(InferenceError):
            await detector.detect(make_input())

    async def test_inference_error_is_a_detection_error(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), fail_after_calls=0)
        with pytest.raises(InferenceError) as excinfo:
            await detector.detect(make_input())
        assert isinstance(excinfo.value, DetectionError)
        assert isinstance(excinfo.value, Exception)

    async def test_detector_remains_usable_after_failure(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), fail_after_calls=1)
        await detector.detect(make_input())
        with pytest.raises(InferenceError):
            await detector.detect(make_input())
        # A fresh configuration error-free run keeps working: the fake's
        # failure policy is per-call, not a poisoned state.
        assert detector.calls == 2


class TestCancellation:
    async def test_cancelled_detect_propagates_and_detector_is_reusable(self) -> None:
        release = asyncio.Event()

        class BlockingDetector:
            """A structural ObjectDetector that blocks until released."""

            def __init__(self) -> None:
                self._spec = make_spec()

            @property
            def model_spec(self) -> ModelSpec:
                return self._spec

            async def warmup(self) -> None:
                return None

            async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
                await release.wait()
                return []

        detector = BlockingDetector()
        task = asyncio.create_task(detector.detect(make_input()))
        await asyncio.sleep(0)  # let detect() reach the await point
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Reusable after cancellation — no partial state left behind.
        release.set()
        assert await detector.detect(make_input()) == []


class TestWarmup:
    async def test_warmup_is_idempotent_and_not_counted(self) -> None:
        detector = FakeDetector(model_spec=make_spec())
        await detector.warmup()
        await detector.warmup()
        assert detector.calls == 0
        await detector.detect(make_input())
        assert detector.calls == 1


class TestProvenanceAndMetadata:
    async def test_metadata_follows_canonical_schema(self) -> None:
        spec = make_spec(device=Device.MPS)
        metadata = detection_metadata(spec, input_width=640, input_height=480)
        assert metadata == {
            "model_id": "fake-detector",
            "model": "fake-detector",
            "model_version": "1.0.0",
            "artifact_sha256": "0" * 64,
            "device": "mps",
            "input_width": 640,
            "input_height": 480,
        }

    async def test_detections_carry_canonical_metadata(self) -> None:
        detector = FakeDetector(model_spec=make_spec(), detections_per_frame=1)
        det = (await detector.detect(make_input()))[0]
        assert det.detector_metadata is not None
        assert det.detector_metadata["model"] == "fake-detector"
        assert det.detector_metadata["model_version"] == "1.0.0"
        assert set(det.detector_metadata) == {
            "model_id",
            "model",
            "model_version",
            "artifact_sha256",
            "device",
            "input_width",
            "input_height",
        }

    def test_provenance_validation_passes_for_matching_output(self) -> None:
        frame = make_frame()
        spec = make_spec()
        metadata = detection_metadata(spec, input_width=640, input_height=640)
        detections = [
            DetectionObservation(
                detection_id=DetectionId(new_uuid()),
                frame_id=frame.frame_id,
                session_id=frame.session_id,
                source_ref=frame.source_ref,
                frame_index=frame.frame_index,
                class_name="person",
                confidence=0.9,
                bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
                event_time=frame.event_time,
                detector_metadata=metadata,
            )
        ]
        validate_detection_provenance(frame, detections)  # must not raise

    def test_provenance_validation_rejects_wrong_session(self) -> None:
        frame = make_frame()
        spec = make_spec()
        detections = [
            DetectionObservation(
                detection_id=DetectionId(new_uuid()),
                frame_id=frame.frame_id,
                session_id=VideoSessionId(new_uuid()),  # different session
                class_name="person",
                confidence=0.9,
                bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
                event_time=frame.event_time,
                detector_metadata=detection_metadata(spec, input_width=640, input_height=640),
            )
        ]
        with pytest.raises(ValueError, match="session_id"):
            validate_detection_provenance(frame, detections)

    def test_provenance_validation_rejects_wrong_frame_index(self) -> None:
        frame = make_frame(frame_index=4)
        spec = make_spec()
        detections = [
            DetectionObservation(
                detection_id=DetectionId(new_uuid()),
                frame_id=frame.frame_id,
                frame_index=99,  # fabricated index
                class_name="person",
                confidence=0.9,
                bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
                event_time=frame.event_time,
                detector_metadata=detection_metadata(spec, input_width=640, input_height=640),
            )
        ]
        with pytest.raises(ValueError, match="frame_index"):
            validate_detection_provenance(frame, detections)

    def test_provenance_validation_rejects_wrong_frame(self) -> None:
        frame = make_frame()
        spec = make_spec()
        detections = [
            DetectionObservation(
                detection_id=DetectionId(new_uuid()),
                frame_id=FrameId(new_uuid()),  # different frame
                class_name="person",
                confidence=0.9,
                bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
                event_time=frame.event_time,
                detector_metadata=detection_metadata(spec, input_width=640, input_height=640),
            )
        ]
        with pytest.raises(ValueError, match="frame_id"):
            validate_detection_provenance(frame, detections)

    def test_provenance_validation_rejects_wrong_event_time(self) -> None:
        frame = make_frame()
        spec = make_spec()
        detections = [
            DetectionObservation(
                detection_id=DetectionId(new_uuid()),
                frame_id=frame.frame_id,
                class_name="person",
                confidence=0.9,
                bounding_box=BoundingBox(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5),
                event_time=utc_now(),  # fabricated time — must be rejected
                detector_metadata=detection_metadata(spec, input_width=640, input_height=640),
            )
        ]
        with pytest.raises(ValueError, match="event_time"):
            validate_detection_provenance(frame, detections)


# ---------------------------------------------------------------------------
# SDK isolation guard
# ---------------------------------------------------------------------------


def _production_python_files() -> list[Path]:
    """All production Python files (backend/app + contracts)."""
    files: list[Path] = []
    for root in PRODUCTION_DIRS:
        files.extend(sorted(root.rglob("*.py")))
    return sorted(files)


def _rel(path: Path) -> str:
    """Repo-relative path for readable diagnostics."""
    return str(path.relative_to(PROJECT_ROOT))


class TestSdkIsolation:
    """Repo-wide guard: the detection SDK never leaks into production code.

    Phase 10 architectural isolation audit.  The SDK name
    ("ultralytics") and SDK call patterns (``model.predict``) may exist
    ONLY inside the designated adapter file; vendor SDK imports and
    tracking calls are forbidden EVERYWHERE in production — domain
    models, business services, repositories, analytics, evidence,
    alerts, recommendations, API contracts, and generic CV
    orchestration.
    """

    def test_vendor_sdk_mentions_only_in_adapter_repo_wide(self) -> None:
        """The SDK name appears only where governed identity needs it.

        Since Task 12 Step 5, "ultralytics" is additionally a governed
        DATA VALUE: the runtime identifier in the model registry
        (``ModelDefinition.runtime``) and its typed configuration
        (``Settings.detection_runtime``).  The SDK is still an
        implementation detail — the string is never an import or a
        call outside the adapter, and the import-level guard below
        (``test_no_vendor_sdk_imports_in_production``) remains strict.
        """
        forbidden = ("ultralytics",)
        allowed_data_value_files = {
            "backend/app/intelligence/detectors/registry.py",
            "backend/app/infrastructure/config.py",
        }
        offenders = {
            _rel(path)
            for path in _production_python_files()
            if any(term in path.read_text(encoding="utf-8").lower() for term in forbidden)
        }
        assert offenders == {
            "backend/app/intelligence/detectors/yolo_adapter.py",
            *allowed_data_value_files,
        }, f"SDK references leaked outside the adapter: {sorted(offenders)}"

    def test_no_vendor_sdk_imports_in_production(self) -> None:
        """No inference/tracking-framework import exists anywhere in production."""
        vendor_modules = ("ultralytics", "torch", "cv2", "opencv", "onnxruntime", "bytetrack")
        for path in _production_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        assert root not in vendor_modules, (
                            f"{_rel(path)} imports vendor SDK '{root}'"
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    assert root not in vendor_modules, f"{_rel(path)} imports vendor SDK '{root}'"

    def test_no_tracking_calls_in_production(self) -> None:
        """Tracking (``model.track``) is not implemented and must not leak.

        The tracker boundary is a separate future concern; a tracking
        call appearing in production would couple business code to the
        SDK before a tracker abstraction exists.  ``model.track`` matches
        every realistic SDK call site (``self.model.track(``,
        ``detector.model.track(``) without the broad ``.track(`` pattern
        that would false-positive on unrelated identifiers.
        """
        forbidden = ("model.track",)
        offenders = {
            _rel(path)
            for path in _production_python_files()
            if any(term in path.read_text(encoding="utf-8") for term in forbidden)
        }
        assert not offenders, f"tracking calls leaked into production: {sorted(offenders)}"

    def test_predict_calls_only_in_adapter(self) -> None:
        """``model.predict`` exists only inside the designated adapter."""
        forbidden = ("model.predict",)
        offenders = {
            _rel(path)
            for path in _production_python_files()
            if any(term in path.read_text(encoding="utf-8") for term in forbidden)
        }
        assert offenders == {"backend/app/intelligence/detectors/yolo_adapter.py"}, (
            f"predict calls leaked outside the adapter: {sorted(offenders)}"
        )
