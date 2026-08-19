"""FakeDetector — deterministic, dependency-free ObjectDetector.

The canonical mock/test detector for the generic abstraction: it
implements the full ``ObjectDetector`` contract with no inference
framework, so unit tests, integration tests and local runs can drive
the entire downstream CV path deterministically.  It demonstrates the
replacement guarantee — a mock detector swaps in for a real backend
without any downstream change.

Behavior:

- emits a fixed number of detections per frame (``detections_per_frame``),
  with classes cycled from ``ModelSpec.class_names``;
- derives bounding boxes deterministically from the frame index so
  tests can assert exact outputs;
- honors the ``DetectorConfig`` supplied on the ``DetectionInput``:
  the confidence threshold filters emissions and ``max_detections``
  caps them;
- can be configured to raise ``InferenceError`` after a number of
  successful calls to exercise downstream failure handling
  (``fail_after_calls``; ``0`` fails on the very first call).
"""

from __future__ import annotations

from backend.app.intelligence.detectors.base import (
    DEFAULT_DETECTOR_CONFIG,
    DetectionInput,
    ModelSpec,
    ObjectDetector,
    detection_metadata,
)
from backend.app.intelligence.detectors.exceptions import InferenceError
from contracts.common import DetectionId, new_uuid
from contracts.vision import BoundingBox, DetectionObservation

__all__ = ["FakeDetector"]


class FakeDetector(ObjectDetector):
    """Deterministic in-memory detector implementing the ObjectDetector port."""

    def __init__(
        self,
        *,
        model_spec: ModelSpec,
        detections_per_frame: int = 1,
        confidence: float = 0.95,
        fail_after_calls: int | None = None,
    ) -> None:
        if detections_per_frame < 0:
            msg = f"detections_per_frame must be >= 0, got {detections_per_frame}"
            raise ValueError(msg)
        if not 0.0 <= confidence <= 1.0:
            msg = f"confidence must be in [0, 1], got {confidence}"
            raise ValueError(msg)
        if fail_after_calls is not None and fail_after_calls < 0:
            msg = f"fail_after_calls must be >= 0, got {fail_after_calls}"
            raise ValueError(msg)
        self._model_spec = model_spec
        self._detections_per_frame = detections_per_frame
        self._confidence = confidence
        self._fail_after_calls = fail_after_calls
        self._calls = 0

    @property
    def model_spec(self) -> ModelSpec:
        """The model identity this fake presents to downstream code."""
        return self._model_spec

    @property
    def calls(self) -> int:
        """Number of ``detect()`` invocations (warmup is not counted)."""
        return self._calls

    async def warmup(self) -> None:
        """No inference resources exist — nothing to prime (idempotent)."""
        return None

    async def detect(self, inp: DetectionInput) -> list[DetectionObservation]:
        """Emit deterministic detections for one frame per the contract."""
        self._calls += 1
        if self._fail_after_calls is not None and self._calls > self._fail_after_calls:
            raise InferenceError(
                f"fake detector failed on call {self._calls} "
                f"(fail_after_calls={self._fail_after_calls})"
            )
        config = inp.config or DEFAULT_DETECTOR_CONFIG
        if self._confidence < config.confidence_threshold:
            return []
        count = min(self._detections_per_frame, config.max_detections)
        if count == 0:
            return []
        class_names = self._model_spec.class_names
        frame_index = inp.frame.frame_index
        metadata = detection_metadata(
            self._model_spec,
            input_width=config.input_width,
            input_height=config.input_height,
        )
        detections: list[DetectionObservation] = []
        for i in range(count):
            # Deterministic, always-valid normalized box: x_min in
            # [0, 0.8), x_max = x_min + 0.1 <= 0.9 — inside [0, 1].
            x_min = round(((frame_index % 7) * 0.02 + i * 0.12) % 0.8, 6)
            y_min = round(((frame_index % 5) * 0.03 + i * 0.09) % 0.8, 6)
            detections.append(
                DetectionObservation(
                    detection_id=DetectionId(new_uuid()),
                    frame_id=inp.frame.frame_id,
                    session_id=inp.frame.session_id,
                    source_ref=inp.frame.source_ref,
                    frame_index=inp.frame.frame_index,
                    class_name=class_names[i % len(class_names)],
                    class_id=i % len(class_names),
                    confidence=self._confidence,
                    bounding_box=BoundingBox(
                        x_min=x_min,
                        y_min=y_min,
                        x_max=round(x_min + 0.1, 6),
                        y_max=round(y_min + 0.1, 6),
                    ),
                    event_time=inp.frame.event_time,
                    image_width=config.input_width,
                    image_height=config.input_height,
                    detector_metadata=metadata,
                )
            )
        return detections
