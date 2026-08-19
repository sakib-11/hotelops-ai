"""Deterministic storage object key generation, parsing, and validation.

Enforces the canonical multi-tenant and multi-venue storage key hierarchy:
  tenants/{tenant_id}/venues/{venue_id}/{category}/{year}/{month}/{day}/{artifact_id}.{extension}
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from backend.app.infrastructure.storage.exceptions import InvalidObjectKeyError
from backend.app.infrastructure.storage.types import ObjectCategory, StorageKeyComponents

_KEY_PATTERN = re.compile(
    r"^tenants/(?P<tenant_id>[0-9a-fA-F-]{36})/"
    r"venues/(?P<venue_id>[0-9a-fA-F-]{36})/"
    r"(?P<category>recordings|evidence|reports|analytics|temporary)/"
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<artifact_id>[0-9a-fA-F-]{36})\.(?P<extension>[a-zA-Z0-9_.]+)$"
)

_FORBIDDEN_CHARACTERS = {"\x00", "\\", "\r", "\n", "\t", " "}


def normalize_extension(ext: str) -> str:
    """Normalize file extension by removing leading dots and converting to lowercase."""
    if not isinstance(ext, str):
        msg = f"Extension must be a string, got {type(ext)}"
        raise InvalidObjectKeyError(str(ext), msg)

    if any(c in ext for c in _FORBIDDEN_CHARACTERS):
        msg = f"Invalid file extension characters: '{ext}'"
        raise InvalidObjectKeyError(ext, msg)

    cleaned = ext.lstrip(".").lower()
    if not cleaned:
        msg = f"Invalid file extension: '{ext}'"
        raise InvalidObjectKeyError(ext, msg)

    if not re.match(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$", cleaned):
        msg = f"Invalid file extension characters: '{ext}'"
        raise InvalidObjectKeyError(ext, msg)

    return cleaned


def _validate_uuid(val: UUID | str, name: str) -> UUID:
    """Ensure an identifier is a valid UUID object."""
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        msg = f"Invalid {name}: '{val}' is not a valid UUID"
        raise InvalidObjectKeyError(str(val), msg) from exc


def build_object_key(
    tenant_id: UUID | str,
    venue_id: UUID | str,
    category: ObjectCategory | str,
    artifact_id: UUID | str,
    extension: str,
    *,
    capture_time: datetime | None = None,
) -> str:
    """Build a deterministic, scoped storage object key.

    Args:
        tenant_id: Root tenant boundary UUID.
        venue_id: Venue scope UUID.
        category: Controlled category (recordings, evidence, reports, analytics, temporary).
        artifact_id: Canonical artifact UUID.
        extension: File extension (e.g. 'mp4', 'jpg', 'pdf', 'json.gz').
        capture_time: Optional capture/creation time for date partitioning (defaults to UTC now).

    Returns:
        A normalized S3 object key string.
    """
    valid_tenant_id = _validate_uuid(tenant_id, "tenant_id")
    valid_venue_id = _validate_uuid(venue_id, "venue_id")
    valid_artifact_id = _validate_uuid(artifact_id, "artifact_id")

    if isinstance(category, str):
        try:
            cat = ObjectCategory(category)
        except ValueError as exc:
            msg = f"Unknown category '{category}'"
            raise InvalidObjectKeyError(str(category), msg) from exc
    else:
        cat = category

    ts = capture_time.astimezone(UTC) if capture_time is not None else datetime.now(UTC)
    ext = normalize_extension(extension)

    return (
        f"tenants/{valid_tenant_id}/venues/{valid_venue_id}/{cat.value}/"
        f"{ts.year:04d}/{ts.month:02d}/{ts.day:02d}/{valid_artifact_id}.{ext}"
    )


def build_recording_key(
    tenant_id: UUID | str,
    venue_id: UUID | str,
    recording_id: UUID | str,
    extension: str = "mp4",
    *,
    capture_time: datetime | None = None,
) -> str:
    """Build a deterministic object key for a CCTV video recording asset."""
    return build_object_key(
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=ObjectCategory.RECORDINGS,
        artifact_id=recording_id,
        extension=extension,
        capture_time=capture_time,
    )


def build_evidence_key(
    tenant_id: UUID | str,
    venue_id: UUID | str,
    evidence_id: UUID | str,
    extension: str = "jpg",
    *,
    capture_time: datetime | None = None,
) -> str:
    """Build a deterministic object key for a visual evidence keyframe or crop."""
    return build_object_key(
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=ObjectCategory.EVIDENCE,
        artifact_id=evidence_id,
        extension=extension,
        capture_time=capture_time,
    )


def build_report_key(
    tenant_id: UUID | str,
    venue_id: UUID | str,
    report_id: UUID | str,
    extension: str = "pdf",
    *,
    capture_time: datetime | None = None,
) -> str:
    """Build a deterministic object key for an operational or executive report."""
    return build_object_key(
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=ObjectCategory.REPORTS,
        artifact_id=report_id,
        extension=extension,
        capture_time=capture_time,
    )


def build_analytics_key(
    tenant_id: UUID | str,
    venue_id: UUID | str,
    artifact_id: UUID | str,
    extension: str = "json.gz",
    *,
    capture_time: datetime | None = None,
) -> str:
    """Build a deterministic object key for an analytical artifact or heatmap."""
    return build_object_key(
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=ObjectCategory.ANALYTICS,
        artifact_id=artifact_id,
        extension=extension,
        capture_time=capture_time,
    )


def build_temporary_key(
    tenant_id: UUID | str,
    venue_id: UUID | str,
    upload_id: UUID | str,
    extension: str = "bin",
    *,
    capture_time: datetime | None = None,
) -> str:
    """Build a deterministic object key for an in-flight temporary upload."""
    return build_object_key(
        tenant_id=tenant_id,
        venue_id=venue_id,
        category=ObjectCategory.TEMPORARY,
        artifact_id=upload_id,
        extension=extension,
        capture_time=capture_time,
    )


def parse_object_key(object_key: str) -> StorageKeyComponents:
    """Parse and validate a standard scoped object key into its constituent components.

    Raises:
        InvalidObjectKeyError: If the key does not match the canonical hierarchy.
    """
    if not isinstance(object_key, str):
        msg = f"Object key must be a string, got {type(object_key)}"
        raise InvalidObjectKeyError(str(object_key), msg)

    # Prevent directory traversal and control character attacks
    if (
        ".." in object_key
        or "//" in object_key
        or object_key.startswith("/")
        or any(c in object_key for c in _FORBIDDEN_CHARACTERS)
    ):
        raise InvalidObjectKeyError(object_key, "Contains illegal path traversal characters")

    match = _KEY_PATTERN.match(object_key)
    if not match:
        raise InvalidObjectKeyError(
            object_key,
            "Key does not match format: tenants/{tenant_id}/venues/{venue_id}/{category}/{YYYY}/{MM}/{DD}/{id}.{ext}",
        )

    try:
        tenant_id = UUID(match.group("tenant_id"))
        venue_id = UUID(match.group("venue_id"))
        artifact_id = UUID(match.group("artifact_id"))
        category = ObjectCategory(match.group("category"))
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        extension = match.group("extension")

        return StorageKeyComponents(
            tenant_id=tenant_id,
            venue_id=venue_id,
            category=category,
            year=year,
            month=month,
            day=day,
            artifact_id=artifact_id,
            extension=extension,
        )
    except Exception as exc:
        raise InvalidObjectKeyError(object_key, f"Failed to parse key components: {exc}") from exc
