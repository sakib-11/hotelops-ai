"""Generic object-detection abstraction (Task 12, Phase 3).

The stable application/domain boundary between video ingestion
(``contracts.video.FramePacket``, Task 11) and any concrete object
detection backend.  Downstream business logic depends ONLY on this
module and the canonical Task 4 observation contract
(``contracts.vision.DetectionObservation``) — never on a detector
SDK's types.

``ObjectDetector`` is deliberately SDK-free and replaceable: a
concrete detector backend, a mock detector, or a test detector can be
swapped in without touching downstream code.

Explicit contract behavior:

- Successful detection
    ``detect()`` returns one ``DetectionObservation`` per detected
    object.  Coordinates are normalized to [0, 1] relative to the
    original frame dimensions (``contracts.vision.BoundingBox``),
    confidence is bounded to [0, 1], and provenance (``frame_id``,
    ``event_time``) is copied verbatim from the consumed
    ``FramePacket`` — never fabricated.
- Empty detection
    ``detect()`` returns an empty list when nothing passes the
    configured confidence threshold (or the backend produces no
    detections).  An empty list is a valid, successful result — not
    an error.
- Invalid input
    ``DetectionInput`` validates at construction (non-empty image,
    sane dimensions).  Implementations must also reject malformed
    input with ``ValueError`` BEFORE any inference is attempted.
- Inference failure
    A runtime inference failure raises ``InferenceError`` (a
    ``DetectionError``).  It is non-fatal at the frame level: the
    caller counts the frame and continues.  Provider/SDK exceptions
    are attached as ``cause`` and never cross the boundary.
- Cancellation
    ``detect()``/``warmup()`` are cooperative cancellation points:
    ``asyncio.CancelledError`` propagates unchanged, no partial state
    is left behind, and the detector remains usable afterwards.
- Provenance
    Every emitted observation carries ``frame_id`` and ``event_time``
    copied from the input frame, plus canonical model identity in
    ``detector_metadata`` (see :func:`detection_metadata`).
    :func:`validate_detection_provenance` enforces the invariant.
- Metadata
    ``detector_metadata`` follows ONE schema (model id, model name,
    model version, artifact digest, device, input size) built by
    :func:`detection_metadata` so downstream consumers can rely on
    stable keys across all detector implementations.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from backend.app.intelligence.detectors.exceptions import UnsupportedDeviceError
from contracts.video import FramePacket
from contracts.vision import DetectionObservation

__all__ = [
    "DEFAULT_DETECTOR_CONFIG",
    "MAX_BATCH_SIZE",
    "BatchDetector",
    "DetectionInput",
    "DetectorConfig",
    "Device",
    "ModelSpec",
    "ObjectDetector",
    "detection_metadata",
    "resolve_device",
    "validate_detection_provenance",
]

_SHA256_HEX = "0123456789abcdef"

#: Hard cap on a single inference batch — memory is bounded by design.
MAX_BATCH_SIZE = 64


class Device(StrEnum):
    """Explicit inference device selection.

    ``AUTO`` delegates the choice to the concrete implementation at
    load time; ``CPU``, ``CUDA`` and ``MPS`` are explicit selections.
    An explicitly requested device that is unavailable on the host is
    a startup error (``UnsupportedDeviceError``), never a silent
    fallback.
    """

    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"
    AUTO = "auto"


def resolve_device(
    device: Device,
    *,
    allow_cpu_fallback: bool,
    cuda_available: bool,
    mps_available: bool,
) -> str:
    """Resolve the requested ``Device`` to an explicit runtime device string.

    ``AUTO`` is the application's explicit "pick the best available"
    choice: prefer CUDA, then MPS, then CPU.  An explicitly requested
    CUDA/MPS device that is unavailable is an error UNLESS
    ``allow_cpu_fallback`` is enabled by the application — the device
    never falls back silently.
    """
    if device is Device.AUTO:
        if cuda_available:
            return "cuda:0"
        if mps_available:
            return "mps"
        return "cpu"
    if device is Device.CPU:
        return "cpu"
    if device is Device.CUDA:
        if cuda_available:
            return "cuda:0"
        if allow_cpu_fallback:
            return "cpu"
        msg = (
            "CUDA requested but unavailable on this host "
            f"(allow_cpu_fallback={allow_cpu_fallback}); refusing to fall back silently"
        )
        raise UnsupportedDeviceError(msg)
    if device is Device.MPS:
        if mps_available:
            return "mps"
        if allow_cpu_fallback:
            return "cpu"
        msg = (
            "MPS requested but unavailable on this host "
            f"(allow_cpu_fallback={allow_cpu_fallback}); refusing to fall back silently"
        )
        raise UnsupportedDeviceError(msg)
    msg = f"unsupported device: {device!r}"
    raise UnsupportedDeviceError(msg)


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Inference configuration shared by all detector implementations.

    Provider-agnostic inference knobs.  Concrete backends translate
    these into their own SDK parameters; downstream code never sees
    SDK-specific settings.
    """

    confidence_threshold: float = 0.5
    nms_iou_threshold: float = 0.45
    max_detections: int = 300
    input_width: int = 640
    input_height: int = 640
    device: Device = Device.AUTO
    warmup_frames: int = 0
    half_precision: bool = False
    # Explicit GPU -> CPU fallback policy: when False (default), a
    # requested but unavailable CUDA/MPS device is a startup error.  Only
    # an application that explicitly configures this flag may fall back.
    allow_cpu_fallback: bool = False
    # Bounded inference batch size: 1 disables batching (per-frame
    # execution — the default).  Batching is used only when the
    # application explicitly configures it AND the detector supports it.
    batch_size: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.device, Device):
            msg = f"device must be a Device enum, got {self.device!r}"
            raise ValueError(msg)
        if not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            msg = f"batch_size must be in [1, {MAX_BATCH_SIZE}], got {self.batch_size}"
            raise ValueError(msg)
        if not 0.0 <= self.confidence_threshold <= 1.0:
            msg = f"confidence_threshold must be in [0, 1], got {self.confidence_threshold}"
            raise ValueError(msg)
        if not 0.0 <= self.nms_iou_threshold <= 1.0:
            msg = f"nms_iou_threshold must be in [0, 1], got {self.nms_iou_threshold}"
            raise ValueError(msg)
        if self.max_detections < 1:
            msg = f"max_detections must be >= 1, got {self.max_detections}"
            raise ValueError(msg)
        if self.input_width < 1 or self.input_height < 1:
            msg = (
                f"input_width/input_height must be >= 1, got {self.input_width}x{self.input_height}"
            )
            raise ValueError(msg)
        if self.warmup_frames < 0:
            msg = f"warmup_frames must be >= 0, got {self.warmup_frames}"
            raise ValueError(msg)


#: Sane defaults used when a caller supplies no per-frame override.
DEFAULT_DETECTOR_CONFIG = DetectorConfig()


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Immutable model identity + artifact provenance.

    Plain data — never a detector SDK type.  Every emitted
    ``DetectionObservation.detector_metadata`` is derived from this
    spec so model identity and artifact provenance are preserved end
    to end.  ``model_id`` is the stable governed identifier assigned by
    the model registry (Task 12 Step 5) and is carried on every
    observation so detections are traceable to the exact model.
    """

    model_id: str
    model_name: str
    model_version: str
    artifact_uri: str
    artifact_sha256: str
    device: Device
    class_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            msg = "model_id must be a non-empty string"
            raise ValueError(msg)
        if not self.model_name.strip():
            msg = "model_name must be a non-empty string"
            raise ValueError(msg)
        if not self.model_version.strip():
            msg = "model_version must be a non-empty string"
            raise ValueError(msg)
        if not self.artifact_uri.strip():
            msg = "artifact_uri must be a non-empty string"
            raise ValueError(msg)
        if not self.class_names:
            msg = "class_names must not be empty"
            raise ValueError(msg)
        if any(not name.strip() for name in self.class_names):
            msg = "class_names must not contain empty names"
            raise ValueError(msg)
        if len(set(self.class_names)) != len(self.class_names):
            msg = "class_names must be unique"
            raise ValueError(msg)
        digest = self.artifact_sha256
        if len(digest) != 64 or any(c not in _SHA256_HEX for c in digest):
            msg = "artifact_sha256 must be a 64-character lowercase hex SHA-256 digest"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DetectionInput:
    """One frame presented to a detector.

    Consumes the canonical Task 11 ``FramePacket`` as the identity,
    session and timing carrier (never re-serialized) plus the decoded
    pixel bytes delivered in-process by the ingestion layer
    (``FrameData.data``).  Byte decoding to an internal tensor format
    is the concrete detector's responsibility and stays inside it.
    """

    frame: FramePacket
    image: bytes
    width: int | None = None
    height: int | None = None
    config: DetectorConfig | None = None

    def __post_init__(self) -> None:
        if self.frame is None:
            msg = "frame is required"
            raise ValueError(msg)
        if not self.image:
            msg = "image must be non-empty"
            raise ValueError(msg)
        if self.width is not None and self.width < 1:
            msg = f"width must be >= 1 when present, got {self.width}"
            raise ValueError(msg)
        if self.height is not None and self.height < 1:
            msg = f"height must be >= 1 when present, got {self.height}"
            raise ValueError(msg)


@runtime_checkable
class ObjectDetector(Protocol):
    """The stable detector boundary.

    Implementations (a concrete backend, a mock, a test double) must:

    - return only ``DetectionObservation`` values built from the input
      frame (provenance invariant, see :func:`validate_detection_provenance`);
    - apply the canonical metadata schema (:func:`detection_metadata`);
    - raise ``InferenceError`` (never a provider exception) for runtime
      failures;
    - validate input and raise ``ValueError`` before inference;
    - propagate ``asyncio.CancelledError`` and stay reusable after
      cancellation;
    - expose their ``ModelSpec`` so identity/provenance is observable.
    """

    @property
    def model_spec(self) -> ModelSpec:
        """Identity + artifact provenance of the loaded model."""
        ...

    async def warmup(self) -> None:
        """Explicitly prime inference resources (e.g. GPU kernels).

        Called exactly once by the caller at startup; idempotent.
        """
        ...

    async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
        """Run detection on one frame.

        Returns a normalized, provenance-complete observation per
        detected object, or an empty list when nothing is detected.
        Raises ``InferenceError`` on runtime failure and
        ``ValueError`` on invalid input.
        """
        ...


@runtime_checkable
class BatchDetector(Protocol):
    """OPTIONAL detector capability: bounded batch inference.

    A detector implements this ONLY when it can genuinely process a
    bounded group of frames in one inference call (e.g. a backend with
    list-source prediction).  The execution policy uses it only when
    batching is explicitly configured, a batch is present, AND the
    detector is an instance of this protocol — batching is never
    assumed or automatic.

    Contract: returns exactly one result list per input, and every
    observation in each list preserves the provenance of ITS input
    frame.  An empty input sequence must return an empty sequence.
    """

    async def detect_batch(
        self, inputs: Sequence[DetectionInput]
    ) -> list[list[DetectionObservation]]:
        """Run bounded batched inference over ``inputs``."""
        ...


def detection_metadata(
    spec: ModelSpec,
    *,
    input_width: int,
    input_height: int,
    device: str | None = None,
) -> dict[str, Any]:
    """Build the canonical ``detector_metadata`` schema for one frame.

    Single source of truth for the metadata keys every implementation
    must attach to its observations: model identity (id + name),
    version, artifact digest, the device actually used for inference,
    and the input size.  ``device`` overrides the spec's declared
    device so adapters can record the RESOLVED runtime device (e.g.
    ``cuda:0`` from ``auto``).
    """
    return {
        "model_id": spec.model_id,
        "model": spec.model_name,
        "model_version": spec.model_version,
        "artifact_sha256": spec.artifact_sha256,
        "device": device if device is not None else spec.device.value,
        "input_width": input_width,
        "input_height": input_height,
    }


def validate_detection_provenance(
    frame: FramePacket, detections: Iterable[DetectionObservation]
) -> None:
    """Enforce the provenance invariant on detector output.

    Every observation must reference the exact ``FramePacket`` it was
    produced from (``frame_id`` and ``event_time`` copied verbatim).
    A violation is a programming error in the detector implementation
    and raises ``ValueError``.
    """
    for detection in detections:
        if detection.frame_id != frame.frame_id:
            msg = (
                f"detection {detection.detection_id} frame_id "
                f"{detection.frame_id} does not match input frame {frame.frame_id}"
            )
            raise ValueError(msg)
        if detection.event_time != frame.event_time:
            msg = (
                f"detection {detection.detection_id} event_time "
                f"{detection.event_time} does not match input frame {frame.event_time}"
            )
            raise ValueError(msg)
        if detection.session_id is not None and detection.session_id != frame.session_id:
            msg = (
                f"detection {detection.detection_id} session_id "
                f"{detection.session_id} does not match input frame {frame.session_id}"
            )
            raise ValueError(msg)
        if detection.source_ref is not None and detection.source_ref != frame.source_ref:
            msg = (
                f"detection {detection.detection_id} source_ref "
                f"{detection.source_ref} does not match input frame {frame.source_ref}"
            )
            raise ValueError(msg)
        if detection.frame_index is not None and detection.frame_index != frame.frame_index:
            msg = (
                f"detection {detection.detection_id} frame_index "
                f"{detection.frame_index} does not match input frame {frame.frame_index}"
            )
            raise ValueError(msg)
