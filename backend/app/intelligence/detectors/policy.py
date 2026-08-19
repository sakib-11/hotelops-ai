"""Inference execution policy (Task 12, Phase 6).

A deterministic, explicit execution policy that sits ABOVE the
``ObjectDetector`` port and OWNS three concerns:

CPU/GPU
    Device selection is explicit (``DetectorConfig.device``), validated
    against actual availability at ``startup()``, resolved exactly once
    (deterministic single-shot — the selected device never changes),
    and exposed via ``selected_device``.  A requested-but-unavailable
    CUDA/MPS device fails startup loudly (``UnsupportedDeviceError``)
    unless the application explicitly enabled ``allow_cpu_fallback``.
    At execution time the policy verifies the detector is not running
    on a different device (device drift fails loudly).

Warmup
    ``run_warmup()`` ALWAYS invokes the detector's real ``warmup()``
    (never simulated), times it, records the duration, and raises a
    typed ``InferenceExecutionError`` on failure.

Batching
    Batch execution is bounded and explicit-only: ``batch_size`` is
    configured (1 disables batching; hard-capped by ``MAX_BATCH_SIZE``
    so memory is bounded), inputs are processed in chunks of at most
    ``batch_size``, and a batch is actually used ONLY when it is
    appropriate — batching is configured AND there is a batch AND the
    detector implements the ``BatchDetector`` capability.  Per-input
    provenance is preserved and verified for every result.

This module is SDK-free: device availability is probed through
importlib seams (or injected by the caller) and batching is a pure
orchestration concern.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Sequence
from typing import cast

from backend.app.intelligence.detectors.base import (
    DEFAULT_DETECTOR_CONFIG,
    BatchDetector,
    DetectionInput,
    DetectorConfig,
    ObjectDetector,
    resolve_device,
    validate_detection_provenance,
)
from backend.app.intelligence.detectors.exceptions import InferenceExecutionError
from contracts.vision import DetectionObservation

__all__ = ["InferenceExecutionPolicy"]


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


class InferenceExecutionPolicy:
    """Deterministic inference execution policy (device + warmup + batch)."""

    def __init__(self, *, config: DetectorConfig | None = None) -> None:
        self._config = config or DEFAULT_DETECTOR_CONFIG
        self._started = False
        self._selected_device: str | None = None
        self._warmup_duration: float | None = None

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def config(self) -> DetectorConfig:
        """The policy's inference configuration (explicit, immutable)."""
        return self._config

    @property
    def started(self) -> bool:
        """True once ``startup()`` completed successfully."""
        return self._started

    @property
    def selected_device(self) -> str | None:
        """The device selected at startup (None until started)."""
        return self._selected_device

    @property
    def warmup_duration_seconds(self) -> float | None:
        """Measured duration of the last ``run_warmup()`` (None before)."""
        return self._warmup_duration

    @property
    def batch_size(self) -> int:
        """The configured batch size (1 = batching disabled)."""
        return self._config.batch_size

    # ------------------------------------------------------------------
    # CPU/GPU startup — explicit, validated, deterministic
    # ------------------------------------------------------------------

    async def startup(
        self,
        *,
        cuda_available: bool | None = None,
        mps_available: bool | None = None,
    ) -> None:
        """Validate device availability and select the device ONCE.

        Deterministic single-shot: after a successful startup the
        selected device is fixed forever (re-calls are no-ops, so the
        device can never silently change).  Availability is probed via
        the importlib seams unless injected by the caller.
        """
        if self._started:
            return
        cuda = _cuda_available() if cuda_available is None else cuda_available
        mps = _mps_available() if mps_available is None else mps_available
        self._selected_device = resolve_device(
            self._config.device,
            allow_cpu_fallback=self._config.allow_cpu_fallback,
            cuda_available=cuda,
            mps_available=mps,
        )
        self._started = True

    async def shutdown(self) -> None:
        """Reset policy state (idempotent). A later ``startup()``
        deterministically re-selects the device."""
        self._started = False
        self._selected_device = None
        self._warmup_duration = None

    # ------------------------------------------------------------------
    # Warmup — explicit, measurable, never simulated
    # ------------------------------------------------------------------

    async def run_warmup(self, detector: ObjectDetector) -> None:
        """Run the detector's REAL warmup and measure its duration.

        The detector's ``warmup()`` is always invoked (never simulated
        or skipped) and timed; any failure raises
        ``InferenceExecutionError`` carrying the original cause.
        """
        self._require_started()
        self._assert_device_stable(detector)
        start = time.perf_counter()
        try:
            await detector.warmup()
        except Exception as exc:
            msg = "detector warmup failed"
            raise InferenceExecutionError(msg, cause=exc) from exc
        self._warmup_duration = time.perf_counter() - start

    # ------------------------------------------------------------------
    # Batching — bounded, explicit, provenance-preserving
    # ------------------------------------------------------------------

    def is_batching_appropriate(self, detector: ObjectDetector, input_count: int) -> bool:
        """Whether batching should be used for a group of inputs.

        Batching is used ONLY when all of: it is explicitly configured
        (``batch_size > 1``), there is an actual batch (``> 1`` inputs),
        and the detector implements the ``BatchDetector`` capability.
        It is never introduced simply because a framework supports it.
        """
        return (
            self._config.batch_size > 1 and input_count > 1 and isinstance(detector, BatchDetector)
        )

    async def execute(
        self,
        detector: ObjectDetector,
        inputs: Sequence[DetectionInput],
    ) -> list[list[DetectionObservation]]:
        """Execute inputs in bounded chunks preserving per-input provenance.

        Inputs are processed in chunks of at most ``batch_size`` (1 =
        strictly sequential, the default).  Batches are used only when
        appropriate (see :meth:`is_batching_appropriate`); otherwise the
        chunk is executed per frame.  Every result list is verified to
        belong to ITS input frame.  An empty input sequence returns an
        empty sequence.  Per-frame ``InferenceError`` failures propagate
        unchanged (non-fatal at the frame level); structural violations
        (device drift, wrong result count, broken provenance) raise
        ``InferenceExecutionError``.
        """
        self._require_started()
        self._assert_device_stable(detector)
        if not inputs:
            return []
        batch_size = self._config.batch_size
        results: list[list[DetectionObservation]] = []
        for start in range(0, len(inputs), batch_size):
            chunk = inputs[start : start + batch_size]
            if self.is_batching_appropriate(detector, len(chunk)):
                chunk_results = await cast(BatchDetector, detector).detect_batch(chunk)
            else:
                chunk_results = [await detector.detect(inp) for inp in chunk]
            if len(chunk_results) != len(chunk):
                msg = (
                    f"detector returned {len(chunk_results)} results for a {len(chunk)}-input batch"
                )
                raise InferenceExecutionError(msg)
            for inp, detections in zip(chunk, chunk_results, strict=True):
                try:
                    validate_detection_provenance(inp.frame, detections)
                except ValueError as exc:
                    msg = f"detector violated provenance for frame {inp.frame.frame_id}"
                    raise InferenceExecutionError(msg, cause=exc) from exc
            results.extend(chunk_results)
        return results

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _require_started(self) -> None:
        if not self._started:
            msg = "inference execution policy is not started; call startup() first"
            raise InferenceExecutionError(msg)

    def _assert_device_stable(self, detector: ObjectDetector) -> None:
        """Never silently change devices.

        If the detector exposes its resolved device and it differs from
        the policy-selected device, execution fails loudly — the policy
        refuses to run on a device it did not select at startup.
        """
        if not self._started:
            return
        reported = getattr(detector, "device", None)
        if reported is not None and reported != self._selected_device:
            msg = (
                f"detector device {reported!r} differs from policy-selected "
                f"device {self._selected_device!r}; refusing to run on a different device"
            )
            raise InferenceExecutionError(msg)
