"""YOLOv8 adapter — the concrete detector behind the ObjectDetector port.

THIS module is the ONLY place in the application that references the
detection SDK.  Everything below is confined here:

- the SDK is imported lazily via :func:`importlib.import_module` (never
  a module-level import), so the rest of the application imports and
  runs without the ``cv`` extras installed;
- ``Results`` / ``Boxes`` objects and SDK tensors exist only inside
  this module's functions and never cross the boundary;
- the public surface exposes only the ``ObjectDetector`` protocol
  types (``DetectionInput``, ``DetectionObservation``) and typed
  ``DetectionError`` exceptions.

Behavior contract (Task 12 Phase 4):

1.  Load the configured model artifact (``ModelSpec.artifact_uri`` —
    never hardcoded), after verifying its configured SHA-256 checksum
    (a mismatch raises ``ModelArtifactCorruptError`` before load).
2.  Validate the model: class names must be non-empty and must match
    the declared ``ModelSpec.class_names``.
3.  Identify model name / version from ``ModelSpec``.
4.  Preserve artifact provenance (URI + SHA-256) in every
    ``detector_metadata``.
5.  Select the configured CPU/GPU device — explicitly, with no silent
    fallback: a requested but unavailable CUDA/MPS device raises
    ``UnsupportedDeviceError`` unless ``DetectorConfig.allow_cpu_fallback``
    is explicitly enabled.
6.  Perform inference with the configured confidence threshold, NMS
    IoU threshold and max detections.
7.  Translate normalized, provenance-complete ``DetectionObservation``
    values (source/session/frame/event-time copied from the input
    ``FramePacket``; boxes normalized to [0, 1] relative to the frame).
    Malformed model geometry is NEVER silently hidden: boxes are
    normalized through :func:`normalize_xyxy`, which raises
    ``InvalidGeometryError`` for out-of-range coordinates, inverted
    corners or zero-size boxes, and confidence outside [0, 1] beyond
    float32 boundary noise is an ``InferenceError``.
8.  Record inference duration + device information (``stats()``).
9.  Support explicit warmup and resource cleanup (``close()``).
10. Handle empty results, undecodable frames and inference failures
    with typed errors; the detector remains usable afterwards.
"""

from __future__ import annotations

import hashlib
import importlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_DETECTIONS,
    record_pipeline_metric,
)
from backend.app.intelligence.detectors.base import (
    DEFAULT_DETECTOR_CONFIG,
    BatchDetector,
    DetectionInput,
    DetectorConfig,
    ModelSpec,
    ObjectDetector,
    detection_metadata,
    resolve_device,
)
from backend.app.intelligence.detectors.exceptions import (
    DetectionError,
    InferenceError,
    ModelArtifactCorruptError,
    ModelLoadError,
)
from backend.app.intelligence.detectors.normalize import (
    NORMALIZATION_EPSILON,
    normalize_xyxy,
)
from contracts.common import DetectionId, new_uuid
from contracts.video import FramePacket
from contracts.vision import DetectionObservation

__all__ = ["DetectionStats", "YOLOv8Adapter", "resolve_device"]


# =========================================================================
# Lazy SDK seams — the only SDK access points in the application.
# Each is a module-level function so tests can substitute them without
# installing the SDK.
# =========================================================================


def _import_ultralytics() -> Any:
    """Import the SDK lazily, raising a typed error when absent."""
    try:
        return importlib.import_module("ultralytics")
    except ImportError as exc:
        msg = "the detection SDK is not installed; install the 'cv' extras to use this adapter"
        raise ModelLoadError(msg, cause=exc) from exc


def _cuda_available() -> bool:
    """Probe CUDA availability without importing torch at module scope."""
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def _mps_available() -> bool:
    """Probe Apple MPS availability without importing torch at module scope."""
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return False
    backends = getattr(torch, "backends", None)
    mps = getattr(backends, "mps", None) if backends is not None else None
    if mps is None:
        return False
    return bool(mps.is_available())


def _decode_image_bytes(image: bytes) -> tuple[Any, int, int]:
    """Decode encoded frame bytes to a BGR image array.

    Returns ``(array, width, height)``.  An undecodable payload is a
    frame-level inference failure (``InferenceError``).
    """
    try:
        numpy = importlib.import_module("numpy")
        cv2 = importlib.import_module("cv2")
    except ImportError as exc:
        msg = "image decoding requires the 'cv' extras (numpy + opencv)"
        raise InferenceError(msg, cause=exc) from exc
    raw = numpy.frombuffer(image, dtype=numpy.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if img is None:
        msg = "image bytes could not be decoded"
        raise InferenceError(msg)
    height, width = img.shape[0], img.shape[1]
    return img, int(width), int(height)


def _blank_image(config: DetectorConfig) -> Any:
    """A zero frame of the configured input size, used for warmup passes."""
    numpy = importlib.import_module("numpy")
    return numpy.zeros((config.input_height, config.input_width, 3), dtype=numpy.uint8)


def _validated_confidence(value: Any) -> float:
    """Validate a model confidence, snapping float32 boundary noise.

    Values within ``NORMALIZATION_EPSILON`` of [0, 1] (e.g. a
    ``1.0000001`` from float32 arithmetic) are snapped to the range;
    anything beyond is malformed model output and rejected with
    ``InferenceError`` — never silently hidden.
    """
    conf = _scalar_float(value)
    if -NORMALIZATION_EPSILON <= conf <= 1.0 + NORMALIZATION_EPSILON:
        return min(1.0, max(0.0, conf))
    msg = f"model returned out-of-range confidence {conf!r}"
    raise InferenceError(msg)


def _scalar_float(value: Any) -> float:
    """Extract a float from an SDK scalar or a 1-element container.

    Tolerates the shape differences between SDK tensor versions
    (``(N,)`` vs ``(N, 1)``) and plain list-likes.
    """
    if isinstance(value, (list, tuple)):
        return float(value[0])
    return float(value)


def _scalar_int(value: Any) -> int:
    """Extract an int from an SDK scalar or a 1-element container."""
    if isinstance(value, (list, tuple)):
        return int(value[0])
    return int(value)


def _with_frame_context(exc: DetectionError, frame: FramePacket) -> DetectionError:
    """Return ``exc`` with the input frame identity appended to its message.

    Failures carry the context that actually exists (session ID + frame
    index) without changing the error's category or losing its original
    cause.  ``type`` is preserved so callers can still discriminate
    (e.g. ``InvalidGeometryError`` stays ``InvalidGeometryError``).

    Callers re-raise with ``raise ... from exc`` so the original stays
    visible as ``__cause__`` while ``.cause`` keeps the root cause.
    """
    enriched = f"{exc.message} [frame {frame.frame_index} of session {frame.session_id}]"
    return type(exc)(enriched, cause=exc.cause)


# =========================================================================
# Artifact checksum governance (Task 12, Step 5)
# =========================================================================


def _artifact_sha256(path: Path) -> str:
    """Streaming SHA-256 of a local artifact (bounded memory).

    Model artifacts can be hundreds of MB; the digest is computed in
    1 MiB chunks so peak memory stays flat (the artifact is never
    loaded into RAM just to be hashed).
    """
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_artifact_path(uri: str) -> Path | None:
    """The local file path for an artifact URI, or None when not local.

    Local references are plain paths and ``file://`` URIs.  Non-local
    URIs (``memory://``, ``s3://``, ``http(s)://``) return None —
    their checksums are verified at fetch time by the storage layer and
    the digest is still carried as provenance on every observation.
    """
    if "://" in uri:
        if uri.startswith("file://"):
            return Path(uri.removeprefix("file://"))
        return None
    return Path(uri)


def _verify_artifact_checksum(uri: str, expected_sha256: str) -> None:
    """Verify the artifact's SHA-256 digest before load (fail-fast).

    A configured checksum mismatch is NEVER silently ignored: for a
    locally resolvable artifact a digest mismatch raises
    ``ModelArtifactCorruptError`` before the SDK is even imported.
    A missing local artifact raises ``ModelLoadError``.

    Raises:
        ModelLoadError: the local artifact file does not exist.
        ModelArtifactCorruptError: the computed digest does not match
            the configured checksum.
    """
    path = _local_artifact_path(uri)
    if path is None:
        return
    if not path.is_file():
        msg = f"model artifact not found at '{uri}'"
        raise ModelLoadError(msg)
    actual = _artifact_sha256(path)
    if actual != expected_sha256:
        msg = (
            f"model artifact checksum mismatch for '{uri}': "
            f"expected {expected_sha256}, computed {actual}"
        )
        raise ModelArtifactCorruptError(msg)


def _extract_class_names(names: Any) -> tuple[str, ...]:
    """Normalize the model's class-name table to an ordered tuple.

    Accepts the dict/sequence shapes the SDK exposes; the ordering
    preserves the model's own index mapping.
    """
    if isinstance(names, dict):
        ordered: list[Any] = [names[key] for key in sorted(names)]
    else:
        ordered = list(names)
    if not ordered:
        msg = "model exposes no class names"
        raise ModelLoadError(msg)
    result = tuple(str(name).strip() for name in ordered)
    if any(not name for name in result):
        msg = "model exposes empty class names"
        raise ModelLoadError(msg)
    return result


# =========================================================================
# Inference statistics (metrics boundary hook)
# =========================================================================


@dataclass(frozen=True, slots=True)
class DetectionStats:
    """Atomic snapshot of the adapter's inference observability counters.

    Warmup passes are excluded; every ``detect()`` call is counted
    (success or failure) with its duration and detection yield.
    """

    model_name: str
    model_version: str
    device: str | None
    total_calls: int
    total_failed_calls: int
    total_detections: int
    last_inference_seconds: float | None
    total_inference_seconds: float


# =========================================================================
# The adapter
# =========================================================================


class YOLOv8Adapter(ObjectDetector, BatchDetector):
    """Ultralytics-backed ``ObjectDetector`` implementation.

    All SDK interaction is lazy and confined to this class: the model
    is acquired on first use (``load()``/``warmup()``/``detect()``),
    device resolution is explicit (no silent GPU->CPU fallback), and
    every emitted observation is normalized and provenance-complete.

    Batch capability: the backend supports list-source prediction, so
    this adapter implements ``BatchDetector``.  Batching is applied by
    the execution policy ONLY when explicitly configured — never
    automatically.
    """

    def __init__(
        self,
        *,
        model_spec: ModelSpec,
        config: DetectorConfig | None = None,
    ) -> None:
        self._model_spec = model_spec
        self._config = config or DEFAULT_DETECTOR_CONFIG
        self._model: Any | None = None
        self._names: tuple[str, ...] | None = None
        self._device: str | None = None
        self._loaded = False
        self._total_calls = 0
        self._total_failed_calls = 0
        self._total_detections = 0
        self._last_duration: float | None = None
        self._total_duration = 0.0

    # ------------------------------------------------------------------
    # Identity / observability
    # ------------------------------------------------------------------

    @property
    def model_spec(self) -> ModelSpec:
        """The model identity + artifact provenance this adapter serves."""
        return self._model_spec

    @property
    def device(self) -> str | None:
        """The resolved runtime device (None until loaded)."""
        return self._device

    @property
    def loaded(self) -> bool:
        """True once the model artifact is loaded."""
        return self._loaded

    def stats(self) -> DetectionStats:
        """Snapshot of inference counters (duration, detections, device)."""
        return DetectionStats(
            model_name=self._model_spec.model_name,
            model_version=self._model_spec.model_version,
            device=self._device,
            total_calls=self._total_calls,
            total_failed_calls=self._total_failed_calls,
            total_detections=self._total_detections,
            last_inference_seconds=self._last_duration,
            total_inference_seconds=self._total_duration,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Load (or reload) the model artifact. Idempotent; fail-fast."""
        await self._load()

    async def close(self) -> None:
        """Release the loaded model and cached state. Idempotent.

        GPU memory is released best-effort; the next ``detect()``/
        ``warmup()`` call re-acquires the model.
        """
        device = self._device
        self._model = None
        self._names = None
        self._device = None
        self._loaded = False
        if device is not None and device.startswith("cuda"):
            try:
                torch = importlib.import_module("torch")
                torch.cuda.empty_cache()
            except Exception:
                # Best-effort cache release; never mask the cleanup path.
                pass

    async def warmup(self) -> None:
        """Explicitly prime inference resources (load + N synthetic passes).

        Warmup passes are executed with the configured input size and
        device but are excluded from ``stats()``.
        """
        await self._load()
        for _ in range(self._config.warmup_frames):
            self._predict(_blank_image(self._config))

    # ------------------------------------------------------------------
    # ObjectDetector protocol
    # ------------------------------------------------------------------

    async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
        """Run YOLOv8 inference on one frame.

        Returns a normalized, provenance-complete observation per
        detected object, or ``[]`` for an empty/blank frame.  Raises
        ``InferenceError`` (non-fatal, per frame) on decode/inference
        failures, ``InvalidGeometryError`` when the model emits
        malformed geometry (never silently clamped), and ``ValueError``
        for invalid input (enforced by ``DetectionInput``).
        """
        await self._load()
        start = time.perf_counter()
        detections: list[DetectionObservation] = []
        failed = False
        try:
            image, width, height = _decode_image_bytes(inp.image)
            results = self._predict(image)
            detections = self._translate(results, inp, width, height)
        except DetectionError as exc:
            failed = True
            raise _with_frame_context(exc, inp.frame) from exc
        except Exception as exc:
            failed = True
            msg = (
                f"detection failed for frame {inp.frame.frame_index} "
                f"of session {inp.frame.session_id}"
            )
            raise InferenceError(msg, cause=exc) from exc
        finally:
            self._record(time.perf_counter() - start, len(detections), failed=failed)
        return detections

    async def detect_batch(
        self, inputs: Sequence[DetectionInput]
    ) -> list[list[DetectionObservation]]:
        """Run ONE SDK prediction over a bounded batch of frames.

        The backend's list-source prediction returns one result per
        input image; each result is translated back against ITS input,
        preserving per-frame provenance.  An empty batch is a no-op
        (``[]``).  A result-count mismatch is an ``InferenceError``;
        malformed geometry in any result raises
        ``InvalidGeometryError`` (never silently clamped).
        """
        await self._load()
        if not inputs:
            return []
        start = time.perf_counter()
        images: list[Any] = []
        dims: list[tuple[int, int]] = []
        results_all: list[list[DetectionObservation]] = [[] for _ in inputs]
        failed = False
        try:
            for inp in inputs:
                try:
                    image, width, height = _decode_image_bytes(inp.image)
                except DetectionError as exc:
                    # A decode failure IS attributable to the frame being
                    # decoded — carry its identity (session + index).
                    raise _with_frame_context(exc, inp.frame) from exc
                images.append(image)
                dims.append((width, height))
            sdk_results = self._predict(images)
            if sdk_results is None or len(sdk_results) != len(inputs):
                count = 0 if sdk_results is None else len(sdk_results)
                msg = f"batch inference returned {count} results for {len(inputs)} inputs"
                raise InferenceError(msg)
            for index, (inp, sdk_result) in enumerate(zip(inputs, sdk_results, strict=True)):
                width, height = dims[index]
                results_all[index] = self._translate([sdk_result], inp, width, height)
        except DetectionError:
            failed = True
            raise
        except Exception as exc:
            failed = True
            msg = f"batch detection failed for {len(inputs)} frames"
            raise InferenceError(msg, cause=exc) from exc
        finally:
            total = sum(len(results) for results in results_all)
            self._record(time.perf_counter() - start, total, failed=failed)
        return results_all

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _load(self) -> None:
        """Acquire the model + device exactly once (idempotent)."""
        if self._loaded:
            return
        # Governance (Task 12 Step 5): verify the artifact checksum
        # BEFORE the SDK is even imported — a corrupt or missing
        # artifact fails initialization fast, never mid-stream.
        _verify_artifact_checksum(self._model_spec.artifact_uri, self._model_spec.artifact_sha256)
        ultralytics = _import_ultralytics()
        device_str = resolve_device(
            self._config.device,
            allow_cpu_fallback=self._config.allow_cpu_fallback,
            cuda_available=_cuda_available(),
            mps_available=_mps_available(),
        )
        try:
            model = ultralytics.YOLO(self._model_spec.artifact_uri)
        except Exception as exc:
            msg = f"failed to load model artifact '{self._model_spec.artifact_uri}'"
            raise ModelLoadError(msg, cause=exc) from exc
        try:
            names = _extract_class_names(model.names)
        except DetectionError:
            raise
        except Exception as exc:
            msg = "failed to read model class names"
            raise ModelLoadError(msg, cause=exc) from exc
        if set(names) != set(self._model_spec.class_names):
            msg = (
                f"artifact class names {sorted(names)} do not match declared spec "
                f"{sorted(self._model_spec.class_names)}"
            )
            raise ModelLoadError(msg)
        self._model = model
        self._names = names
        self._device = device_str
        self._loaded = True

    def _predict(self, image: Any) -> Any:
        """Run the SDK prediction with the configured inference knobs."""
        model = self._model
        if model is None:  # pragma: no cover - _load() always precedes
            raise InferenceError("detector is not loaded")
        try:
            return model.predict(
                source=image,
                conf=self._config.confidence_threshold,
                iou=self._config.nms_iou_threshold,
                imgsz=(self._config.input_height, self._config.input_width),
                device=self._device,
                half=self._config.half_precision,
                max_det=self._config.max_detections,
                verbose=False,
            )
        except Exception as exc:
            msg = (
                f"detector inference failed for model {self._model_spec.model_id}"
                f"@{self._model_spec.model_version}"
            )
            raise InferenceError(msg, cause=exc) from exc

    def _translate(
        self,
        results: Any,
        inp: DetectionInput,
        width: int,
        height: int,
    ) -> list[DetectionObservation]:
        """Convert SDK results into normalized DetectionObservation values.

        SDK tensors/response types are consumed here and never returned.
        Every box is normalized through :func:`normalize_xyxy` (the
        project's coordinate convention); malformed geometry raises
        ``InvalidGeometryError`` explicitly instead of being silently
        clamped or dropped.
        """
        if results is None:
            return []
        names = self._names
        if names is None:  # pragma: no cover - _load() always precedes
            raise InferenceError("detector is not loaded")
        metadata = detection_metadata(
            self._model_spec,
            input_width=self._config.input_width,
            input_height=self._config.input_height,
            device=self._device,
        )
        detections: list[DetectionObservation] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            xyxy = boxes.xyxy
            confs = boxes.conf
            cls_ids = boxes.cls
            for i in range(len(boxes)):
                row = xyxy[i]
                class_index = _scalar_int(cls_ids[i])
                if class_index < 0 or class_index >= len(names):
                    msg = f"model returned out-of-range class index {class_index}"
                    raise InferenceError(msg)
                x1 = float(row[0])
                y1 = float(row[1])
                x2 = float(row[2])
                y2 = float(row[3])
                # Geometry is validated for EVERY row before the
                # max_detections slice below: a malformed box must fail
                # the frame even if it would have been capped off.
                box = normalize_xyxy(x1, y1, x2, y2, width=width, height=height)
                detections.append(
                    DetectionObservation(
                        detection_id=DetectionId(new_uuid()),
                        frame_id=inp.frame.frame_id,
                        session_id=inp.frame.session_id,
                        source_ref=inp.frame.source_ref,
                        frame_index=inp.frame.frame_index,
                        class_name=names[class_index],
                        class_id=class_index,
                        confidence=_validated_confidence(confs[i]),
                        bounding_box=box,
                        event_time=inp.frame.event_time,
                        image_width=width,
                        image_height=height,
                        detector_metadata=metadata,
                    )
                )
        return detections[: self._config.max_detections]

    def _record(self, duration: float, detections: int, *, failed: bool) -> None:
        """Accumulate inference observability counters."""
        self._total_calls += 1
        self._total_duration += duration
        self._last_duration = duration
        if failed:
            self._total_failed_calls += 1
        else:
            self._total_detections += detections
            # Task 18.18 — detections produced at the detection boundary.
            record_pipeline_metric(PIPELINE_METRIC_DETECTIONS, detections)
