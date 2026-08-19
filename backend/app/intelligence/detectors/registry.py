"""Model artifact and version governance (Task 12, Step 5).

The governed source of model identity for object detection.  A small,
deterministic, in-memory registry — deliberately NOT an ML platform:
no training, no hosting, no automatic downloading, no approval
workflow.  It provides only the governance required to make detection
provenance reliable and production-auditable.

``ModelRegistry`` maps ``(model_id, model_version)`` pairs to an
immutable ``ModelDefinition`` (identity + artifact reference +
lifecycle state).  It is the intended production source of
``ModelSpec`` values: the ``YOLOv8Adapter`` consumes an approved model
definition and never invents identity, version, or artifact paths.

Lifecycle policy: only ``ACTIVE`` models resolve for use.  A model
that is ``DEPRECATED`` or ``DISABLED`` is refused with a typed
``ModelUnavailableError`` so an invalid/deprecated model can never be
loaded silently.

The registry is seeded from typed configuration
(``ModelRegistry.from_settings``) following the project's single
Settings convention — no hardcoded model paths or versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from backend.app.intelligence.detectors.base import Device, ModelSpec
from backend.app.intelligence.detectors.exceptions import (
    ModelNotFoundError,
    ModelUnavailableError,
    ModelVersionNotFoundError,
)

if TYPE_CHECKING:  # pragma: no cover - type-hint only, avoids import cycles
    from backend.app.infrastructure.config import Settings

__all__ = ["SUPPORTED_RUNTIMES", "ModelDefinition", "ModelLifecycleState", "ModelRegistry"]

_SHA256_HEX = "0123456789abcdef"

#: The inference runtimes the governance layer currently approves.  A
#: new detector backend extends this set together with its adapter —
#: governance and the adapter boundary change together.
SUPPORTED_RUNTIMES: frozenset[str] = frozenset({"ultralytics"})


class ModelLifecycleState(StrEnum):
    """The governed lifecycle state of a model definition.

    Only ``ACTIVE`` models may be used for inference; ``DEPRECATED``
    and ``DISABLED`` models are refused with a typed error.  These are
    the only states the project needs today.
    """

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    """Immutable, approved model identity + artifact reference.

    Fields:
        model_id: stable identifier used by business logic, analytics
            and evidence to trace detections.  It is carried on every
            emitted observation as
            ``DetectionObservation.detector_metadata["model_id"]``.
        model_name: model/architecture name (e.g. ``yolov8n``).
        model_version: explicit, deterministic, auditable version —
            never derived from filenames, timestamps, or directories.
        model_family: architecture family (e.g. ``yolov8``).
        runtime: inference runtime/framework; must be one of the
            approved ``SUPPORTED_RUNTIMES`` (unknown runtimes are
            rejected — an unapproved runtime must never be wired in
            silently).
        state: lifecycle state; only ACTIVE resolves for use.
        artifact_uri: explicitly configured, environment-independent
            artifact reference (no developer-specific paths).
        artifact_sha256: artifact checksum, verified before load for
            locally resolvable artifacts.
        class_names: the model's class table; must match the artifact.
        device: declared device preference (availability is validated
            at runtime by the inference execution policy).
    """

    model_id: str
    model_name: str
    model_version: str
    model_family: str
    runtime: str
    state: ModelLifecycleState
    artifact_uri: str
    artifact_sha256: str
    class_names: tuple[str, ...]
    device: Device = Device.AUTO

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
        if not self.model_family.strip():
            msg = "model_family must be a non-empty string"
            raise ValueError(msg)
        if not self.runtime.strip():
            msg = "runtime must be a non-empty string"
            raise ValueError(msg)
        if self.runtime not in SUPPORTED_RUNTIMES:
            supported = ", ".join(sorted(SUPPORTED_RUNTIMES))
            msg = f"unsupported runtime {self.runtime!r}; supported runtimes: {supported}"
            raise ValueError(msg)
        if not isinstance(self.state, ModelLifecycleState):
            msg = f"state must be a ModelLifecycleState, got {self.state!r}"
            raise ValueError(msg)
        if not self.artifact_uri.strip():
            msg = "artifact_uri must be a non-empty string"
            raise ValueError(msg)
        digest = self.artifact_sha256
        if len(digest) != 64 or any(c not in _SHA256_HEX for c in digest):
            msg = "artifact_sha256 must be a 64-character lowercase hex SHA-256 digest"
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
        if not isinstance(self.device, Device):
            msg = f"device must be a Device enum, got {self.device!r}"
            raise ValueError(msg)

    def to_model_spec(self) -> ModelSpec:
        """Derive the ``ModelSpec`` consumed by the detector boundary.

        The adapter consumes this spec; it never constructs model
        identity itself.
        """
        return ModelSpec(
            model_id=self.model_id,
            model_name=self.model_name,
            model_version=self.model_version,
            artifact_uri=self.artifact_uri,
            artifact_sha256=self.artifact_sha256,
            device=self.device,
            class_names=self.class_names,
        )


class ModelRegistry:
    """Deterministic in-memory registry of approved model definitions.

    ``(model_id, model_version)`` pairs are unique; re-registering an
    existing pair is an error.  Resolution is strict and typed:

    - unknown ``model_id``          → ``ModelNotFoundError``
    - unknown ``model_version``     → ``ModelVersionNotFoundError``
    - ambiguous version-less lookup → ``ModelVersionNotFoundError``
    - non-ACTIVE model              → ``ModelUnavailableError``
      (unless ``require_active=False``)

    No hidden global state: a registry is an explicit instance owned by
    the application wiring.
    """

    def __init__(self, *, default_model_id: str | None = None) -> None:
        self._models: dict[str, dict[str, ModelDefinition]] = {}
        self._default_model_id = default_model_id

    def register(self, definition: ModelDefinition) -> None:
        """Register one approved model definition.

        Raises:
            ValueError: the ``(model_id, model_version)`` pair is
                already registered (deterministic, never silent).
        """
        versions = self._models.setdefault(definition.model_id, {})
        if definition.model_version in versions:
            msg = (
                f"model {definition.model_id!r} version "
                f"{definition.model_version!r} is already registered"
            )
            raise ValueError(msg)
        versions[definition.model_version] = definition

    def resolve(
        self,
        model_id: str,
        version: str | None = None,
        *,
        require_active: bool = True,
    ) -> ModelDefinition:
        """Resolve an approved model definition for use.

        With ``version=None`` the lookup succeeds only when exactly one
        version is registered (deterministic).  A DEPRECATED or
        DISABLED model is refused unless ``require_active=False``.
        """
        versions = self._models.get(model_id)
        if not versions:
            msg = f"no model registered with id {model_id!r}"
            raise ModelNotFoundError(msg)
        if version is None:
            if len(versions) == 1:
                definition = next(iter(versions.values()))
            else:
                available = sorted(versions)
                msg = (
                    f"model {model_id!r} has multiple versions ({available}); a version is required"
                )
                raise ModelVersionNotFoundError(msg)
        else:
            candidate = versions.get(version)
            if candidate is None:
                available = sorted(versions)
                msg = (
                    f"model {model_id!r} has no version {version!r}; "
                    f"available versions: {available}"
                )
                raise ModelVersionNotFoundError(msg)
            definition = candidate
        if require_active and definition.state is not ModelLifecycleState.ACTIVE:
            msg = (
                f"model {model_id!r}@{definition.model_version} is "
                f"{definition.state.value}; only ACTIVE models may be used"
            )
            raise ModelUnavailableError(msg)
        return definition

    def default(self) -> ModelDefinition:
        """Resolve the registry's configured default model."""
        if self._default_model_id is None:
            msg = "this registry has no default model id configured"
            raise ModelNotFoundError(msg)
        return self.resolve(self._default_model_id)

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelRegistry:
        """Build a registry seeded from the typed ``DETECTION_*`` config.

        The single configured default model is registered as ACTIVE and
        becomes the registry's default.  No model paths or versions are
        hardcoded here — they come from Settings.
        """
        definition = ModelDefinition(
            model_id=settings.detection_model_id,
            model_name=settings.detection_model_name,
            model_version=settings.detection_model_version,
            model_family=settings.detection_model_family,
            runtime=settings.detection_runtime,
            state=ModelLifecycleState.ACTIVE,
            artifact_uri=settings.detection_artifact_uri,
            artifact_sha256=settings.detection_artifact_sha256,
            class_names=tuple(
                name.strip() for name in settings.detection_class_names.split(",") if name.strip()
            ),
            device=Device(settings.detection_device),
        )
        registry = cls(default_model_id=definition.model_id)
        registry.register(definition)
        return registry
