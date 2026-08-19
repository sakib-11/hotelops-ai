"""Generic object-detection abstraction (Task 12, Phase 3).

- ``base`` — the stable ``ObjectDetector`` boundary: input/output
  types, inference configuration, model identity/provenance, and the
  canonical metadata + provenance helpers.
- ``exceptions`` — the typed error taxonomy downstream code depends
  on (provider errors never cross the boundary).
- ``fake`` — ``FakeDetector``, the deterministic mock/test detector
  proving the replacement guarantee.

Concrete detector backends implement ``ObjectDetector`` and are
confined behind this boundary: no detector SDK type is ever visible
here.
"""

from backend.app.intelligence.detectors.base import (
    DEFAULT_DETECTOR_CONFIG,
    MAX_BATCH_SIZE,
    BatchDetector,
    DetectionInput,
    DetectorConfig,
    Device,
    ModelSpec,
    ObjectDetector,
    detection_metadata,
    resolve_device,
    validate_detection_provenance,
)
from backend.app.intelligence.detectors.exceptions import (
    DetectionError,
    InferenceError,
    InferenceExecutionError,
    InvalidGeometryError,
    ModelArtifactCorruptError,
    ModelLoadError,
    ModelNotFoundError,
    ModelUnavailableError,
    ModelVersionNotFoundError,
    UnsupportedDeviceError,
)
from backend.app.intelligence.detectors.fake import FakeDetector
from backend.app.intelligence.detectors.normalize import (
    NORMALIZATION_EPSILON,
    normalize_xyxy,
    validate_bounding_box,
    validate_detections_geometry,
)
from backend.app.intelligence.detectors.policy import InferenceExecutionPolicy
from backend.app.intelligence.detectors.registry import (
    SUPPORTED_RUNTIMES,
    ModelDefinition,
    ModelLifecycleState,
    ModelRegistry,
)
from backend.app.intelligence.detectors.yolo_adapter import DetectionStats, YOLOv8Adapter

__all__ = [
    "DEFAULT_DETECTOR_CONFIG",
    "MAX_BATCH_SIZE",
    "NORMALIZATION_EPSILON",
    "SUPPORTED_RUNTIMES",
    "BatchDetector",
    "DetectionError",
    "DetectionInput",
    "DetectionStats",
    "DetectorConfig",
    "Device",
    "FakeDetector",
    "InferenceError",
    "InferenceExecutionError",
    "InferenceExecutionPolicy",
    "InvalidGeometryError",
    "ModelArtifactCorruptError",
    "ModelDefinition",
    "ModelLifecycleState",
    "ModelLoadError",
    "ModelNotFoundError",
    "ModelRegistry",
    "ModelSpec",
    "ModelUnavailableError",
    "ModelVersionNotFoundError",
    "ObjectDetector",
    "UnsupportedDeviceError",
    "YOLOv8Adapter",
    "detection_metadata",
    "normalize_xyxy",
    "resolve_device",
    "validate_bounding_box",
    "validate_detection_provenance",
    "validate_detections_geometry",
]
