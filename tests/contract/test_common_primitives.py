"""Tests for common contract primitives: IDs, time, and versioning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts.common import (
    SCHEMA_VERSION,
    EventId,
    FrameId,
    new_uuid,
    parse_utc,
    serialize_utc,
    to_utc,
    utc_now,
    validate_schema_version,
    validate_utc,
)

# =============================================================================
# ID Tests
# =============================================================================


class TestIds:
    """Semantic ID types produce valid UUIDs and maintain type distinction."""

    def test_new_uuid_returns_uuid(self) -> None:
        """new_uuid() returns a valid UUID."""
        from uuid import UUID

        uid = new_uuid()
        assert isinstance(uid, UUID)

    def test_new_uuid_is_unique(self) -> None:
        """Consecutive new_uuid() calls return different values."""
        uid1 = new_uuid()
        uid2 = new_uuid()
        assert uid1 != uid2

    def test_event_id_is_uuid(self) -> None:
        """EventId wraps a UUID."""
        uid = new_uuid()
        eid = EventId(uid)
        assert isinstance(eid, type(uid))

    def test_frame_id_is_uuid(self) -> None:
        """FrameId wraps a UUID."""
        uid = new_uuid()
        fid = FrameId(uid)
        assert isinstance(fid, type(uid))

    def test_event_id_and_frame_id_are_distinct_types(self) -> None:
        """EventId and FrameId are nominally distinct (NewType)."""
        uid = new_uuid()
        eid = EventId(uid)
        fid = FrameId(uid)
        # They're both UUIDs at runtime, but statically distinct
        assert type(eid) is type(uid)
        assert type(fid) is type(uid)

    def test_new_uuid_str_representation(self) -> None:
        """UUID string representation is standard hex format."""
        uid = new_uuid()
        s = str(uid)
        assert len(s) == 36
        assert s.count("-") == 4


# =============================================================================
# Time Tests
# =============================================================================


class TestTime:
    """Canonical UTC datetime semantics."""

    def test_utc_now_returns_aware_datetime(self) -> None:
        """utc_now() returns a timezone-aware datetime."""
        now = utc_now()
        assert now.tzinfo is not None

    def test_utc_now_is_utc(self) -> None:
        """utc_now() returns UTC timezone."""
        now = utc_now()
        assert now.tzinfo == UTC or now.utcoffset() == timedelta(0)

    def test_utc_now_is_recent(self) -> None:
        """utc_now() returns a time within the last 5 seconds."""
        now = utc_now()
        delta = datetime.now(UTC) - now
        assert delta.total_seconds() < 5

    def test_to_utc_converts_aware_datetime(self) -> None:
        """to_utc() converts a non-UTC timezone-aware datetime to UTC."""
        eastern = timezone(timedelta(hours=-5))
        dt = datetime(2026, 7, 29, 7, 0, 0, tzinfo=eastern)
        result = to_utc(dt)
        assert result.hour == 12  # 07:00 -05:00 = 12:00 UTC
        assert result.tzinfo == UTC

    def test_to_utc_rejects_naive_datetime(self) -> None:
        """to_utc() raises ValueError for naive datetime."""
        dt = datetime(2026, 7, 29, 12, 0, 0)
        with pytest.raises(ValueError, match="naive"):
            to_utc(dt)

    def test_validate_utc_accepts_aware(self) -> None:
        """validate_utc() passes through a timezone-aware datetime."""
        dt = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        result = validate_utc(dt)
        assert result is dt

    def test_validate_utc_rejects_naive(self) -> None:
        """validate_utc() raises ValueError for naive datetime."""
        dt = datetime(2026, 7, 29, 12, 0, 0)
        with pytest.raises(ValueError, match="naive"):
            validate_utc(dt)

    def test_serialize_utc_iso_format(self) -> None:
        """serialize_utc() produces ISO-8601 with explicit UTC offset."""
        dt = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        result = serialize_utc(dt)
        assert result.endswith("+00:00")
        assert "2026-07-29T12:00:00" in result

    def test_serialize_utc_converts_non_utc(self) -> None:
        """serialize_utc() converts to UTC before serialization."""
        eastern = timezone(timedelta(hours=-5))
        dt = datetime(2026, 7, 29, 7, 0, 0, tzinfo=eastern)
        result = serialize_utc(dt)
        # 07:00 -05:00 = 12:00 UTC
        assert "12:00:00" in result

    def test_serialize_utc_rejects_naive(self) -> None:
        """serialize_utc() raises ValueError for naive datetime."""
        dt = datetime(2026, 7, 29, 12, 0, 0)
        with pytest.raises(ValueError, match="naive"):
            serialize_utc(dt)

    def test_parse_utc_from_iso_string(self) -> None:
        """parse_utc() parses ISO-8601 string to UTC datetime."""
        result = parse_utc("2026-07-29T12:00:00+00:00")
        assert result == datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

    def test_parse_utc_from_aware_datetime(self) -> None:
        """parse_utc() returns aware datetime unchanged."""
        dt = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        result = parse_utc(dt)
        assert result is dt

    def test_parse_utc_rejects_naive_string_by_assuming_utc(self) -> None:
        """parse_utc() assumes UTC for naive ISO strings (lenient parsing)."""
        result = parse_utc("2026-07-29T12:00:00")
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_parse_utc_rejects_naive_datetime(self) -> None:
        """parse_utc() rejects a naive datetime object."""
        dt = datetime(2026, 7, 29, 12, 0, 0)
        with pytest.raises(ValueError, match="naive"):
            parse_utc(dt)

    def test_parse_utc_from_offset_string(self) -> None:
        """parse_utc() handles ISO strings with non-UTC offsets."""
        result = parse_utc("2026-07-29T07:00:00-05:00")
        assert result == datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

    def test_round_trip_serialization(self) -> None:
        """Serialization round-trip: datetime -> str -> datetime preserves value."""
        original = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
        serialized = serialize_utc(original)
        deserialized = parse_utc(serialized)
        assert deserialized == original
        assert deserialized.tzinfo == UTC


# =============================================================================
# Versioning Tests
# =============================================================================


class TestVersioning:
    """Canonical schema versioning."""

    def test_schema_version_is_string(self) -> None:
        """SCHEMA_VERSION is a non-empty string."""
        assert isinstance(SCHEMA_VERSION, str)
        assert len(SCHEMA_VERSION) > 0

    def test_schema_version_format(self) -> None:
        """SCHEMA_VERSION follows semver-like 'major.minor' format."""
        parts = SCHEMA_VERSION.split(".")
        assert len(parts) == 2
        assert parts[0].isdigit()
        assert parts[1].isdigit()

    def test_validate_schema_version_accepts_current(self) -> None:
        """validate_schema_version() accepts the current version."""
        result = validate_schema_version(SCHEMA_VERSION)
        assert result == SCHEMA_VERSION

    def test_validate_schema_version_rejects_unknown(self) -> None:
        """validate_schema_version() rejects unsupported versions."""
        with pytest.raises(ValueError, match="Unsupported"):
            validate_schema_version("2.0")

    def test_validate_schema_version_rejects_empty(self) -> None:
        """validate_schema_version() rejects empty string."""
        with pytest.raises(ValueError, match="Unsupported"):
            validate_schema_version("")

    def test_validate_schema_version_rejects_garbage(self) -> None:
        """validate_schema_version() rejects clearly invalid versions."""
        with pytest.raises(ValueError, match="Unsupported"):
            validate_schema_version("not-a-version")
