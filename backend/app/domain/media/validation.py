"""Bounded content validation for uploaded media (Task 9.10).

Validates that the actual object bytes match the declared category and
content type using file signatures (magic bytes) only — never trusting
the client-provided filename, extension, or content-type header alone.

Security rules enforced here:
  - A valid filename/extension with invalid bytes must FAIL.
  - A correct content-type header with invalid bytes must FAIL.
  - Only a bounded prefix of the object is read — full objects are
    never buffered into memory (CCTV recordings are large).
  - Size limits are enforced per category.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from contracts.media.models import MediaCategory

# Number of leading bytes read from the object for signature inspection.
# Large enough for every signature below, small enough to be cheap.
VALIDATION_PREFIX_BYTES = 512

# Per-category size ceilings (bytes). Objects larger than the ceiling
# for their category are rejected — prevents absurd uploads.
_SIZE_LIMITS_BYTES: dict[MediaCategory, int] = {
    MediaCategory.RECORDINGS: 50 * 1024 * 1024 * 1024,  # 50 GiB
    MediaCategory.EVIDENCE: 512 * 1024 * 1024,  # 512 MiB
    MediaCategory.REPORTS: 512 * 1024 * 1024,  # 512 MiB
    MediaCategory.ANALYTICS: 4 * 1024 * 1024 * 1024,  # 4 GiB
    MediaCategory.TEMPORARY: 4 * 1024 * 1024 * 1024,  # 4 GiB
}

# MIME types that cannot be spoofed away from their magic bytes.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"
_GZIP_MAGIC = b"\x1f\x8b"
_MP4_FTYP_OFFSET = 4


class ContentFormat(StrEnum):
    """Detected container/codec formats the validator understands."""

    MP4 = "mp4"
    JPEG = "jpeg"
    PNG = "png"
    PDF = "pdf"
    JSON = "json"
    GZIP_JSON = "gzip-json"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContentValidationResult:
    """Outcome of validating a media object's leading bytes."""

    valid: bool
    detected_format: ContentFormat
    reason: str | None = None


def detect_format(header: bytes) -> ContentFormat:
    """Identify the container format from magic bytes (bounded prefix)."""
    if len(header) >= 8 and header[:8] == _PNG_MAGIC:
        return ContentFormat.PNG
    if len(header) >= 3 and header[:3] == _JPEG_MAGIC:
        return ContentFormat.JPEG
    if len(header) >= 5 and header[:5] == _PDF_MAGIC:
        return ContentFormat.PDF
    if len(header) >= 2 and header[:2] == _GZIP_MAGIC:
        return ContentFormat.GZIP_JSON
    if len(header) >= 8 and header[_MP4_FTYP_OFFSET : _MP4_FTYP_OFFSET + 4] == b"ftyp":
        return ContentFormat.MP4
    stripped = header.lstrip(b"\xef\xbb\xbf \t\r\n")  # BOM + whitespace
    # Only the first significant byte is authoritative — never attempt a
    # full JSON parse of a truncated prefix (valid large JSON documents
    # would be falsely rejected).
    if stripped[:1] in (b"{", b"["):
        return ContentFormat.JSON
    return ContentFormat.UNKNOWN


def _content_type_format(content_type: str) -> ContentFormat | None:
    """Map a declared MIME type to an expected format (None = untyped)."""
    ctype = content_type.lower().strip().split(";", 1)[0].strip()
    mapping = {
        "video/mp4": ContentFormat.MP4,
        "video/quicktime": ContentFormat.MP4,  # .mov containers also use ftyp
        "image/jpeg": ContentFormat.JPEG,
        "image/jpg": ContentFormat.JPEG,
        "image/png": ContentFormat.PNG,
        "application/pdf": ContentFormat.PDF,
        "application/json": ContentFormat.JSON,
        "application/gzip": ContentFormat.GZIP_JSON,
        "application/x-gzip": ContentFormat.GZIP_JSON,
    }
    if ctype in mapping:
        return mapping[ctype]
    if "json" in ctype and "gzip" in ctype:
        return ContentFormat.GZIP_JSON
    return None


def _allowed_formats(category: MediaCategory) -> frozenset[ContentFormat]:
    """Formats acceptable for a storage category (policy)."""
    if category == MediaCategory.RECORDINGS:
        return frozenset({ContentFormat.MP4})
    if category == MediaCategory.EVIDENCE:
        return frozenset({ContentFormat.JPEG, ContentFormat.PNG})
    if category == MediaCategory.REPORTS:
        return frozenset({ContentFormat.PDF})
    if category == MediaCategory.ANALYTICS:
        return frozenset({ContentFormat.JSON, ContentFormat.GZIP_JSON})
    # TEMPORARY — intentionally permissive; content is validated at
    # promotion time once the true category is known.
    return frozenset(ContentFormat)


def validate_content(
    category: MediaCategory,
    content_type: str,
    header: bytes,
    *,
    size_bytes: int,
) -> ContentValidationResult:
    """Validate uploaded content against category, MIME, and size policy.

    Args:
        category: The storage category the media was registered under.
        content_type: The declared MIME type (never trusted alone).
        header: The bounded leading bytes of the stored object.
        size_bytes: The provider-reported object size.

    Returns:
        A ContentValidationResult — valid only when the magic bytes match
        both the declared MIME type and the category's allowed formats,
        and the size is within the category ceiling.
    """
    if not header:
        return ContentValidationResult(
            valid=False,
            detected_format=ContentFormat.UNKNOWN,
            reason="object is empty",
        )

    detected = detect_format(header)

    if detected == ContentFormat.UNKNOWN:
        return ContentValidationResult(
            valid=False,
            detected_format=detected,
            reason="file signature does not match any supported format",
        )

    allowed = _allowed_formats(category)
    if detected not in allowed:
        return ContentValidationResult(
            valid=False,
            detected_format=detected,
            reason=(f"format {detected.value!r} is not allowed for category {category.value!r}"),
        )

    declared = _content_type_format(content_type)
    if declared is not None and declared != detected:
        return ContentValidationResult(
            valid=False,
            detected_format=detected,
            reason=(
                f"declared content type {content_type!r} does not match "
                f"detected format {detected.value!r}"
            ),
        )

    ceiling = _SIZE_LIMITS_BYTES[category]
    if size_bytes > ceiling:
        return ContentValidationResult(
            valid=False,
            detected_format=detected,
            reason=(f"size {size_bytes} exceeds the {category.value} ceiling of {ceiling} bytes"),
        )

    return ContentValidationResult(valid=True, detected_format=detected)
