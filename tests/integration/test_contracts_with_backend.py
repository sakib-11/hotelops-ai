"""End-to-end integration tests: Canonical contracts x existing FastAPI backend.

Tests that:
1. The existing backend starts and responds correctly
2. Contract models serialize properly through the HTTP layer
3. Contract payloads can be sent as HTTP request bodies
4. Round-trip (Python -> JSON -> HTTP -> JSON -> Python) preserves data
5. Invalid contract data is rejected at the serialization boundary
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from contracts.common import SCHEMA_VERSION, utc_now
from contracts.events import EventEnvelope
from contracts.operations import ActionCommand
from contracts.video import FramePacket
from contracts.vision import BoundingBox, DetectionObservation

pytestmark = [pytest.mark.integration]

# =============================================================================
# Test Setup — Minimal FastAPI app that doesn't need Postgres/Redis/MinIO
# =============================================================================


def _make_test_app() -> FastAPI:
    """Create a lightweight test FastAPI app with a contract echo endpoint.

    Uses the real backend's router to verify existing endpoints still work,
    plus adds a contract-echo endpoint to demonstrate HTTP round-trip.

    Infrastructure dependencies (db, redis, storage) are NOT initialized —
    the settings and readiness dependencies are overridden with stubs.
    """
    from backend.app.api.router import api_router
    from backend.app.dependencies import get_settings
    from backend.app.infrastructure.config import Settings

    app = FastAPI(title="HotelOps AI — Test")
    app.include_router(api_router)

    # Add the root route that's defined in main.py but needed for tests
    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "HotelOps AI",
            "version": "0.1.0",
            "status": "running",
        }

    # Override settings dependency to avoid needing .env file
    async def _override_settings() -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            APP_NAME="HotelOps AI",
            APP_ENV="test",
            APP_VERSION="0.1.0",
            DEBUG=False,
            LOG_LEVEL="INFO",
        )

    app.dependency_overrides[get_settings] = _override_settings

    # Add a contract echo endpoint to demonstrate HTTP-level contract serialization
    @app.post("/_test/echo/detection")
    async def echo_detection(payload: DetectionObservation) -> DetectionObservation:
        """Echo back a DetectionObservation — validates HTTP JSON ↔ contract."""
        return payload

    @app.post("/_test/echo/envelope")
    async def echo_envelope(payload: dict[str, Any]) -> dict[str, Any]:
        """Accept a generic event envelope payload and echo it back."""
        return payload

    @app.post("/_test/echo/frame")
    async def echo_frame(payload: FramePacket) -> FramePacket:
        """Echo back a FramePacket — validates HTTP JSON ↔ contract."""
        return payload

    @app.post("/_test/echo/action")
    async def echo_action(payload: ActionCommand) -> ActionCommand:
        """Echo back an ActionCommand — validates HTTP JSON ↔ contract."""
        return payload

    return app


@pytest.fixture
def client() -> TestClient:
    """Provide a TestClient with the lightweight test app."""
    app = _make_test_app()
    with TestClient(app) as test_client:
        yield test_client


# =============================================================================
# Existing Backend Endpoints
# =============================================================================


class TestExistingEndpoints:
    """Existing backend endpoints must still respond correctly."""

    def test_root_endpoint(self, client: TestClient) -> None:
        """GET / returns service information."""
        response = client.get("/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["service"] == "HotelOps AI"
        assert "version" in data
        assert data["status"] == "running"

    def test_health_endpoint(self, client: TestClient) -> None:
        """GET /health returns liveness check."""
        response = client.get("/health")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "HotelOps AI"


# =============================================================================
# Contract HTTP Serialization Round-Trip
# =============================================================================


def _utc(**overrides: Any) -> datetime:
    """Create a UTC datetime for test purposes."""
    return datetime.now(UTC) + timedelta(**overrides)


class TestContractHttpRoundTrip:
    """Canonical contracts survive serialization through the HTTP layer."""

    # ------------------------------------------------------------------
    # DetectionObservation HTTP round-trip
    # ------------------------------------------------------------------

    def test_detection_observation_round_trip(self, client: TestClient) -> None:
        """DetectionObservation serializes to JSON, survives HTTP, and deserializes."""
        payload = DetectionObservation(
            detection_id=UUID("11111111-1111-1111-1111-111111111111"),
            frame_id=UUID("22222222-2222-2222-2222-222222222222"),
            class_name="person",
            confidence=0.95,
            bounding_box=BoundingBox(x_min=0.1, y_min=0.2, x_max=0.5, y_max=0.8),
            event_time=_utc(seconds=-5),
        )
        response = client.post("/_test/echo/detection", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_200_OK, response.text

        restored = DetectionObservation.model_validate(response.json())
        assert restored == payload
        assert restored.class_name == "person"
        from pytest import approx

        assert restored.confidence == approx(0.95)
        assert restored.bounding_box.x_min == approx(0.1)
        assert restored.schema_version == SCHEMA_VERSION

    def test_detection_observation_invalid_confidence_rejected(self, client: TestClient) -> None:
        """Invalid confidence is rejected at the HTTP boundary."""
        invalid_data = {
            "detection_id": str(UUID("11111111-1111-1111-1111-111111111111")),
            "frame_id": str(UUID("22222222-2222-2222-2222-222222222222")),
            "class_name": "person",
            "confidence": 1.5,  # > 1.0 — invalid
            "bounding_box": {"x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.8},
            "event_time": _utc(seconds=-5).isoformat(),
        }
        response = client.post("/_test/echo/detection", json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # ------------------------------------------------------------------
    # FramePacket HTTP round-trip
    # ------------------------------------------------------------------

    def test_frame_packet_round_trip(self, client: TestClient) -> None:
        """FramePacket serializes to JSON, survives HTTP, and deserializes."""
        payload = FramePacket(
            frame_id=UUID("33333333-3333-3333-3333-333333333333"),
            session_id=UUID("44444444-4444-4444-4444-444444444444"),
            frame_index=42,
            event_time=_utc(seconds=-10),
            width=1920,
            height=1080,
            source_ref=UUID("55555555-5555-5555-5555-555555555555"),
        )
        response = client.post("/_test/echo/frame", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_200_OK, response.text

        restored = FramePacket.model_validate(response.json())
        assert restored == payload
        assert restored.frame_index == 42
        assert restored.width == 1920
        assert restored.height == 1080

    def test_frame_packet_negative_index_rejected(self, client: TestClient) -> None:
        """Negative frame index is rejected at the HTTP boundary."""
        invalid_data = {
            "frame_id": str(UUID("33333333-3333-3333-3333-333333333333")),
            "session_id": str(UUID("44444444-4444-4444-4444-444444444444")),
            "frame_index": -1,
            "event_time": _utc(seconds=-10).isoformat(),
        }
        response = client.post("/_test/echo/frame", json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # ------------------------------------------------------------------
    # ActionCommand HTTP round-trip
    # ------------------------------------------------------------------

    def test_action_command_round_trip(self, client: TestClient) -> None:
        """ActionCommand serializes through HTTP and deserializes correctly."""
        payload = ActionCommand(
            command_id=UUID("66666666-6666-6666-6666-666666666666"),
            command_type="notify_staff",
            parameters={"channel": "slack", "message": "Front desk needs support"},
            issued_at=_utc(seconds=-2),
        )
        response = client.post("/_test/echo/action", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_200_OK, response.text

        restored = ActionCommand.model_validate(response.json())
        assert restored == payload
        assert restored.command_type == "notify_staff"
        assert restored.parameters["channel"] == "slack"

    # ------------------------------------------------------------------
    # EventEnvelope with arbitrary payload through HTTP
    # ------------------------------------------------------------------

    def test_event_envelope_http_round_trip(self, client: TestClient) -> None:
        """EventEnvelope with dict payload survives HTTP round-trip."""
        envelope = EventEnvelope[dict](
            event_id=UUID("77777777-7777-7777-7777-777777777777"),
            event_type="detection.observed",
            event_time=_utc(seconds=-5),
            produced_at=utc_now(),
            source="integration_test",
            payload={"count": 5, "class": "person"},
        )
        json_data = envelope.model_dump(mode="json")
        response = client.post("/_test/echo/envelope", json=json_data)
        assert response.status_code == status.HTTP_200_OK, response.text

        restored = EventEnvelope[dict].model_validate(response.json())
        assert restored.event_id == envelope.event_id
        assert restored.event_type == "detection.observed"
        assert restored.payload["count"] == 5

    # ------------------------------------------------------------------
    # Extra fields rejected at HTTP boundary
    # ------------------------------------------------------------------

    def test_extra_fields_rejected_at_http_boundary(self, client: TestClient) -> None:
        """Unknown fields sent to contract endpoints are rejected with 422."""
        data_with_extra = {
            "detection_id": str(UUID("11111111-1111-1111-1111-111111111111")),
            "frame_id": str(UUID("22222222-2222-2222-2222-222222222222")),
            "class_name": "person",
            "confidence": 0.95,
            "bounding_box": {"x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.8},
            "event_time": datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC).isoformat(),
            "unknown_field": "should_not_be_allowed",
        }
        response = client.post("/_test/echo/detection", json=data_with_extra)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
