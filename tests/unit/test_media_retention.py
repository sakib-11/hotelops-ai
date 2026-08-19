"""Unit tests for the retention policy registry (Task 9.12).

Validates approved classes, deterministic durations, category defaults,
legal-hold protection, and preservation metadata defense-in-depth.
"""

from __future__ import annotations

from datetime import timedelta

from backend.app.domain.media.retention import RetentionPolicyRegistry
from contracts.media.models import MediaCategory


class TestPolicyResolution:
    """Retention class resolution and durations."""

    def test_known_class_kept(self) -> None:
        assert (
            RetentionPolicyRegistry.resolve_class(MediaCategory.RECORDINGS, "cctv_30_days")
            == "cctv_30_days"
        )

    def test_unknown_class_falls_back_to_category_default(self) -> None:
        # A typo must never disable retention — it falls back to the
        # category default instead.
        assert (
            RetentionPolicyRegistry.resolve_class(MediaCategory.RECORDINGS, "bogus_class")
            == "cctv_30_days"
        )
        assert (
            RetentionPolicyRegistry.resolve_class(MediaCategory.EVIDENCE, None)
            == "evidence_365_days"
        )
        assert (
            RetentionPolicyRegistry.resolve_class(MediaCategory.REPORTS, None) == "report_730_days"
        )
        assert (
            RetentionPolicyRegistry.resolve_class(MediaCategory.ANALYTICS, None)
            == "analytics_90_days"
        )

    def test_durations_are_deterministic(self) -> None:
        assert RetentionPolicyRegistry.duration_for("cctv_30_days") == timedelta(days=30)
        assert RetentionPolicyRegistry.duration_for("evidence_365_days") == timedelta(days=365)
        assert RetentionPolicyRegistry.duration_for("report_730_days") == timedelta(days=730)
        assert RetentionPolicyRegistry.duration_for("analytics_90_days") == timedelta(days=90)

    def test_legal_hold_never_expires(self) -> None:
        assert RetentionPolicyRegistry.duration_for("legal_hold") is None

    def test_unknown_class_has_no_duration(self) -> None:
        assert RetentionPolicyRegistry.duration_for("does_not_exist") is None


class TestProtection:
    """Preservation / legal hold protections."""

    def test_legal_hold_class_protected(self) -> None:
        assert RetentionPolicyRegistry.is_protected("legal_hold") is True

    def test_normal_classes_not_protected(self) -> None:
        assert RetentionPolicyRegistry.is_protected("cctv_30_days") is False
        assert RetentionPolicyRegistry.is_protected("evidence_365_days") is False
        assert RetentionPolicyRegistry.is_protected(None) is False

    def test_preservation_metadata_flag_protects(self) -> None:
        assert (
            RetentionPolicyRegistry.is_protected("cctv_30_days", {"preservation_hold": "true"})
            is True
        )
        assert (
            RetentionPolicyRegistry.is_protected("cctv_30_days", {"preservation_hold": "TRUE"})
            is True
        )

    def test_preservation_metadata_false_does_not_protect(self) -> None:
        assert (
            RetentionPolicyRegistry.is_protected("cctv_30_days", {"preservation_hold": "false"})
            is False
        )

    def test_unrelated_metadata_does_not_protect(self) -> None:
        assert RetentionPolicyRegistry.is_protected("cctv_30_days", {"camera": "cam-01"}) is False
