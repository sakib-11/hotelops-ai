"""Unit tests for bounded media content validation (Task 9.10).

Security scenarios: MIME spoofing, extension spoofing, mismatched
declared type, malformed content, size ceilings, and empty objects.
"""

from __future__ import annotations

import pytest

from backend.app.domain.media.validation import (
    VALIDATION_PREFIX_BYTES,
    ContentFormat,
    detect_format,
    validate_content,
)
from contracts.media.models import MediaCategory

MP4_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF_HEADER = b"%PDF-1.7\n" + b"\x00" * 64
JSON_HEADER = b'{"tenant_id": "abc"}' + b" " * 64
GZIP_HEADER = b"\x1f\x8b\x08\x00" + b"\x00" * 64


class TestDetectFormat:
    """Magic-byte signature detection."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (MP4_HEADER, ContentFormat.MP4),
            (JPEG_HEADER, ContentFormat.JPEG),
            (PNG_HEADER, ContentFormat.PNG),
            (PDF_HEADER, ContentFormat.PDF),
            (JSON_HEADER, ContentFormat.JSON),
            (GZIP_HEADER, ContentFormat.GZIP_JSON),
        ],
    )
    def test_detects_known_formats(self, header: bytes, expected: ContentFormat) -> None:
        assert detect_format(header) == expected

    def test_unknown_bytes_rejected(self) -> None:
        assert detect_format(b"random non-media bytes") == ContentFormat.UNKNOWN


class TestContentValidation:
    """Category + MIME + size policy enforcement."""

    def test_valid_recording(self) -> None:
        result = validate_content(
            MediaCategory.RECORDINGS, "video/mp4", MP4_HEADER, size_bytes=1024
        )
        assert result.valid is True
        assert result.detected_format == ContentFormat.MP4

    def test_valid_evidence_jpeg(self) -> None:
        result = validate_content(
            MediaCategory.EVIDENCE, "image/jpeg", JPEG_HEADER, size_bytes=1024
        )
        assert result.valid is True

    def test_valid_report_pdf(self) -> None:
        result = validate_content(
            MediaCategory.REPORTS, "application/pdf", PDF_HEADER, size_bytes=1024
        )
        assert result.valid is True

    def test_extension_spoof_rejected(self) -> None:
        # .mp4 category with JPEG magic bytes — must fail despite a
        # plausible "video/mp4" declared content type.
        result = validate_content(
            MediaCategory.RECORDINGS, "video/mp4", JPEG_HEADER, size_bytes=1024
        )
        assert result.valid is False
        assert "not allowed" in (result.reason or "")

    def test_mime_spoof_rejected(self) -> None:
        # Valid PNG bytes but a lying "image/jpeg" content type header.
        result = validate_content(MediaCategory.EVIDENCE, "image/jpeg", PNG_HEADER, size_bytes=1024)
        assert result.valid is False
        assert "does not match" in (result.reason or "")

    def test_unknown_content_rejected(self) -> None:
        result = validate_content(
            MediaCategory.EVIDENCE, "image/jpeg", b"definitely not an image", size_bytes=1024
        )
        assert result.valid is False

    def test_empty_object_rejected(self) -> None:
        result = validate_content(MediaCategory.EVIDENCE, "image/jpeg", b"", size_bytes=0)
        assert result.valid is False
        assert "empty" in (result.reason or "")

    def test_size_ceiling_enforced(self) -> None:
        over_limit = 600 * 1024 * 1024  # > 512 MiB evidence ceiling
        result = validate_content(
            MediaCategory.EVIDENCE, "image/jpeg", JPEG_HEADER, size_bytes=over_limit
        )
        assert result.valid is False
        assert "ceiling" in (result.reason or "")

    def test_temporary_category_is_permissive(self) -> None:
        # Temporary buffers accept any recognized format.
        result = validate_content(
            MediaCategory.TEMPORARY, "application/octet-stream", PDF_HEADER, size_bytes=1024
        )
        assert result.valid is True

    def test_large_json_document_passes(self) -> None:
        # A valid analytics JSON artifact that is far larger than the
        # signature prefix must NOT be rejected (regression: only the
        # first significant byte is authoritative, never a truncated parse).
        header = b'{"tenant_id": "abc", "payload": ' + b"x" * 8192
        result = validate_content(
            MediaCategory.ANALYTICS, "application/json", header, size_bytes=1_000_000
        )
        assert result.valid is True
        assert result.detected_format == ContentFormat.JSON


class TestBoundedPrefix:
    """The validator only ever needs a bounded prefix."""

    def test_prefix_constant_is_small(self) -> None:
        assert VALIDATION_PREFIX_BYTES <= 4096
