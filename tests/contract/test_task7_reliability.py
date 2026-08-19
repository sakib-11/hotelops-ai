"""Contract tests for Task 7 (Phase 15).

Verifies the Task 4 EventEnvelope remains the ONLY event contract in the
reliability pipeline:

  - serialize_envelope produces deterministic, JSON-safe envelope dicts
  - a serialized envelope round-trips through model_validate unchanged
  - invalid envelopes (missing event ID, invalid UUID, naive timestamps,
    bad schema version, empty event type, malformed payload, extra
    fields) are REJECTED before they can enter the outbox
  - the outbox payload carries NO tenant/venue identity — tenant scope
    is derived server-side from the ActorContext (never client-supplied)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.application.services.outbox import serialize_envelope, validate_envelope
from contracts.common import SCHEMA_VERSION
from contracts.events import EventEnvelope


def _envelope(**overrides) -> EventEnvelope[dict]:
    """A canonical valid envelope with optional field overrides."""
    now = datetime.now(UTC)
    values = {
        "event_id": uuid.uuid4(),
        "event_type": "operational.event",
        "event_time": now,
        "produced_at": now,
        "source": "test.pipeline",
        "payload": {"class_name": "person"},
    }
    values.update(overrides)
    return EventEnvelope[dict](**values)


class TestEnvelopeSerialization:
    def test_serialize_round_trips(self) -> None:
        envelope = _envelope(payload={"count": 1, "nested": {"ok": True}})
        data = serialize_envelope(envelope)
        restored = EventEnvelope[dict].model_validate(data)
        assert restored == envelope

    def test_serialize_is_json_safe_and_deterministic(self) -> None:
        envelope = _envelope()
        data = serialize_envelope(envelope)
        assert serialize_envelope(envelope) == data
        for key in ("event_id", "event_type", "schema_version", "event_time", "source"):
            assert isinstance(data[key], (str, int, float, bool)) or data[key] is None

    def test_outbox_payload_carries_no_tenant_identity(self) -> None:
        """Tenant/venue are recorded on the outbox ROW, not in the payload.

        A client can never smuggle a tenant_id into the event — the
        envelope contract has no such field (extra=forbid enforces this).
        """
        data = serialize_envelope(_envelope())
        assert "tenant_id" not in data
        assert "venue_id" not in data

    def test_schema_version_is_canonical(self) -> None:
        assert serialize_envelope(_envelope())["schema_version"] == SCHEMA_VERSION


class TestEnvelopeValidationBeforeOutbox:
    def test_valid_envelope_accepted(self) -> None:
        validate_envelope(_envelope())  # must not raise

    def test_missing_event_id_rejected(self) -> None:
        data = serialize_envelope(_envelope())
        del data["event_id"]
        with pytest.raises(ValidationError):
            EventEnvelope[dict].model_validate(data)

    def test_invalid_uuid_rejected(self) -> None:
        data = serialize_envelope(_envelope())
        data["event_id"] = "not-a-uuid"
        with pytest.raises(ValidationError):
            EventEnvelope[dict].model_validate(data)

    def test_invalid_event_version_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(schema_version="2.99-unknown")

    def test_naive_timestamp_rejected(self) -> None:
        data = serialize_envelope(_envelope())
        data["event_time"] = datetime.now().isoformat()  # no tz
        with pytest.raises(ValidationError):
            EventEnvelope[dict].model_validate(data)

    def test_event_time_in_future_accepted_but_produced_is_utc(self) -> None:
        # A future event_time is semantically allowed (scheduled events);
        # the invariant that matters is UTC awareness.
        future = datetime.now(UTC) + timedelta(hours=1)
        validate_envelope(_envelope(event_time=future))

    def test_empty_event_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(event_type="")

    def test_empty_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(source="")

    def test_malformed_payload_rejected(self) -> None:
        """The payload is typed dict — a non-JSON value is malformed."""
        data = serialize_envelope(_envelope())
        data["payload"] = object()  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            EventEnvelope[dict].model_validate(data)

    def test_extra_fields_rejected(self) -> None:
        """extra=forbid — a client cannot smuggle tenant_id into the event."""
        data = serialize_envelope(_envelope())
        data["tenant_id"] = str(uuid.uuid4())
        with pytest.raises(ValidationError):
            EventEnvelope[dict].model_validate(data)
