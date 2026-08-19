"""Exception taxonomy for the generic object-detection boundary (Task 12).

Mirrors the project's provider-isolation convention (see
``backend/app/intelligence/sources/exceptions.py`` and
``backend/app/infrastructure/storage/exceptions.py``): downstream
business logic depends only on these types, never on a detector
SDK's error types.

Fatal vs non-fatal semantics:

- ``InferenceError`` is NON-fatal at the frame level — the caller
  counts the failed frame, skips it, and continues (same convention
  as ``FrameDecodeError`` in the Task 11 ingestion boundary).
- ``ModelNotFoundError`` / ``ModelVersionNotFoundError`` /
  ``ModelArtifactCorruptError`` / ``ModelLoadError`` /
  ``UnsupportedDeviceError`` are FATAL at load/startup time — a
  missing artifact or an unavailable device must fail fast before
  any frame is processed, never mid-stream.
"""

from __future__ import annotations


class DetectionError(Exception):
    """Base exception for all object-detection errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class InferenceError(DetectionError):
    """A single inference call failed at runtime.

    Non-fatal at the frame level: the caller counts the frame as
    failed and continues. Raised by detector implementations for any
    provider/SDK runtime failure — the provider exception is attached
    as ``cause`` and never allowed to cross the boundary unwrapped.
    """


class ModelNotFoundError(DetectionError):
    """The requested model name is not registered."""


class ModelVersionNotFoundError(DetectionError):
    """The requested model version is not available for the model."""


class ModelArtifactCorruptError(DetectionError):
    """The model artifact failed integrity verification (digest mismatch)."""


class ModelUnavailableError(DetectionError):
    """The model definition exists but is not usable.

    Raised by the model registry when a resolved model is DEPRECATED
    or DISABLED — the detector must refuse an invalid/deprecated model
    according to the governed lifecycle policy.
    """


class ModelLoadError(DetectionError):
    """The model artifact could not be loaded into an inference runtime."""


class UnsupportedDeviceError(DetectionError):
    """The requested inference device is not available on this host."""


class InvalidGeometryError(DetectionError):
    """A detection emitted malformed geometry.

    Raised explicitly by the normalization layer when a model output
    cannot be represented in the project's normalized coordinate
    system: coordinates outside [0, 1], inverted corners, or zero-size
    boxes.  Malformed model output is never silently hidden.
    """


class InferenceExecutionError(DetectionError):
    """A structural inference-execution violation.

    Fatal at the policy level: raised for device drift (a detector
    running on a device other than the policy-selected one), batch
    contract breaches (wrong result count / broken provenance), or
    warmup/startup failures.  Distinct from the per-frame
    ``InferenceError``, which remains non-fatal at the frame level.
    """


__all__ = [
    "DetectionError",
    "InferenceError",
    "InferenceExecutionError",
    "InvalidGeometryError",
    "ModelArtifactCorruptError",
    "ModelLoadError",
    "ModelNotFoundError",
    "ModelUnavailableError",
    "ModelVersionNotFoundError",
    "UnsupportedDeviceError",
]
