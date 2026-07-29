"""Canonical time semantics for HotelOps AI contracts.

All canonical timestamps MUST be timezone-aware UTC.

Timestamp semantics:
    event_time:    When the represented real-world event occurred.
    ingested_at:   When HotelOps received the data.
    processed_at:  When processing occurred.
    created_at:    When a canonical object was created (where applicable).

This distinction is critical because recorded video may be processed days
after the real-world event occurred. Never silently treat processing time
as event time.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current UTC datetime with explicit timezone information.

    Returns:
        A timezone-aware datetime representing the current UTC time.
    """
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """Convert an arbitrary datetime to UTC.

    Args:
        dt: A datetime (timezone-aware or naive).

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        ValueError: If the datetime is naive (no timezone info).
    """
    if dt.tzinfo is None:
        msg = "Cannot convert naive datetime to UTC — attach tzinfo or use validate_utc()"
        raise ValueError(msg)
    return dt.astimezone(UTC)


_NAIVE_DATETIME_ERROR = "Datetime is timezone-naive — attach tzinfo (e.g., timezone.utc)"


def validate_utc(dt: datetime) -> datetime:
    """Validate that a datetime is timezone-aware and return it unchanged.

    Use this as a Pydantic field validator on datetime fields that must be UTC.
    Does NOT use mode="before" — Pydantic's built-in coercion converts strings
    to datetime before this validator runs.

    For optional datetime fields (e.g., datetime | None), the validator
    is called with None by Pydantic when the value is absent. We return
    None in that case to allow the field's type annotation to handle it.

    Args:
        dt: The datetime to validate (already coerced by Pydantic).

    Returns:
        The same datetime if valid.

    Raises:
        ValueError: If the datetime is naive.
    """
    if not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        raise ValueError(_NAIVE_DATETIME_ERROR)
    return dt


def serialize_utc(dt: datetime) -> str:
    """Serialize a UTC datetime to an ISO-8601 string with explicit timezone.

    Args:
        dt: A timezone-aware datetime.

    Returns:
        ISO-8601 string like '2026-07-29T12:00:00+00:00'.

    Raises:
        ValueError: If the datetime is naive.
    """
    if dt.tzinfo is None:
        raise ValueError(_NAIVE_DATETIME_ERROR)
    return dt.astimezone(UTC).isoformat()


def parse_utc(value: str | datetime) -> datetime:
    """Parse a value into a timezone-aware UTC datetime.

    Accepts:
        - ISO-8601 string
        - Existing datetime (validated for timezone awareness)

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        ValueError: If parsing fails or datetime is naive.
        TypeError: If value is not a string or datetime.
    """
    if isinstance(value, datetime):
        return validate_utc(value)

    if not isinstance(value, str):
        msg = f"Expected str or datetime, got {type(value).__name__}"
        raise TypeError(msg)

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


__all__ = [
    "parse_utc",
    "serialize_utc",
    "to_utc",
    "utc_now",
    "validate_utc",
]
