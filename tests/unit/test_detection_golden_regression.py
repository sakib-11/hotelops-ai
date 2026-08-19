"""Golden detection regression tests (Task 12, Phase 8).

Deterministic regression tests over APPROVED synthetic scene fixtures
(tests/unit/fixtures/detection/) — no random test data, no wall-clock
timestamps in the fixtures.

Each scene fixture records:

- a synthetic scene image (``*.png``, pure-stdlib PNG, byte-identical
  across runs);
- the golden SDK predictions the reference model emits for the scene
  (``predictions``);
- the golden expected ``DetectionObservation`` values the adapter must
  produce (``expected`` — normalized boxes, classes, confidences).

The regression suite drives the REAL ``YOLOv8Adapter`` code path
(load -> decode -> predict -> translate -> normalize) with the same
fake-SDK seam used by ``test_yolo_adapter.py``, then asserts the
emitted observations match the golden expectations within the fixture
tolerances.

Locked behavior per scene:

1. normal          — detection presence, class, confidence, normalized
                      coordinates, provenance, model version
2. empty           — empty detection is a valid result (no detections)
3. multiple        — multiple objects of different classes
4. low_confidence  — confidence threshold behavior (below-threshold
                      predictions filtered, like the real SDK)
5. boundary        — boundary objects: boxes on the exact frame edges
                      normalize to exactly 0.0 / 1.0
6. video           — representative multi-frame sequence: per-frame
                      provenance, frame index sequence, event-timestamp
                      behavior, batch inference
7. file_source     — the video sequence re-driven through the REAL Task 11
                      boundary (FileFrameSource -> FramePacket -> adapter):
                      object bytes flow storage -> decoder -> source ->
                      DetectionInput, with recorded timestamp policy
                      (capture_time + pts) and lifecycle DRAINING -> CLOSED

NOTE: The video scenes above do NOT require a video library — the
      decoder SDK is isolated behind the FrameDecoder protocol (same
      convention as the storage port).  The golden ``video`` fixture
      defines the decoded frame sequence deterministically.

Floating-point comparison uses the fixture's ``tolerances`` (abs/rel
1e-9) — bit-identical floats are NOT required (the runtime does not
guarantee them).

NOTE: These tests assert adapter DETERMINISM, not detection accuracy.
No accuracy baseline is asserted here.  The pilot accuracy baseline is
not defined in docs/product (pilot-baseline.md is DRAFT, success
criteria TBD) — see the Phase 8 report.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.intelligence.detectors import (
    DetectionInput,
    DetectorConfig,
    Device,
    ModelSpec,
    yolo_adapter,
)
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from backend.app.intelligence.sources.base import FrameSourceState
from backend.app.intelligence.sources.decoder import DecodedFrame
from backend.app.intelligence.sources.file import FileFrameSource
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

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "detection"
GENERATOR_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "generate_detection_fixtures.py"
)

pytestmark = [pytest.mark.cv_regression]


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def load_scene(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.json"
    with open(path) as f:
        return json.load(f)


def scene_image_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / f"{name}.png").read_bytes()


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read (width, height) from a PNG's IHDR chunk (pure stdlib)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG file"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


# ---------------------------------------------------------------------------
# Fake SDK (same seam pattern as test_yolo_adapter.py)
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


class FakeYOLO:
    """Deterministic stand-in for ``ultralytics.YOLO``.

    Serves the fixture's golden predictions, one prediction-list per
    input (single image or batch), applying the configured confidence
    threshold exactly like the real SDK's ``predict(conf=)`` filtering.
    Always returns a LIST of ``FakeResult`` values — the shape the
    adapter's translation layer iterates.
    """

    instances: ClassVar[list[FakeYOLO]] = []

    def __init__(self, artifact_uri: str, names: dict[int, str]) -> None:
        self.artifact_uri = artifact_uri
        self.names: dict[int, str] = names
        FakeYOLO.instances.append(self)

    def predict(self, **kwargs: Any) -> Any:
        source = kwargs.get("source")
        payloads = kwargs["_fixture_predictions_per_input"]
        conf_threshold = kwargs["conf"]
        if isinstance(source, list):
            # Batch: one result per input, each with ITS OWN predictions.
            if len(payloads) != len(source):
                msg = f"{len(payloads)} prediction-lists for {len(source)} inputs"
                raise AssertionError(msg)
            return [FakeResult(fake_boxes(*_passed(p, conf_threshold))) for p in payloads]
        return [FakeResult(fake_boxes(*_passed(payloads[0], conf_threshold)))]


def fake_boxes(*rows: tuple[float, float, float, float, float, int]) -> FakeBoxes:
    return FakeBoxes(
        [list(row[:4]) for row in rows],
        [[row[4]] for row in rows],
        [[row[5]] for row in rows],
    )


def _passed(
    predictions: list[dict], conf_threshold: float
) -> tuple[tuple[float, float, float, float, float, int], ...]:
    """Filter one prediction-list by the confidence threshold (SDK behavior)."""
    kept = [p for p in predictions if p["confidence"] >= conf_threshold]
    return tuple((p["x1"], p["y1"], p["x2"], p["y2"], p["confidence"], p["class_id"]) for p in kept)


def install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, scene: dict, predictions_per_input: list[list[dict]]
) -> None:
    """Inject the fixture-driven fake SDK + deterministic decode seam.

    ``predictions_per_input`` is one golden prediction-list per input
    (single image scenes: one entry; video: one entry per frame).
    """
    FakeYOLO.instances = []
    names = {i: name for i, name in enumerate(scene["model"]["class_names"])}
    module = types.ModuleType("ultralytics")

    class _YOLO(FakeYOLO):
        def __init__(self, artifact_uri: str) -> None:
            super().__init__(artifact_uri, names)

        def predict(self, **kwargs: Any) -> Any:
            # The fixture's golden predictions are the model output for
            # this input; the fake applies the SDK conf filter.
            kwargs["_fixture_predictions_per_input"] = predictions_per_input
            return super().predict(**kwargs)

    module.YOLO = _YOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_mps_available", lambda: False)

    def decode(image: bytes) -> tuple[Any, int, int]:
        # Decode dimensions from the REAL fixture PNG header — the image
        # bytes genuinely flow through the pipeline.
        width, height = png_dimensions(image)
        return object(), width, height

    monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", decode)


# ---------------------------------------------------------------------------
# Adapter construction from a scene fixture
# ---------------------------------------------------------------------------


def make_spec(scene: dict) -> ModelSpec:
    model = scene["model"]
    return ModelSpec(
        model_id=model["id"],
        model_name=model["name"],
        model_version=model["version"],
        artifact_uri=model["artifact_uri"],
        artifact_sha256=model["artifact_sha256"],
        device=Device.CPU,
        class_names=tuple(model["class_names"]),
    )


def make_config(scene: dict) -> DetectorConfig:
    config = scene["config"]
    return DetectorConfig(
        confidence_threshold=config["confidence_threshold"],
        nms_iou_threshold=config["nms_iou_threshold"],
        max_detections=config["max_detections"],
        input_width=config["input_width"],
        input_height=config["input_height"],
        device=Device.CPU,
    )


def make_frame(*, frame_index: int = 0, event_time: datetime | None = None) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=VideoSessionId(new_uuid()),
        source_ref=None,
        frame_index=frame_index,
        event_time=event_time or utc_now(),
        width=1920,
        height=1080,
    )


def tolerance(scene: dict) -> dict:
    return scene["tolerances"]


def assert_matches_golden(
    det: DetectionObservation,
    golden: dict,
    *,
    frame: FramePacket,
    scene: dict,
    tol: dict,
) -> None:
    """Assert one detection matches the golden expectation + provenance."""
    # Class identity + label
    assert det.class_id == golden["class_id"]
    assert det.class_name == golden["class_name"]
    # Confidence within tolerance (not bit-identical)
    assert det.confidence == pytest.approx(golden["confidence"], abs=tol["abs"], rel=tol["rel"])
    # Normalized coordinates within tolerance
    box = det.bounding_box
    assert box.x_min == pytest.approx(golden["x_min"], abs=tol["abs"], rel=tol["rel"])
    assert box.y_min == pytest.approx(golden["y_min"], abs=tol["abs"], rel=tol["rel"])
    assert box.x_max == pytest.approx(golden["x_max"], abs=tol["abs"], rel=tol["rel"])
    assert box.y_max == pytest.approx(golden["y_max"], abs=tol["abs"], rel=tol["rel"])
    # Image dimensions recorded
    assert det.image_width == scene["width"]
    assert det.image_height == scene["height"]
    # Provenance copied verbatim from the FramePacket
    assert det.frame_id == frame.frame_id
    assert det.session_id == frame.session_id
    assert det.source_ref == frame.source_ref
    assert det.frame_index == frame.frame_index
    assert det.event_time == frame.event_time
    assert det.schema_version == SCHEMA_VERSION
    # Model identity + version + artifact provenance
    assert det.detector_metadata is not None
    assert det.detector_metadata["model_id"] == scene["model"]["id"]
    assert det.detector_metadata["model"] == scene["model"]["name"]
    assert det.detector_metadata["model_version"] == scene["model"]["version"]
    assert det.detector_metadata["artifact_sha256"] == scene["model"]["artifact_sha256"]
    assert det.detector_metadata["device"] == "cpu"


# ---------------------------------------------------------------------------
# Image scenes: normal / empty / multiple / low_confidence / boundary
# ---------------------------------------------------------------------------


class TestImageScenes:
    @pytest.mark.parametrize(
        "name",
        ["normal", "empty", "multiple", "low_confidence", "boundary"],
    )
    async def test_scene_matches_golden(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        scene = load_scene(name)
        expected = scene["expected"]
        tol = tolerance(scene)
        install_fake_sdk(monkeypatch, scene, [scene["predictions"]])

        frame = make_frame(frame_index=7)
        image = scene_image_bytes(name)
        assert png_dimensions(image) == (scene["width"], scene["height"])

        adapter = YOLOv8Adapter(model_spec=make_spec(scene), config=make_config(scene))
        detections = await adapter.detect(DetectionInput(frame=frame, image=image))

        # Detection presence
        assert len(detections) == expected["detection_count"]
        for det, golden in zip(detections, expected["detections"], strict=True):
            assert_matches_golden(det, golden, frame=frame, scene=scene, tol=tol)

    # --- confidence behavior (low_confidence scene) ---
    async def test_low_confidence_filters_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scene = load_scene("low_confidence")
        install_fake_sdk(monkeypatch, scene, [scene["predictions"]])
        adapter = YOLOv8Adapter(model_spec=make_spec(scene), config=make_config(scene))
        detections = await adapter.detect(
            DetectionInput(frame=make_frame(), image=scene_image_bytes("low_confidence"))
        )
        # Only the 0.72 detection survives the 0.5 threshold.
        assert len(detections) == 1
        assert detections[0].confidence == pytest.approx(0.72)
        assert detections[0].class_name == "person"
        # Confidence is NOT clamped or faked — values pass through as-is.
        assert all(d.confidence >= scene["config"]["confidence_threshold"] for d in detections)

    # --- boundary normalization (boundary scene) ---
    async def test_boundary_objects_normalize_to_exact_edges(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scene = load_scene("boundary")
        tol = tolerance(scene)
        install_fake_sdk(monkeypatch, scene, [scene["predictions"]])
        adapter = YOLOv8Adapter(model_spec=make_spec(scene), config=make_config(scene))
        detections = await adapter.detect(
            DetectionInput(frame=make_frame(), image=scene_image_bytes("boundary"))
        )
        assert len(detections) == 3
        # Full-frame box: exactly 0.0..1.0 on both axes.
        full = detections[0]
        assert full.bounding_box.x_min == pytest.approx(0.0, abs=tol["abs"])
        assert full.bounding_box.y_min == pytest.approx(0.0, abs=tol["abs"])
        assert full.bounding_box.x_max == pytest.approx(1.0, abs=tol["abs"])
        assert full.bounding_box.y_max == pytest.approx(1.0, abs=tol["abs"])


# ---------------------------------------------------------------------------
# Video scene: per-frame provenance, frame index, timestamp behavior
# ---------------------------------------------------------------------------


class _SceneFrameDecoder:
    """Deterministic in-memory ``FrameDecoder`` for the video fixture.

    Emits the pre-built ``DecodedFrame`` sequence exactly once (EOF on
    exhaustion); the container bytes from storage are received but not
    parsed (the decode library is isolated behind this protocol).
    """

    def __init__(self, frames: list[DecodedFrame]) -> None:
        self._frames = list(frames)
        self._position = 0
        self.opened = False
        self.closed = False
        self.received_stream: AsyncIterator[bytes] | None = None

    async def open(self, stream: AsyncIterator[bytes]) -> None:
        self.opened = True
        self.received_stream = stream

    async def read(self) -> DecodedFrame | None:
        if not self.opened:
            msg = "decoder not opened"
            raise RuntimeError(msg)
        if self._position >= len(self._frames):
            return None
        frame = self._frames[self._position]
        self._position += 1
        return frame

    async def close(self) -> None:
        self.closed = True


async def _object_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


class TestVideoScene:
    async def test_frames_preserve_provenance_index_and_timestamps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scene = load_scene("video")
        tol = tolerance(scene)
        frames_meta = scene["frames"]
        # All frames share ONE session; timestamps advance by 1s.
        session_id = VideoSessionId(new_uuid())
        base_time = utc_now()
        frames: list[FramePacket] = []
        inputs: list[DetectionInput] = []
        for meta in frames_meta:
            frame = FramePacket(
                frame_id=FrameId(new_uuid()),
                session_id=session_id,
                source_ref=None,
                frame_index=meta["index"],
                event_time=base_time + timedelta(seconds=meta["index"]),
                width=1920,
                height=1080,
            )
            frames.append(frame)
            inputs.append(
                DetectionInput(
                    frame=frame, image=scene_image_bytes(meta["image"].removesuffix(".png"))
                )
            )

        install_fake_sdk(monkeypatch, scene, [m["predictions"] for m in frames_meta])
        adapter = YOLOv8Adapter(model_spec=make_spec(scene), config=make_config(scene))

        # Batch inference over the representative sequence.
        results = await adapter.detect_batch(inputs)
        assert len(results) == len(frames_meta)

        for index, (detections, frame, meta) in enumerate(
            zip(results, frames, frames_meta, strict=True)
        ):
            # Detection presence per frame
            assert len(detections) == meta["expected"]["detection_count"]
            assert len(detections) == 1
            det = detections[0]
            # Frame index sequence preserved verbatim
            assert det.frame_index == index
            assert det.frame_index == frame.frame_index
            # Session identity shared across the sequence
            assert det.session_id == session_id
            # Event-timestamp behavior: verbatim copy, strictly increasing
            assert det.event_time == frame.event_time
            assert det.event_time == base_time + timedelta(seconds=index)
            assert_matches_golden(
                det, meta["expected"]["detections"][0], frame=frame, scene=scene, tol=tol
            )

        # The person walks left to right: x_min increases across frames.
        x_positions = [results[i][0].bounding_box.x_min for i in range(len(results))]
        assert x_positions == sorted(x_positions)
        assert x_positions[-1] > x_positions[0]
        # One SDK call served the whole batch (bounded, provenance-preserving).
        assert adapter.stats().total_calls == 1


# ---------------------------------------------------------------------------
# Golden video through the REAL Task 11 boundary (FileFrameSource)
# ---------------------------------------------------------------------------


class TestVideoSceneViaFileFrameSource:
    """Golden video regression through the real ingestion boundary.

    ``video fixture → FileFrameSource → FramePacket → ObjectDetector
    → YOLOv8Adapter → DetectionObservation``.

    The recorded-video object bytes genuinely flow: storage → decoder →
    ``FileFrameSource`` → ``DetectionInput`` → adapter (decode seam reads
    the real PNG header), so the canonical observations are produced
    from source-emitted packets — not hand-built ``FramePacket`` values.
    """

    async def test_golden_video_via_real_file_source_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scene = load_scene("video")
        tol = tolerance(scene)
        frames_meta = scene["frames"]
        # Deterministic recording time — never wall-clock (fixture policy).
        capture_time = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

        # 1. Seed the recorded-video object in (in-memory) storage.
        storage = FakeStorageAdapter()
        object_key = "tenants/golden/venues/v1/recordings/2026/07/29/cam1.mp4"
        await storage.put_object_stream(
            object_key,
            _object_stream(b"golden-video-fixture"),
            content_type="video/mp4",
            size_bytes=len(b"golden-video-fixture"),
        )

        # 2. Decoder emits one DecodedFrame per fixture frame: the REAL
        #    scene PNG bytes as pixel payload, dims from the PNG header,
        #    presentation time = frame index.
        decoded: list[DecodedFrame] = []
        for meta in frames_meta:
            image = scene_image_bytes(meta["image"].removesuffix(".png"))
            width, height = png_dimensions(image)
            decoded.append(
                DecodedFrame(
                    width=width,
                    height=height,
                    data=image,
                    pts_seconds=float(meta["index"]),
                )
            )

        # 3. The real FileFrameSource over storage + decoder.
        decoder = _SceneFrameDecoder(decoded)
        session_id = VideoSessionId(new_uuid())
        source = FileFrameSource(
            session_id=session_id,
            source_ref=VideoAssetId(new_uuid()),
            storage=storage,
            object_key=object_key,
            decoder=decoder,
            capture_time=capture_time,
        )

        install_fake_sdk(monkeypatch, scene, [m["predictions"] for m in frames_meta])
        adapter = YOLOv8Adapter(model_spec=make_spec(scene), config=make_config(scene))

        # 4. Iterate the source: each FramePacket carries its decoded
        #    bytes companion (last_frame_data) — the pipeline's real
        #    envelope -> payload pairing.
        packets: list[FramePacket] = []
        images: list[bytes] = []
        async with source:
            # The storage object's byte stream reached the decoder.
            assert decoder.opened is True
            assert decoder.received_stream is not None
            for meta in frames_meta:
                packet = await anext(source)
                assert source.last_frame_data is not None
                packets.append(packet)
                images.append(source.last_frame_data.data)
                # Recorded timestamp policy: capture_time + pts_seconds.
                assert packet.event_time == capture_time + timedelta(seconds=meta["index"])
            with pytest.raises(StopAsyncIteration):  # clean EOF
                await anext(source)
            assert source.state is FrameSourceState.DRAINING
        # Source shutdown released the decoder (FileFrameSource._stop).
        assert decoder.closed is True
        assert source.state is FrameSourceState.CLOSED

        # 5. Detection over the source-emitted sequence (bounded batch).
        assert len(packets) == len(frames_meta)
        results = await adapter.detect_batch([
            DetectionInput(frame=packet, image=image)
            for packet, image in zip(packets, images, strict=True)
        ])
        assert len(results) == len(frames_meta)
        for index, (detections, packet, meta) in enumerate(
            zip(results, packets, frames_meta, strict=True)
        ):
            assert len(detections) == meta["expected"]["detection_count"]
            det = detections[0]
            # Frame index sequence follows the Task 11 convention (0..n-1).
            assert det.frame_index == index == meta["index"]
            # Session + source identity from the source, verbatim.
            assert det.session_id == session_id
            assert det.session_id == packet.session_id
            assert det.source_ref == packet.source_ref
            assert det.source_ref == source.source_ref
            # Timestamp preserved verbatim (never replaced with wall-clock).
            assert det.event_time == packet.event_time
            assert det.event_time == capture_time + timedelta(seconds=index)
            assert_matches_golden(
                det, meta["expected"]["detections"][0], frame=packet, scene=scene, tol=tol
            )

        # The person walks left to right across the source-driven sequence.
        x_positions = [results[i][0].bounding_box.x_min for i in range(len(results))]
        assert x_positions == sorted(x_positions)
        assert x_positions[-1] > x_positions[0]
        # One bounded batch served the whole source sequence.
        assert adapter.stats().total_calls == 1


# ---------------------------------------------------------------------------
# Determinism / reproducibility: regeneration is byte-identical
# ---------------------------------------------------------------------------


class TestFixtureReproducibility:
    def test_fixtures_regenerate_byte_identically(self, tmp_path: Path) -> None:
        """The committed fixtures are exactly what the generator produces.

        Proves determinism: no random data, no wall-clock timestamps —
        regenerating must be a byte-identical no-op.
        """
        spec = importlib.util.spec_from_file_location("detection_fixture_gen", GENERATOR_SCRIPT)
        assert spec is not None and spec.loader is not None
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)

        written = generator.generate(tmp_path)
        assert len(written) >= 15  # 5 image scenes + 4 video frames + JSONs

        for path in written:
            committed = FIXTURES_DIR / path.name
            assert committed.exists(), f"generator produced unknown file {path.name}"
            assert path.read_bytes() == committed.read_bytes(), (
                f"fixture {path.name} is not reproducible (committed bytes differ)"
            )

        # And nothing is missing from the committed set.
        committed_files = sorted(FIXTURES_DIR.glob("*"))
        assert {p.name for p in committed_files} == {p.name for p in written}
