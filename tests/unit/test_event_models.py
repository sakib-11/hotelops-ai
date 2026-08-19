"""Tests for Task 6.6 — operational event ORM model.

Tests schema correctness — columns, timestamps, constraints, composite
FKs, and indexes — without requiring a live database. Hypertable
conversion itself is exercised by the integration tests against a real
TimescaleDB.

Uses SQLAlchemy's Table metadata inspection to verify schema.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.models.events import OperationalEventModel


class TestOperationalEventsSchema:
    """Typed event persistence following the Task 4 EventEnvelope."""

    def _table(self) -> Table:
        return Base.metadata.tables["operational_events"]

    def test_table_exists(self) -> None:
        assert "operational_events" in Base.metadata.tables

    def test_model_table_name(self) -> None:
        assert OperationalEventModel.__tablename__ == "operational_events"

    def test_primary_key_is_composite_time_event(self) -> None:
        """The canonical hypertable PK includes the partitioning column:
        TimescaleDB requires it inside any unique constraint."""
        table = self._table()
        assert [c.name for c in table.primary_key.columns] == ["event_time", "event_id"]
        assert str(table.columns["event_id"].type) == "UUID"

    def test_envelope_metadata_typed_columns(self) -> None:
        """Envelope metadata is typed — no generic key-value structure."""
        table = self._table()
        assert not table.columns["event_type"].nullable
        assert table.columns["event_type"].type.length == 100
        assert not table.columns["schema_version"].nullable
        assert not table.columns["source"].nullable

    def test_tenant_scope_columns(self) -> None:
        table = self._table()
        assert not table.columns["tenant_id"].nullable
        assert not table.columns["venue_id"].nullable
        assert table.columns["session_id"].nullable
        assert table.columns["camera_id"].nullable

    def test_correlation_identifiers(self) -> None:
        table = self._table()
        assert table.columns["correlation_id"].nullable
        assert table.columns["causation_id"].nullable

    def test_event_time_explicit(self) -> None:
        """event_time is an explicit required column — never created_at."""
        table = self._table()
        assert not table.columns["event_time"].nullable
        assert table.columns["event_time"].type.timezone is True

    def test_schema_version_server_default(self) -> None:
        """The envelope schema version defaults to the contract SCHEMA_VERSION."""
        table = self._table()
        default = table.columns["schema_version"].server_default
        assert default is not None
        assert "1.0" in default.arg

    def test_ingestion_and_processing_times(self) -> None:
        table = self._table()
        assert not table.columns["ingestion_time"].nullable
        assert table.columns["ingestion_time"].type.timezone is True
        assert table.columns["processing_time"].nullable
        assert table.columns["processing_time"].type.timezone is True

    def test_payload_jsonb_required(self) -> None:
        """The envelope payload (generic PayloadT) is JSONB and required."""
        table = self._table()
        assert not table.columns["payload"].nullable
        assert "JSONB" in str(table.columns["payload"].type)

    def test_event_type_not_empty_check(self) -> None:
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_operational_events_event_type_not_empty" in checks
        assert "ck_operational_events_source_not_empty" in checks
        assert "ck_operational_events_schema_version_not_empty" in checks

    def test_timestamp_ordering_checks(self) -> None:
        """Ingestion/processing can never precede the real-world event."""
        table = self._table()
        checks = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert "ck_operational_events_ingestion_not_before_event" in checks
        assert "ck_operational_events_processing_not_before_event" in checks

    def test_venue_tenant_reference_prevented(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        venue_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["venue_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["venues", "venues"]
        ]
        assert len(venue_fks) >= 1
        assert venue_fks[0].ondelete == "CASCADE"

    def test_camera_tenant_reference_prevented(self) -> None:
        table = self._table()
        fks = table.foreign_key_constraints
        camera_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["camera_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["cameras", "cameras"]
        ]
        assert len(camera_fks) >= 1

    def test_session_tenant_reference_prevented(self) -> None:
        """Events reference sessions via the composite (session_id, tenant_id)
        FK, which requires a matching unique target on video_sessions."""
        table = self._table()
        fks = table.foreign_key_constraints
        session_fks = [
            fk
            for fk in fks
            if [c.name for c in fk.columns] == ["session_id", "tenant_id"]
            and [e.column.table.name for e in fk.elements] == ["video_sessions", "video_sessions"]
        ]
        assert len(session_fks) >= 1
        # The target unique constraint must exist on video_sessions.
        target = Base.metadata.tables["video_sessions"]
        uqs = [c for c in target.constraints if isinstance(c, UniqueConstraint)]
        assert any([col.name for col in uq.columns] == ["session_id", "tenant_id"] for uq in uqs)

    def test_indexes_for_query_patterns(self) -> None:
        table = self._table()
        idx_names = {idx.name for idx in table.indexes}
        for name in (
            "ix_operational_events_tenant_time",
            "ix_operational_events_type_time",
            "ix_operational_events_venue_id",
            "ix_operational_events_session_id",
        ):
            assert name in idx_names, (
                f"Missing index: {name}"
            )  # Task 6.13 review: global event_time-only lookups are served by the
        # hypertable PK (event_time, event_id) — the single-column index was
        # redundant (removed in migration 015).
        assert "ix_operational_events_event_time" not in idx_names


class TestEventModelDefaults:
    """The model accepts the envelope fields and applies defaults."""

    def test_event_creation(self) -> None:
        now = datetime.now(UTC)
        event = OperationalEventModel(
            event_id=uuid.uuid4(),
            event_type="detection.observation",
            tenant_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            camera_id=uuid.uuid4(),
            event_time=now,
            produced_at=now,
            source="cv.pipeline",
            payload={"class_name": "person", "confidence": 0.92},
        )
        assert event.event_type == "detection.observation"
        assert event.correlation_id is None
        assert event.ingestion_time is None  # server default applies at flush

    def test_event_with_correlation(self) -> None:
        now = datetime.now(UTC)
        event = OperationalEventModel(
            event_id=uuid.uuid4(),
            event_type="track.observation",
            tenant_id=uuid.uuid4(),
            venue_id=uuid.uuid4(),
            event_time=now,
            produced_at=now,
            source="cv.tracker",
            correlation_id="corr-123",
            causation_id="cause-456",
            payload={"track_state": "active"},
        )
        assert event.correlation_id == "corr-123"
        assert event.causation_id == "cause-456"
