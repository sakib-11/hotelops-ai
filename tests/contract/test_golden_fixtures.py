"""Golden fixture tests — load canonical JSON fixtures and validate them.

Golden fixtures are versioned in Git and serve as the compatibility baseline.
"""

from __future__ import annotations

import json
from pathlib import Path

from pytest import approx

from contracts.events import EventEnvelope
from contracts.intelligence import EvidencePackage, Recommendation
from contracts.operations import ActionCommand
from contracts.video import FramePacket
from contracts.vision import DetectionObservation, TrackObservation

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / name
    with open(path) as f:
        return json.load(f)


class TestGoldenFixtures:
    """Golden fixtures must deserialize correctly against canonical models."""

    def test_frame_packet_fixture(self) -> None:
        data = _load_fixture("frame_packet.json")
        packet = FramePacket.model_validate(data)
        assert packet.schema_version == "1.0"
        assert packet.frame_index == 0
        assert packet.width == 1920
        assert packet.height == 1080

    def test_detection_observation_fixture(self) -> None:
        data = _load_fixture("detection_observation.json")
        det = DetectionObservation.model_validate(data)
        assert det.class_name == "person"
        assert det.confidence == approx(0.95)
        assert det.bounding_box.x_min == approx(0.1)
        assert det.bounding_box.y_max == approx(0.8)

    def test_track_observation_fixture(self) -> None:
        data = _load_fixture("track_observation.json")
        track = TrackObservation.model_validate(data)
        assert track.track_state.value == "active"

    def test_event_envelope_fixture(self) -> None:
        data = _load_fixture("event_envelope.json")
        envelope = EventEnvelope[dict].model_validate(data)
        assert envelope.event_type == "detection.observed"
        assert envelope.payload["detection_count"] == 3

    def test_evidence_package_fixture(self) -> None:
        data = _load_fixture("evidence_package.json")
        pkg = EvidencePackage.model_validate(data)
        assert len(pkg.evidence_refs) == 1
        assert pkg.evidence_refs[0].ref_type.value == "frame"

    def test_recommendation_fixture(self) -> None:
        data = _load_fixture("recommendation.json")
        rec = Recommendation.model_validate(data)
        assert rec.priority.value == "high"
        assert len(rec.finding_ids) == 1

    def test_action_command_fixture(self) -> None:
        data = _load_fixture("action_command.json")
        cmd = ActionCommand.model_validate(data)
        assert cmd.command_type == "notify_staff"
        assert cmd.parameters["channel"] == "slack"

    def test_all_fixtures_deserialize(self) -> None:
        """Every JSON file in the fixtures directory deserializes without error."""
        fixture_files = sorted(FIXTURES_DIR.glob("*.json"))
        assert len(fixture_files) >= 7, f"Expected at least 7 fixtures, found {len(fixture_files)}"

        # Map of fixture file -> model class
        model_map: dict[str, type] = {
            "frame_packet.json": FramePacket,
            "detection_observation.json": DetectionObservation,
            "track_observation.json": TrackObservation,
            "event_envelope.json": EventEnvelope[dict],
            "evidence_package.json": EvidencePackage,
            "recommendation.json": Recommendation,
            "action_command.json": ActionCommand,
        }

        for fixture_path in fixture_files:
            if fixture_path.name == "__init__.py":
                continue
            model_class = model_map.get(fixture_path.name)
            if model_class is None:
                msg = f"No model class mapped for fixture: {fixture_path.name}"
                raise AssertionError(msg)
            data = json.loads(fixture_path.read_text())
            instance = model_class.model_validate(data)
            assert instance.schema_version == "1.0"

    def test_fixture_round_trip(self) -> None:
        """Serializing a deserialized fixture preserves JSON structure."""
        data = _load_fixture("frame_packet.json")
        packet = FramePacket.model_validate(data)
        serialized = packet.model_dump(mode="json")
        # Keys should match (ignoring order)
        assert set(serialized.keys()) == set(data.keys())
        assert serialized["frame_id"] == data["frame_id"]
        assert serialized["frame_index"] == data["frame_index"]
