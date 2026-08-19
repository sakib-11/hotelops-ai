"""Task 18.4 — YOLO vertical slice.

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 12
detection boundary:

    FramePacket → ObjectDetector → YOLOv8Adapter → normalized DetectionObservation

The adapter runs behind its lazy-SDK seam (the only place the application
touches Ultralytics); these tests inject the deterministic fake SDK exactly
as the detection golden regression does — the fixture's golden predictions
become the SDK output, the confidence threshold filter is applied by the
fake (matching the real SDK), and image dimensions are read from the real
PNG header.  Business logic never imports Ultralytics: the test imports
only the adapter + canonical contracts.

Verified here:
- valid frame   → exactly one ``person`` detection, geometry matching the
                  golden box normalized to [0, 1];
- empty frame   → no detections (a valid result, not an error);
- model startup → load/warmup, device resolution, checksum-before-load;
- corrupt model → ModelArtifactCorruptError (fail-fast, before inference);
- deterministic detection contract + provenance (frame/session/source/
  event-time copied verbatim, canonical detector_metadata);
- inference metrics (DetectionStats).

The golden manifest carries the model + config the slice runs under; the
test asserts the object CLASS and reasonable geometry — it never tunes a
rule around one accidental detection.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from backend.app.intelligence.detectors import (
    DetectionInput,
    DetectorConfig,
    Device,
    ModelSpec,
    yolo_adapter,
)
from backend.app.intelligence.detectors.base import validate_detection_provenance
from backend.app.intelligence.detectors.exceptions import ModelArtifactCorruptError
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from contracts.common import SCHEMA_VERSION, FrameId, VideoAssetId, VideoSessionId, new_uuid
from contracts.video import FramePacket
from contracts.vision import DetectionObservation

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = FIXTURES_DIR / "manifest.json"


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _frame_packet(
    *,
    frame_index: int,
    event_time: datetime,
    session_id: VideoSessionId,
    source_ref: VideoAssetId,
) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=session_id,
        frame_index=frame_index,
        event_time=event_time,
        width=320,
        height=240,
        source_ref=source_ref,
    )


def _frame_image(frame_index: int) -> bytes:
    return (FIXTURES_DIR / f"frame_{frame_index:03d}.png").read_bytes()


def _png_dimensions(data: bytes) -> tuple[int, int]:
    import struct

    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def make_spec(manifest: dict) -> ModelSpec:
    model = manifest["model"]
    return ModelSpec(
        model_id=model["id"],
        model_name=model["name"],
        model_version=model["version"],
        artifact_uri=model["artifact_uri"],
        artifact_sha256=model["artifact_sha256"],
        device=Device.CPU,
        class_names=tuple(model["class_names"]),
    )


def make_config(manifest: dict) -> DetectorConfig:
    config = manifest["config"]
    return DetectorConfig(
        confidence_threshold=config["confidence_threshold"],
        nms_iou_threshold=config["nms_iou_threshold"],
        max_detections=config["max_detections"],
        input_width=320,
        input_height=240,
        device=Device.CPU,
    )


# ---------------------------------------------------------------------------
# Fake SDK (the deterministic seam behind the adapter — same pattern as the
# detection golden regression).  The fixture's golden predictions per frame
# are served as SDK output with the real SDK's conf filter.
# ---------------------------------------------------------------------------


class _FakeBoxes:
    def __init__(
        self, xyxy: list[list[float]], conf: list[list[float]], cls: list[list[int]]
    ) -> None:
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls

    def __len__(self) -> int:
        return len(self.xyxy)


@dataclass
class _FakeResult:
    boxes: _FakeBoxes | None = None


def _fake_boxes(*rows: tuple[float, float, float, float, float, int]) -> _FakeBoxes:
    return _FakeBoxes(
        [list(row[:4]) for row in rows],
        [[row[4]] for row in rows],
        [[row[5]] for row in rows],
    )


class _FakeYOLO:
    """Deterministic stand-in for ``ultralytics.YOLO`` serving the fixture's
    golden predictions with the SDK's confidence-threshold filter."""

    instances: ClassVar[list[_FakeYOLO]] = []

    def __init__(self, artifact_uri: str, names: dict[int, str]) -> None:
        self.artifact_uri = artifact_uri
        self.names: dict[int, str] = names
        _FakeYOLO.instances.append(self)

    def predict(self, **kwargs: Any) -> Any:
        source = kwargs.get("source")
        payloads = kwargs["_fixture_predictions_per_input"]
        conf_threshold = kwargs["conf"]
        if isinstance(source, list):
            return [_FakeResult(_fake_boxes(*_passed(p, conf_threshold))) for p in payloads]
        return [_FakeResult(_fake_boxes(*_passed(payloads[0], conf_threshold)))]


def _passed(
    predictions: list[dict], conf_threshold: float
) -> tuple[tuple[float, float, float, float, float, int], ...]:
    kept = [p for p in predictions if p["confidence"] >= conf_threshold]
    return tuple((p["x1"], p["y1"], p["x2"], p["y2"], p["confidence"], p["class_id"]) for p in kept)


def install_fake_sdk(monkeypatch: pytest.MonkeyPatch, manifest: dict) -> None:
    """Inject the fixture-driven fake SDK + PNG-header decode seam."""
    _FakeYOLO.instances = []
    names = {i: name for i, name in enumerate(manifest["model"]["class_names"])}
    module = types.ModuleType("ultralytics")

    def make_predictions(frame_index: int) -> list[dict]:
        entry = manifest["timeline"][frame_index]
        return entry["golden_detections"]

    class _YOLO(_FakeYOLO):
        def __init__(self, artifact_uri: str) -> None:
            super().__init__(artifact_uri, names)

        def predict(self, **kwargs: Any) -> Any:
            source = kwargs.get("source")
            if isinstance(source, list):
                kwargs["_fixture_predictions_per_input"] = [
                    make_predictions(i) for i in range(len(source))
                ]
            else:
                # Single frame: the adapter passes only the decoded image.
                # The decode seam stashes the frame index on the decoded
                # object, so the fake serves that frame's golden predictions.
                frame_index = getattr(source, "_fixture_frame_index", None)
                if frame_index is None:
                    raise AssertionError("single-image predict without a fixture frame index")
                kwargs["_fixture_predictions_per_input"] = [make_predictions(frame_index)]
            return super().predict(**kwargs)

    module.YOLO = _YOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_mps_available", lambda: False)

    def decode(image: bytes) -> tuple[Any, int, int]:
        width, height = _png_dimensions(image)
        # Identify the fixture frame from its exact PNG bytes (deterministic
        # lookup — every frame is a distinct committed byte string).
        frame_index = next(
            i
            for i in range(manifest["metadata"]["frame_count"])
            if (FIXTURES_DIR / f"frame_{i:03d}.png").read_bytes() == image
        )
        marker = types.SimpleNamespace(_fixture_frame_index=frame_index)
        return marker, width, height

    monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", decode)


def golden_detection(manifest: dict, frame_index: int) -> dict | None:
    detections = manifest["timeline"][frame_index]["golden_detections"]
    return detections[0] if detections else None


# ---------------------------------------------------------------------------
# Slice: fixture frames → YOLOv8Adapter → normalized DetectionObservation
# ---------------------------------------------------------------------------


class TestVerticalSliceDetection:
    @pytest.fixture(autouse=True)
    def _sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fake_sdk(monkeypatch, _load_manifest())

    def _adapter(self) -> YOLOv8Adapter:
        manifest = _load_manifest()
        return YOLOv8Adapter(model_spec=make_spec(manifest), config=make_config(manifest))

    async def _detect(
        self, adapter: YOLOv8Adapter, frame_index: int
    ) -> tuple[FramePacket, list[DetectionObservation]]:
        manifest = _load_manifest()
        meta = manifest["metadata"]
        capture = datetime.fromisoformat(meta["capture_time"])
        packet = _frame_packet(
            frame_index=frame_index,
            event_time=capture + timedelta(seconds=frame_index / meta["fps"]),
            session_id=VideoSessionId(new_uuid()),
            source_ref=VideoAssetId(new_uuid()),
        )
        detections = await adapter.detect(
            DetectionInput(frame=packet, image=_frame_image(frame_index))
        )
        return packet, detections

    async def test_valid_frame_expected_class_and_reasonable_geometry(
        self,
    ) -> None:
        """A frame with the person present yields exactly one ``person`` with
        the golden box normalized to [0, 1] (within float tolerance)."""
        manifest = _load_manifest()
        adapter = self._adapter()
        frame_index = 15  # person inside ROI
        _packet, detections = await self._detect(adapter, frame_index)

        assert len(detections) == 1
        det = detections[0]
        # The expected object class from the controlled fixture.
        assert det.class_name == "person"
        assert det.class_id == 0
        golden = golden_detection(manifest, frame_index)
        assert golden is not None
        # Golden geometry is pixel coordinates; the adapter normalizes to
        # [0, 1] relative to the frame dimensions.
        width = manifest["metadata"]["width"]
        height = manifest["metadata"]["height"]
        assert det.bounding_box.x_min == pytest.approx(golden["x1"] / width, abs=1e-6)
        assert det.bounding_box.y_min == pytest.approx(golden["y1"] / height, abs=1e-6)
        assert det.bounding_box.x_max == pytest.approx(golden["x2"] / width, abs=1e-6)
        assert det.bounding_box.y_max == pytest.approx(golden["y2"] / height, abs=1e-6)
        # Confidence from the fixture, bounded [0, 1].
        assert det.confidence == pytest.approx(golden["confidence"], abs=1e-6)
        # Reasonable geometry: box strictly inside the frame, non-degenerate.
        assert 0.0 <= det.bounding_box.x_min < det.bounding_box.x_max <= 1.0
        assert 0.0 <= det.bounding_box.y_min < det.bounding_box.y_max <= 1.0
        assert det.image_width == width
        assert det.image_height == height

    async def test_empty_frame_yields_no_detections(self) -> None:
        """An empty scene is a valid, successful empty result — not an error."""
        adapter = self._adapter()
        packet, detections = await self._detect(adapter, frame_index=0)
        assert detections == []
        assert packet.frame_index == 0
        # Metrics recorded the successful (empty) call.
        stats = adapter.stats()
        assert stats.total_calls == 1
        assert stats.total_failed_calls == 0
        assert stats.total_detections == 0

    async def test_model_startup_load_warmup_device(self) -> None:
        """load()/warmup() prime the adapter; device resolves explicitly to CPU."""
        manifest = _load_manifest()
        adapter = self._adapter()
        assert adapter.loaded is False
        await adapter.load()
        assert adapter.loaded is True
        assert adapter.device == "cpu"
        # warmup is idempotent-safe and excluded from inference metrics.
        await adapter.warmup()
        stats = adapter.stats()
        assert stats.total_calls == 0
        assert stats.model_name == manifest["model"]["name"]
        assert stats.model_version == manifest["model"]["version"]
        assert stats.device == "cpu"

    async def test_corrupt_model_fails_before_load(self, tmp_path: Path) -> None:
        """A checksum mismatch raises ModelArtifactCorruptError fail-fast."""
        manifest = _load_manifest()
        artifact = tmp_path / "model.pt"
        artifact.write_bytes(b"corrupt-artifact-bytes")
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        # Declared digest disagrees with the artifact on disk.
        wrong = "0" * 64 if actual != "0" * 64 else "1" * 64
        spec = ModelSpec(
            model_id=manifest["model"]["id"],
            model_name=manifest["model"]["name"],
            model_version=manifest["model"]["version"],
            artifact_uri=str(artifact),
            artifact_sha256=wrong,
            device=Device.CPU,
            class_names=tuple(manifest["model"]["class_names"]),
        )
        adapter = YOLOv8Adapter(model_spec=spec, config=make_config(manifest))
        with pytest.raises(ModelArtifactCorruptError):
            await adapter.load()
        # Never loaded, never ran inference.
        assert adapter.loaded is False
        assert adapter.stats().total_calls == 0

    async def test_deterministic_detection_contract(self) -> None:
        """Same frame + same model + same config → same normalized geometry."""
        adapter = self._adapter()
        frame_index = 20
        packet_a, dets_a = await self._detect(adapter, frame_index)
        packet_b, dets_b = await self._detect(adapter, frame_index)
        assert len(dets_a) == len(dets_b) == 1
        for attr in ("x_min", "y_min", "x_max", "y_max"):
            assert getattr(dets_a[0].bounding_box, attr) == getattr(dets_b[0].bounding_box, attr)
        assert dets_a[0].confidence == dets_b[0].confidence
        assert dets_a[0].class_name == dets_b[0].class_name == "person"
        # Different frame ids → different observations, same geometry.
        assert packet_a.frame_id != packet_b.frame_id

    async def test_provenance_preserved_verbatim(self) -> None:
        """Every observation copies frame/session/source/event-time verbatim."""
        manifest = _load_manifest()
        adapter = self._adapter()
        frame_index = 12
        packet, detections = await self._detect(adapter, frame_index)
        assert len(detections) == 1
        det = detections[0]
        assert det.frame_id == packet.frame_id
        assert det.session_id == packet.session_id
        assert det.source_ref == packet.source_ref
        assert det.frame_index == packet.frame_index == frame_index
        assert det.event_time == packet.event_time
        assert det.schema_version == SCHEMA_VERSION
        # The canonical provenance invariant holds (re-validated here).
        validate_detection_provenance(packet, detections)
        # Canonical model provenance on every observation.
        meta = manifest["model"]
        assert det.detector_metadata is not None
        assert det.detector_metadata["model_id"] == meta["id"]
        assert det.detector_metadata["model"] == meta["name"]
        assert det.detector_metadata["model_version"] == meta["version"]
        assert det.detector_metadata["artifact_sha256"] == meta["artifact_sha256"]
        assert det.detector_metadata["device"] == "cpu"

    async def test_inference_metrics(self) -> None:
        """DetectionStats count calls, failures, and detections."""
        adapter = self._adapter()
        # 1 empty + 2 valid frames.
        await self._detect(adapter, frame_index=0)
        await self._detect(adapter, frame_index=8)
        await self._detect(adapter, frame_index=26)
        stats = adapter.stats()
        assert stats.total_calls == 3
        assert stats.total_failed_calls == 0
        assert stats.total_detections == 2  # 2 frames x 1 person
        assert stats.last_inference_seconds is not None and stats.last_inference_seconds >= 0
        assert stats.total_inference_seconds >= 0
        assert stats.device == "cpu"


# ---------------------------------------------------------------------------
# The slice as a bounded batch over the full fixture sequence
# ---------------------------------------------------------------------------


class TestVerticalSliceBatch:
    @pytest.fixture(autouse=True)
    def _sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fake_sdk(monkeypatch, _load_manifest())

    async def test_full_fixture_sequence_batch(self) -> None:
        """Every on-frame entry detects one person; empty entries detect none.

        The expected object class (person) appears exactly on the frames the
        controlled fixture renders it — never tuned around one accidental
        detection: the golden timeline says person is present on
        [enter_frame, empty_from).
        """
        manifest = _load_manifest()
        traj = manifest["trajectory"]
        meta = manifest["metadata"]
        capture = datetime.fromisoformat(meta["capture_time"])
        session_id = VideoSessionId(new_uuid())
        source_ref = VideoAssetId(new_uuid())

        inputs: list[DetectionInput] = []
        for frame_index in range(meta["frame_count"]):
            packet = _frame_packet(
                frame_index=frame_index,
                event_time=capture + timedelta(seconds=frame_index / meta["fps"]),
                session_id=session_id,
                source_ref=source_ref,
            )
            inputs.append(DetectionInput(frame=packet, image=_frame_image(frame_index)))

        adapter = YOLOv8Adapter(model_spec=make_spec(manifest), config=make_config(manifest))
        results = await adapter.detect_batch(inputs)
        assert len(results) == meta["frame_count"]

        for frame_index, detections in enumerate(results):
            golden = golden_detection(manifest, frame_index)
            if golden is None:
                assert detections == []
            else:
                assert len(detections) == 1
                det = detections[0]
                assert det.class_name == "person"
                # Geometry matches the golden box normalized to the frame.
                assert det.bounding_box.x_min == pytest.approx(
                    golden["x1"] / meta["width"], abs=1e-6
                )
                assert det.bounding_box.y_min == pytest.approx(
                    golden["y1"] / meta["height"], abs=1e-6
                )
                assert det.bounding_box.x_max == pytest.approx(
                    golden["x2"] / meta["width"], abs=1e-6
                )
                assert det.bounding_box.y_max == pytest.approx(
                    golden["y2"] / meta["height"], abs=1e-6
                )
                # Per-frame provenance preserved through the batch.
                validate_detection_provenance(inputs[frame_index].frame, detections)
        # The person walks left → right: x_min is strictly increasing across
        # the on-frame interval (deterministic trajectory, not accidental).
        on_frame = [
            results[i][0].bounding_box.x_min for i in range(traj["enter_frame"], traj["empty_from"])
        ]
        assert on_frame == sorted(on_frame)
        assert on_frame[-1] > on_frame[0]
        # One bounded batch call served the whole fixture sequence.
        assert adapter.stats().total_calls == 1
        assert adapter.stats().total_detections == traj["empty_from"] - traj["enter_frame"]


# ---------------------------------------------------------------------------
# Business logic must not import Ultralytics objects
# ---------------------------------------------------------------------------


class TestNoSdkLeak:
    def test_test_module_imports_only_the_adapter_boundary(self) -> None:
        """The slice's detection surface is the ObjectDetector protocol —
        no Ultralytics types are imported by business logic (this module
        imports only ``yolo_adapter`` + canonical contracts; the SDK is
        injected behind the adapter's lazy seam)."""
        import backend.app.intelligence.detectors.base as base_module

        source = Path(base_module.__file__).read_text()
        assert "ultralytics" not in source
        assert "YOLO" not in source

    def test_adapter_confines_sdk_behind_lazy_seam(self) -> None:
        """The ONLY application module referencing the SDK is yolo_adapter,
        and even it imports it lazily (never at module scope)."""
        import backend.app.intelligence.detectors.yolo_adapter as adapter_module

        source = Path(adapter_module.__file__).read_text()
        # No module-level SDK import; the lazy seam is the single access point.
        assert 'import_module("ultralytics")' in source
        # Business modules under rules/ must not reference the SDK.
        import backend.app.intelligence.rules.engine as engine_module

        engine_source = Path(engine_module.__file__).read_text()
        assert "ultralytics" not in engine_source
