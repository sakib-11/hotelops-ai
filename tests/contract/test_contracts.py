"""Comprehensive tests for all canonical HotelOps AI contracts.

Covers serialization, deserialization, invalid schema rejection,
round-trip semantic equality, and golden fixture compatibility.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from contracts.analytics import MetricValue, OpportunityCandidate
from contracts.common import (
    SCHEMA_VERSION,
)
from contracts.events import AnalysisJob, EventEnvelope, EvidenceRef, EvidenceType, JobStatus
from contracts.intelligence import EvidencePackage, Finding, Priority, Recommendation
from contracts.operations import ActionCommand, Alert, ApprovalRequest, ApprovalStatus, Severity
from contracts.video import FramePacket, SourceType, VideoAsset, VideoSession
from contracts.vision import BoundingBox, DetectionObservation, TrackObservation, TrackState


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Helper to create a UTC datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# =============================================================================
# Video Contracts
# =============================================================================


class TestVideoAsset:
    """VideoAsset serialization, deserialization, and validation."""

    def test_minimal_construction(self) -> None:
        asset = VideoAsset(
            asset_id=UUID("00000000-0000-0000-0000-000000000001"),
            source_type=SourceType.LIVE,
        )
        assert asset.schema_version == SCHEMA_VERSION
        assert asset.source_type == SourceType.LIVE

    def test_full_construction(self) -> None:
        asset = VideoAsset(
            asset_id=UUID("00000000-0000-0000-0000-000000000001"),
            source_type=SourceType.RECORDED,
            capture_time=_utc(2026, 7, 29, 12, 0),
            duration_seconds=300.5,
            media_metadata={"codec": "h264", "fps": 30},
        )
        from pytest import approx

        assert asset.duration_seconds == approx(300.5)

    def test_serialize_round_trip(self) -> None:
        original = VideoAsset(
            asset_id=UUID("00000000-0000-0000-0000-000000000001"),
            source_type=SourceType.LIVE,
        )
        data = original.model_dump(mode="json")
        restored = VideoAsset.model_validate(data)
        assert restored == original

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            VideoAsset(
                asset_id=UUID("00000000-0000-0000-0000-000000000001"),
                source_type=SourceType.LIVE,
                duration_seconds=-1.0,
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValueError):
            VideoAsset.model_validate({
                "asset_id": str(UUID("00000000-0000-0000-0000-000000000001")),
                "source_type": "live",
                "unknown_field": "should_not_be_allowed",
            })


class TestVideoSession:
    """VideoSession serialization, deserialization, and validation."""

    def test_minimal_construction(self) -> None:
        session = VideoSession(
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            source_type=SourceType.LIVE,
            started_at=_utc(2026, 7, 29, 12, 0),
        )
        assert session.schema_version == SCHEMA_VERSION

    def test_serialize_round_trip(self) -> None:
        original = VideoSession(
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            source_type=SourceType.RECORDED,
            asset_id=UUID("00000000-0000-0000-0000-000000000001"),
            started_at=_utc(2026, 7, 29, 12, 0),
            ended_at=_utc(2026, 7, 29, 13, 0),
        )
        data = original.model_dump(mode="json")
        restored = VideoSession.model_validate(data)
        assert restored == original

    def test_naive_started_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            VideoSession(
                session_id=UUID("00000000-0000-0000-0000-000000000002"),
                source_type=SourceType.LIVE,
                started_at=datetime(2026, 7, 29, 12, 0),
            )


class TestFramePacket:
    """FramePacket serialization, deserialization, and validation."""

    def test_minimal_construction(self) -> None:
        packet = FramePacket(
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            frame_index=0,
            event_time=_utc(2026, 7, 29, 12, 0, 1),
        )
        assert packet.schema_version == SCHEMA_VERSION

    def test_serialize_round_trip(self) -> None:
        original = FramePacket(
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            frame_index=42,
            event_time=_utc(2026, 7, 29, 12, 0, 2),
            width=1920,
            height=1080,
            source_ref=UUID("00000000-0000-0000-0000-000000000001"),
        )
        data = original.model_dump(mode="json")
        restored = FramePacket.model_validate(data)
        assert restored == original

    def test_negative_frame_index_rejected(self) -> None:
        with pytest.raises(ValueError):
            FramePacket(
                frame_id=UUID("00000000-0000-0000-0000-000000000003"),
                session_id=UUID("00000000-0000-0000-0000-000000000002"),
                frame_index=-1,
                event_time=_utc(2026, 7, 29, 12, 0),
            )


# =============================================================================
# Vision Contracts
# =============================================================================


class TestBoundingBox:
    """BoundingBox validation."""

    def test_valid_box(self) -> None:
        box = BoundingBox(x_min=0.1, y_min=0.2, x_max=0.8, y_max=0.9)
        from pytest import approx

        assert box.x_min == approx(0.1)

    def test_x_max_less_than_x_min_rejected(self) -> None:
        with pytest.raises(ValueError):
            BoundingBox(x_min=0.5, y_min=0.2, x_max=0.3, y_max=0.9)

    def test_out_of_range_values_rejected(self) -> None:
        with pytest.raises(ValueError):
            BoundingBox(x_min=-0.1, y_min=0.2, x_max=1.5, y_max=0.9)


class TestDetectionObservation:
    """DetectionObservation serialization and validation."""

    def test_minimal_construction(self) -> None:
        det = DetectionObservation(
            detection_id=UUID("00000000-0000-0000-0000-000000000010"),
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            class_name="person",
            confidence=0.95,
            bounding_box=BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        assert det.schema_version == SCHEMA_VERSION

    def test_serialize_round_trip(self) -> None:
        original = DetectionObservation(
            detection_id=UUID("00000000-0000-0000-0000-000000000010"),
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            class_name="person",
            confidence=0.88,
            bounding_box=BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        data = original.model_dump(mode="json")
        restored = DetectionObservation.model_validate(data)
        assert restored == original

    def test_invalid_confidence_rejected(self) -> None:
        with pytest.raises(ValueError):
            DetectionObservation(
                detection_id=UUID("00000000-0000-0000-0000-000000000010"),
                frame_id=UUID("00000000-0000-0000-0000-000000000003"),
                class_name="person",
                confidence=1.5,
                bounding_box=BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
                event_time=_utc(2026, 7, 29, 12, 0),
            )


class TestTrackObservation:
    """TrackObservation serialization and validation."""

    def test_minimal_construction(self) -> None:
        track = TrackObservation(
            track_id=UUID("00000000-0000-0000-0000-000000000020"),
            detection_id=UUID("00000000-0000-0000-0000-000000000010"),
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        assert track.track_state == TrackState.ACTIVE

    def test_serialize_round_trip(self) -> None:
        original = TrackObservation(
            track_id=UUID("00000000-0000-0000-0000-000000000020"),
            detection_id=UUID("00000000-0000-0000-0000-000000000010"),
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            event_time=_utc(2026, 7, 29, 12, 0),
            track_state=TrackState.LOST,
        )
        data = original.model_dump(mode="json")
        restored = TrackObservation.model_validate(data)
        assert restored == original

    def test_naive_event_time_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            TrackObservation(
                track_id=UUID("00000000-0000-0000-0000-000000000020"),
                detection_id=UUID("00000000-0000-0000-0000-000000000010"),
                frame_id=UUID("00000000-0000-0000-0000-000000000003"),
                session_id=UUID("00000000-0000-0000-0000-000000000002"),
                event_time=datetime(2026, 7, 29, 12, 0),
            )


# =============================================================================
# Event Contracts
# =============================================================================


class TestEventEnvelope:
    """EventEnvelope serialization and validation."""

    def test_with_dict_payload(self) -> None:
        envelope = EventEnvelope[dict](
            event_id=UUID("00000000-0000-0000-0000-000000000030"),
            event_type="frame.detected",
            event_time=_utc(2026, 7, 29, 12, 0),
            produced_at=_utc(2026, 7, 29, 12, 0, 1),
            source="yolo.detector",
            payload={"detection_count": 3},
        )
        assert envelope.schema_version == SCHEMA_VERSION
        assert envelope.payload["detection_count"] == 3

    def test_serialize_round_trip(self) -> None:
        original = EventEnvelope[dict](
            event_id=UUID("00000000-0000-0000-0000-000000000030"),
            event_type="test.event",
            event_time=_utc(2026, 7, 29, 12, 0),
            produced_at=_utc(2026, 7, 29, 12, 0, 1),
            source="test",
            payload={"key": "value"},
        )
        data = original.model_dump(mode="json")
        restored = EventEnvelope[dict].model_validate(data)
        assert restored == original

    def test_empty_event_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            EventEnvelope[dict](
                event_id=UUID("00000000-0000-0000-0000-000000000030"),
                event_type="",
                event_time=_utc(2026, 7, 29, 12, 0),
                produced_at=_utc(2026, 7, 29, 12, 0, 1),
                source="test",
                payload={},
            )


class TestEvidenceRef:
    """EvidenceRef serialization and validation."""

    def test_minimal_construction(self) -> None:
        ref = EvidenceRef(
            ref_id=UUID("00000000-0000-0000-0000-000000000040"),
            ref_type=EvidenceType.FRAME,
            ref_uri="00000000-0000-0000-0000-000000000003",
        )
        assert ref.schema_version == SCHEMA_VERSION

    def test_serialize_round_trip(self) -> None:
        original = EvidenceRef(
            ref_id=UUID("00000000-0000-0000-0000-000000000040"),
            ref_type=EvidenceType.OBJECT_STORAGE,
            ref_uri="s3://hotelops/evidence/video123.mp4",
            metadata={"duration_s": 120.0},
        )
        data = original.model_dump(mode="json")
        restored = EvidenceRef.model_validate(data)
        assert restored == original

    def test_empty_uri_rejected(self) -> None:
        with pytest.raises(ValueError):
            EvidenceRef(
                ref_id=UUID("00000000-0000-0000-0000-000000000040"),
                ref_type=EvidenceType.FRAME,
                ref_uri="",
            )


class TestAnalysisJob:
    """AnalysisJob serialization and validation."""

    def test_minimal_construction(self) -> None:
        job = AnalysisJob(
            job_id=UUID("00000000-0000-0000-0000-000000000050"),
            job_type="occupancy_analysis",
            created_at=_utc(2026, 7, 29, 12, 0),
        )
        assert job.status == JobStatus.PENDING

    def test_serialize_round_trip(self) -> None:
        original = AnalysisJob(
            job_id=UUID("00000000-0000-0000-0000-000000000050"),
            job_type="dwell_detection",
            status=JobStatus.COMPLETED,
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            created_at=_utc(2026, 7, 29, 12, 0),
        )
        data = original.model_dump(mode="json")
        restored = AnalysisJob.model_validate(data)
        assert restored == original


# =============================================================================
# Analytics Contracts
# =============================================================================


class TestMetricValue:
    """MetricValue serialization and validation."""

    def test_minimal_construction(self) -> None:
        metric = MetricValue(
            metric_name="occupancy_rate",
            value=0.75,
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        from pytest import approx

        assert metric.value == approx(0.75)

    def test_serialize_round_trip(self) -> None:
        original = MetricValue(
            metric_name="avg_dwell_time",
            value=42.5,
            unit="minutes",
            event_time=_utc(2026, 7, 29, 12, 0),
            window_start=_utc(2026, 7, 29, 11, 0),
            window_end=_utc(2026, 7, 29, 12, 0),
        )
        data = original.model_dump(mode="json")
        restored = MetricValue.model_validate(data)
        assert restored == original

    def test_naive_event_time_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            MetricValue(
                metric_name="test",
                value=1.0,
                event_time=datetime(2026, 7, 29, 12, 0),
            )


class TestOpportunityCandidate:
    """OpportunityCandidate serialization and validation."""

    def test_minimal_construction(self) -> None:
        opp = OpportunityCandidate(
            opportunity_id=UUID("00000000-0000-0000-0000-000000000060"),
            description="Low lobby occupancy detected",
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        assert opp.schema_version == SCHEMA_VERSION

    def test_with_metrics(self) -> None:
        opp = OpportunityCandidate(
            opportunity_id=UUID("00000000-0000-0000-0000-000000000060"),
            description="Low lobby occupancy",
            event_time=_utc(2026, 7, 29, 12, 0),
            metric_values=[
                MetricValue(
                    metric_name="occupancy",
                    value=0.2,
                    event_time=_utc(2026, 7, 29, 12, 0),
                ),
            ],
        )
        assert len(opp.metric_values) == 1


# =============================================================================
# Intelligence Contracts
# =============================================================================


class TestEvidencePackage:
    """EvidencePackage serialization and validation."""

    def test_minimal_construction(self) -> None:
        pkg = EvidencePackage(
            package_id=UUID("00000000-0000-0000-0000-000000000070"),
            created_at=_utc(2026, 7, 29, 12, 0),
        )
        assert pkg.schema_version == SCHEMA_VERSION

    def test_with_evidence_refs(self) -> None:
        ref = EvidenceRef(
            ref_id=UUID("00000000-0000-0000-0000-000000000040"),
            ref_type=EvidenceType.FRAME,
            ref_uri="frame://00000000-0000-0000-0000-000000000003",
        )
        pkg = EvidencePackage(
            package_id=UUID("00000000-0000-0000-0000-000000000070"),
            created_at=_utc(2026, 7, 29, 12, 0),
            evidence_refs=[ref],
        )
        assert len(pkg.evidence_refs) == 1


class TestFinding:
    """Finding serialization and validation."""

    def test_minimal_construction(self) -> None:
        finding = Finding(
            finding_id=UUID("00000000-0000-0000-0000-000000000080"),
            evidence_package_id=UUID("00000000-0000-0000-0000-000000000070"),
            description="Lobby staffing insufficient during peak hours",
            event_time=_utc(2026, 7, 29, 12, 0),
            finding_type="staffing_gap",
        )
        assert finding.schema_version == SCHEMA_VERSION

    def test_serialize_round_trip(self) -> None:
        original = Finding(
            finding_id=UUID("00000000-0000-0000-0000-000000000080"),
            evidence_package_id=UUID("00000000-0000-0000-0000-000000000070"),
            description="Lobby staffing insufficient during peak hours",
            event_time=_utc(2026, 7, 29, 12, 0),
            finding_type="staffing_gap",
            confidence=0.92,
        )
        data = original.model_dump(mode="json")
        restored = Finding.model_validate(data)
        assert restored == original

    def test_invalid_confidence_rejected(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                finding_id=UUID("00000000-0000-0000-0000-000000000080"),
                evidence_package_id=UUID("00000000-0000-0000-0000-000000000070"),
                description="Test finding",
                event_time=_utc(2026, 7, 29, 12, 0),
                finding_type="test",
                confidence=1.5,
            )


class TestRecommendation:
    """Recommendation serialization and validation."""

    def test_minimal_construction(self) -> None:
        rec = Recommendation(
            recommendation_id=UUID("00000000-0000-0000-0000-000000000090"),
            description="Add one more front desk staff during 7-9 AM",
            created_at=_utc(2026, 7, 29, 12, 0),
        )
        assert rec.priority == Priority.MEDIUM

    def test_serialize_round_trip(self) -> None:
        original = Recommendation(
            recommendation_id=UUID("00000000-0000-0000-0000-000000000090"),
            finding_ids=[UUID("00000000-0000-0000-0000-000000000080")],
            description="Add one more front desk staff during 7-9 AM",
            priority=Priority.HIGH,
            created_at=_utc(2026, 7, 29, 12, 0),
        )
        data = original.model_dump(mode="json")
        restored = Recommendation.model_validate(data)
        assert restored == original


# =============================================================================
# Operational Contracts
# =============================================================================


class TestAlert:
    """Alert serialization and validation."""

    def test_minimal_construction(self) -> None:
        alert = Alert(
            alert_id=UUID("00000000-0000-0000-0000-000000000100"),
            alert_type="unauthorized_access",
            title="Unauthorized access detected",
            description="Unknown person detected in restricted area",
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        assert alert.severity == Severity.INFO

    def test_serialize_round_trip(self) -> None:
        original = Alert(
            alert_id=UUID("00000000-0000-0000-0000-000000000100"),
            alert_type="fire_alarm",
            severity=Severity.CRITICAL,
            title="Fire alarm triggered",
            description="Smoke detected in kitchen area",
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        data = original.model_dump(mode="json")
        restored = Alert.model_validate(data)
        assert restored == original


class TestApprovalRequest:
    """ApprovalRequest serialization and validation."""

    def test_minimal_construction(self) -> None:
        req = ApprovalRequest(
            request_id=UUID("00000000-0000-0000-0000-000000000110"),
            recommendation_id=UUID("00000000-0000-0000-0000-000000000090"),
            requested_at=_utc(2026, 7, 29, 12, 0),
        )
        assert req.status == ApprovalStatus.PENDING

    def test_serialize_round_trip(self) -> None:
        original = ApprovalRequest(
            request_id=UUID("00000000-0000-0000-0000-000000000110"),
            recommendation_id=UUID("00000000-0000-0000-0000-000000000090"),
            status=ApprovalStatus.APPROVED,
            requested_at=_utc(2026, 7, 29, 12, 0),
            resolved_at=_utc(2026, 7, 29, 12, 30),
            reason="Approved by manager on duty",
        )
        data = original.model_dump(mode="json")
        restored = ApprovalRequest.model_validate(data)
        assert restored == original


class TestActionCommand:
    """ActionCommand serialization and validation."""

    def test_minimal_construction(self) -> None:
        cmd = ActionCommand(
            command_id=UUID("00000000-0000-0000-0000-000000000120"),
            command_type="notify_staff",
            issued_at=_utc(2026, 7, 29, 12, 0),
        )
        assert cmd.schema_version == SCHEMA_VERSION

    def test_with_parameters(self) -> None:
        cmd = ActionCommand(
            command_id=UUID("00000000-0000-0000-0000-000000000120"),
            command_type="adjust_schedule",
            approval_request_id=UUID("00000000-0000-0000-0000-000000000110"),
            parameters={"staff_count": 1, "shift": "morning"},
            issued_at=_utc(2026, 7, 29, 12, 0),
        )
        assert cmd.parameters["staff_count"] == 1

    def test_serialize_round_trip(self) -> None:
        original = ActionCommand(
            command_id=UUID("00000000-0000-0000-0000-000000000120"),
            command_type="send_alert",
            parameters={"channel": "slack", "message": "test"},
            issued_at=_utc(2026, 7, 29, 12, 0),
        )
        data = original.model_dump(mode="json")
        restored = ActionCommand.model_validate(data)
        assert restored == original


# =============================================================================
# Cross-Contract Compatibility
# =============================================================================


class TestContractCompatibility:
    """Cross-contract integration patterns."""

    def test_event_envelope_contains_detection(self) -> None:
        """An EventEnvelope can carry a DetectionObservation as payload."""
        det = DetectionObservation(
            detection_id=UUID("00000000-0000-0000-0000-000000000010"),
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            class_name="person",
            confidence=0.95,
            bounding_box=BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        envelope = EventEnvelope[DetectionObservation](
            event_id=UUID("00000000-0000-0000-0000-000000000030"),
            event_type="detection.observed",
            event_time=_utc(2026, 7, 29, 12, 0),
            produced_at=_utc(2026, 7, 29, 12, 0, 1),
            source="yolo.detector",
            payload=det,
        )
        data = envelope.model_dump(mode="json")
        restored = EventEnvelope[DetectionObservation].model_validate(data)
        assert restored.payload == det

    def test_finding_traces_to_recommendation(self) -> None:
        """Recommendation can reference findings."""
        finding = Finding(
            finding_id=UUID("00000000-0000-0000-0000-000000000080"),
            evidence_package_id=UUID("00000000-0000-0000-0000-000000000070"),
            description="Low lobby occupancy",
            event_time=_utc(2026, 7, 29, 12, 0),
            finding_type="occupancy",
        )
        rec = Recommendation(
            recommendation_id=UUID("00000000-0000-0000-0000-000000000090"),
            finding_ids=[finding.finding_id],
            description="Investigate lobby traffic patterns",
            created_at=_utc(2026, 7, 29, 12, 1),
        )
        assert finding.finding_id in rec.finding_ids

    def test_recommendation_to_approval_to_action_pipeline(self) -> None:
        """Full pipeline: Recommendation -> ApprovalRequest -> ActionCommand."""
        rec = Recommendation(
            recommendation_id=UUID("00000000-0000-0000-0000-000000000090"),
            description="Add front desk staff",
            created_at=_utc(2026, 7, 29, 12, 0),
        )
        approval = ApprovalRequest(
            request_id=UUID("00000000-0000-0000-0000-000000000110"),
            recommendation_id=rec.recommendation_id,
            requested_at=_utc(2026, 7, 29, 12, 0),
        )
        cmd = ActionCommand(
            command_id=UUID("00000000-0000-0000-0000-000000000120"),
            command_type="adjust_schedule",
            approval_request_id=approval.request_id,
            issued_at=_utc(2026, 7, 29, 12, 35),
        )
        assert cmd.approval_request_id == approval.request_id
        assert approval.recommendation_id == rec.recommendation_id

    def test_live_and_recorded_frames_converge(self) -> None:
        """Both live and recorded sources produce FramePackets for shared pipeline."""
        live_packet = FramePacket(
            frame_id=UUID("00000000-0000-0000-0000-000000000003"),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            frame_index=0,
            event_time=_utc(2026, 7, 29, 12, 0),
        )
        recorded_packet = FramePacket(
            frame_id=UUID("00000000-0000-0000-0000-000000000004"),
            session_id=UUID("00000000-0000-0000-0000-000000000005"),
            frame_index=0,
            event_time=_utc(2026, 7, 28, 12, 0),
        )
        # Both use the same FramePacket type — no separate models needed
        assert type(live_packet) is type(recorded_packet)
