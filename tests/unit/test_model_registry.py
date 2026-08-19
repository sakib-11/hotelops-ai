"""Model artifact/version governance tests (Task 12, Step 5).

Covers the governed model identity layer that the YOLOv8Adapter
consumes:

UNIT
- ``ModelDefinition`` validation and ``to_model_spec`` round-trip
- ``ModelRegistry`` register/resolve semantics (typed failures:
  ModelNotFoundError, ModelVersionNotFoundError, ModelUnavailableError)
- settings seeding (``ModelRegistry.from_settings``)

INTEGRATION
- registry → ModelSpec → YOLOv8Adapter (fake SDK seam): detection
  provenance carries model_id/model_version/artifact identity
- artifact checksum validation before load: correct → load,
  mismatch → ModelArtifactCorruptError (SDK never constructed),
  missing local artifact → ModelLoadError, non-local URI → policy skip

No real model and no personal GPU are required: the SDK is a
deterministic seam and device availability is injected.
"""

from __future__ import annotations

import hashlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from backend.app.infrastructure.config import Settings
from backend.app.intelligence.detectors import (
    DetectionInput,
    DetectorConfig,
    Device,
    ModelArtifactCorruptError,
    ModelDefinition,
    ModelLifecycleState,
    ModelLoadError,
    ModelNotFoundError,
    ModelRegistry,
    ModelSpec,
    ModelUnavailableError,
    ModelVersionNotFoundError,
    yolo_adapter,
)
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from contracts.common import FrameId, VideoSessionId, new_uuid, utc_now
from contracts.video import FramePacket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_definition(**overrides: Any) -> ModelDefinition:
    """A valid ACTIVE model definition (overridable per test)."""
    base: dict[str, Any] = {
        "model_id": "yolo-person-detector",
        "model_name": "yolov8n",
        "model_version": "1.0.0",
        "model_family": "yolov8",
        "runtime": "ultralytics",
        "state": ModelLifecycleState.ACTIVE,
        "artifact_uri": "memory://governed/yolov8n.pt",
        "artifact_sha256": "a" * 64,
        "class_names": ("person", "bag"),
        "device": Device.CPU,
    }
    base.update(overrides)
    return ModelDefinition(**base)


def make_registry(*definitions: ModelDefinition) -> ModelRegistry:
    registry = ModelRegistry(default_model_id="yolo-person-detector")
    for definition in definitions:
        registry.register(definition)
    return registry


def make_frame(*, frame_index: int = 0) -> FramePacket:
    return FramePacket(
        frame_id=FrameId(new_uuid()),
        session_id=VideoSessionId(new_uuid()),
        frame_index=frame_index,
        event_time=utc_now(),
        width=640,
        height=480,
    )


def make_config() -> DetectorConfig:
    return DetectorConfig(device=Device.CPU)


def make_input(frame: FramePacket) -> DetectionInput:
    return DetectionInput(frame=frame, image=b"\xff\xd8jpg", width=640, height=480)


# ---------------------------------------------------------------------------
# ModelDefinition validation
# ---------------------------------------------------------------------------


class TestModelDefinitionValidation:
    def test_valid_definition_accepted(self) -> None:
        definition = make_definition()
        assert definition.state is ModelLifecycleState.ACTIVE

    @pytest.mark.parametrize(
        "field",
        ["model_id", "model_name", "model_version", "model_family", "runtime", "artifact_uri"],
    )
    def test_empty_identity_field_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            make_definition(**{field: "  "})

    @pytest.mark.parametrize("digest", ["abc", "0" * 63, "g" + "0" * 63])
    def test_invalid_checksum_rejected(self, digest: str) -> None:
        with pytest.raises(ValueError, match="sha256"):
            make_definition(artifact_sha256=digest)

    def test_empty_class_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="class_names"):
            make_definition(class_names=())

    def test_duplicate_class_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            make_definition(class_names=("person", "person"))

    def test_invalid_device_rejected(self) -> None:
        with pytest.raises(ValueError, match="device"):
            make_definition(device="gpu")  # type: ignore[arg-type]

    def test_invalid_state_rejected(self) -> None:
        with pytest.raises(ValueError, match="state"):
            make_definition(state="active")  # type: ignore[arg-type]

    def test_unsupported_runtime_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported runtime"):
            make_definition(runtime="tensorrt")

    def test_to_model_spec_round_trip(self) -> None:
        definition = make_definition()
        spec = definition.to_model_spec()
        assert isinstance(spec, ModelSpec)
        assert spec.model_id == definition.model_id
        assert spec.model_name == definition.model_name
        assert spec.model_version == definition.model_version
        assert spec.artifact_uri == definition.artifact_uri
        assert spec.artifact_sha256 == definition.artifact_sha256
        assert spec.device == definition.device
        assert spec.class_names == definition.class_names


# ---------------------------------------------------------------------------
# ModelRegistry semantics
# ---------------------------------------------------------------------------


class TestModelRegistry:
    def test_resolve_by_id_and_version(self) -> None:
        registry = make_registry(make_definition())
        resolved = registry.resolve("yolo-person-detector", "1.0.0")
        assert resolved.model_id == "yolo-person-detector"
        assert resolved.model_version == "1.0.0"

    def test_resolve_without_version_when_single_version(self) -> None:
        registry = make_registry(make_definition())
        assert registry.resolve("yolo-person-detector").model_version == "1.0.0"

    def test_ambiguous_version_less_resolution_is_typed(self) -> None:
        registry = make_registry(
            make_definition(model_version="1.0.0"),
            make_definition(model_version="2.0.0"),
        )
        with pytest.raises(ModelVersionNotFoundError, match="version is required"):
            registry.resolve("yolo-person-detector")

    def test_unknown_model_id_raises_not_found(self) -> None:
        registry = make_registry(make_definition())
        with pytest.raises(ModelNotFoundError, match="no model registered"):
            registry.resolve("unknown-model")

    def test_unknown_version_raises_version_not_found(self) -> None:
        registry = make_registry(make_definition())
        with pytest.raises(ModelVersionNotFoundError, match="no version"):
            registry.resolve("yolo-person-detector", "9.9.9")

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = make_registry(make_definition())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(make_definition())

    def test_deprecated_model_is_refused(self) -> None:
        registry = make_registry(
            make_definition(state=ModelLifecycleState.DEPRECATED, model_version="1.0.0")
        )
        with pytest.raises(ModelUnavailableError, match="deprecated"):
            registry.resolve("yolo-person-detector")

    def test_disabled_model_is_refused(self) -> None:
        registry = make_registry(
            make_definition(state=ModelLifecycleState.DISABLED, model_version="1.0.0")
        )
        with pytest.raises(ModelUnavailableError, match="disabled"):
            registry.resolve("yolo-person-detector")

    def test_deprecated_resolvable_when_explicitly_allowed(self) -> None:
        registry = make_registry(
            make_definition(state=ModelLifecycleState.DEPRECATED, model_version="1.0.0")
        )
        resolved = registry.resolve("yolo-person-detector", require_active=False)
        assert resolved.state is ModelLifecycleState.DEPRECATED

    def test_disabled_resolvable_when_explicitly_allowed(self) -> None:
        registry = make_registry(
            make_definition(state=ModelLifecycleState.DISABLED, model_version="1.0.0")
        )
        resolved = registry.resolve("yolo-person-detector", require_active=False)
        assert resolved.state is ModelLifecycleState.DISABLED

    def test_default_resolves_configured_model(self) -> None:
        registry = make_registry(make_definition())
        assert registry.default().model_id == "yolo-person-detector"

    def test_default_without_configured_id_is_typed(self) -> None:
        registry = ModelRegistry()
        registry.register(make_definition())
        with pytest.raises(ModelNotFoundError, match="no default"):
            registry.default()


# ---------------------------------------------------------------------------
# Settings seeding
# ---------------------------------------------------------------------------


class TestSettingsSeeding:
    def test_from_settings_seeds_active_default(self) -> None:
        settings = Settings(_env_file=None)  # default DETECTION_* values
        registry = ModelRegistry.from_settings(settings)
        definition = registry.default()
        assert definition.model_id == settings.detection_model_id
        assert definition.model_name == settings.detection_model_name
        assert definition.model_version == settings.detection_model_version
        assert definition.model_family == settings.detection_model_family
        assert definition.runtime == settings.detection_runtime
        assert definition.artifact_uri == settings.detection_artifact_uri
        assert definition.artifact_sha256 == settings.detection_artifact_sha256
        assert definition.class_names == ("person", "bag")
        assert definition.state is ModelLifecycleState.ACTIVE
        # The seeded spec is immediately usable by the detector boundary.
        assert isinstance(definition.to_model_spec(), ModelSpec)

    def test_from_settings_invalid_device_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DETECTION_DEVICE", "quantum")
        settings = Settings(_env_file=None)
        with pytest.raises(ValueError, match="quantum"):
            ModelRegistry.from_settings(settings)


# ---------------------------------------------------------------------------
# Fake SDK seam (deterministic; no real model, no GPU)
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


class _FakeYOLO:
    """Deterministic stand-in for ``ultralytics.YOLO``."""

    instances: ClassVar[list[_FakeYOLO]] = []

    def __init__(self, artifact_uri: str) -> None:
        self.artifact_uri = artifact_uri
        self.names: dict[int, str] = {0: "person", 1: "bag"}
        _FakeYOLO.instances.append(self)

    def predict(self, **kwargs: Any) -> Any:
        return [_FakeResult(_FakeBoxes([[10.0, 20.0, 300.0, 400.0]], [[0.9]], [[0]]))]


def install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake SDK + deterministic decode/device seams."""
    _FakeYOLO.instances = []
    module = types.ModuleType("ultralytics")
    module.YOLO = _FakeYOLO  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    monkeypatch.setattr(yolo_adapter, "_cuda_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_mps_available", lambda: False)
    monkeypatch.setattr(yolo_adapter, "_decode_image_bytes", lambda image: (object(), 640, 480))


# ---------------------------------------------------------------------------
# Adapter integration: registry -> ModelSpec -> YOLOv8Adapter
# ---------------------------------------------------------------------------


class TestAdapterGovernanceIntegration:
    async def test_governed_spec_drives_detection_provenance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_sdk(monkeypatch)
        registry = make_registry(make_definition())
        spec = registry.resolve("yolo-person-detector", "1.0.0").to_model_spec()
        adapter = YOLOv8Adapter(model_spec=spec, config=make_config())
        frame = make_frame()
        detections = await adapter.detect(make_input(frame))
        assert len(detections) == 1
        det = detections[0]
        # Provenance: governed model identity available after inference.
        assert det.detector_metadata is not None
        assert det.detector_metadata["model_id"] == "yolo-person-detector"
        assert det.detector_metadata["model_version"] == "1.0.0"
        assert det.detector_metadata["artifact_sha256"] == "a" * 64
        assert det.detector_metadata["device"] == "cpu"
        # Frame provenance copied verbatim.
        assert det.frame_id == frame.frame_id
        assert det.event_time == frame.event_time

    async def test_deprecated_model_never_reaches_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_sdk(monkeypatch)
        registry = make_registry(
            make_definition(state=ModelLifecycleState.DEPRECATED, model_version="1.0.0")
        )
        with pytest.raises(ModelUnavailableError):
            registry.resolve("yolo-person-detector")
        # The SDK was never constructed — the governance refusal happens
        # before the adapter is even built.
        assert _FakeYOLO.instances == []


# ---------------------------------------------------------------------------
# Artifact checksum validation before load
# ---------------------------------------------------------------------------


def _write_artifact(tmp_path: Path, payload: bytes) -> str:
    path = tmp_path / "model.pt"
    path.write_bytes(payload)
    return str(path)


class TestChecksumVerification:
    async def test_correct_checksum_loads_and_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_fake_sdk(monkeypatch)
        payload = b"approved-model-bytes"
        uri = _write_artifact(tmp_path, payload)
        digest = hashlib.sha256(payload).hexdigest()
        spec = make_definition(artifact_uri=uri, artifact_sha256=digest).to_model_spec()
        adapter = YOLOv8Adapter(model_spec=spec, config=make_config())
        detections = await adapter.detect(make_input(make_frame()))
        assert len(detections) == 1
        assert _FakeYOLO.instances[0].artifact_uri == uri

    async def test_checksum_mismatch_fails_before_sdk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_fake_sdk(monkeypatch)
        payload = b"approved-model-bytes"
        uri = _write_artifact(tmp_path, payload)
        wrong = hashlib.sha256(b"other-bytes").hexdigest()
        spec = make_definition(artifact_uri=uri, artifact_sha256=wrong).to_model_spec()
        adapter = YOLOv8Adapter(model_spec=spec, config=make_config())
        with pytest.raises(ModelArtifactCorruptError, match="checksum mismatch"):
            await adapter.detect(make_input(make_frame()))
        # Fail-fast: the SDK was never even constructed.
        assert _FakeYOLO.instances == []

    async def test_missing_local_artifact_is_typed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_fake_sdk(monkeypatch)
        spec = make_definition(
            artifact_uri=str(tmp_path / "does-not-exist.pt"), artifact_sha256="a" * 64
        ).to_model_spec()
        adapter = YOLOv8Adapter(model_spec=spec, config=make_config())
        with pytest.raises(ModelLoadError, match="not found"):
            await adapter.detect(make_input(make_frame()))

    async def test_non_local_uri_follows_policy_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fake_sdk(monkeypatch)
        # memory:// artifacts cannot be verified locally — per project
        # policy the checksum remains provenance and loading proceeds.
        spec = make_definition().to_model_spec()
        adapter = YOLOv8Adapter(model_spec=spec, config=make_config())
        detections = await adapter.detect(make_input(make_frame()))
        assert len(detections) == 1
