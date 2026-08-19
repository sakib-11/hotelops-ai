"""Retention policy registry for media lifecycle (Task 9.12).

Retention is policy-driven and centralized — services never hard-code
durations. Each approved retention class maps to a fixed duration; the
``legal_hold`` class never expires and is exempt from policy deletion.

Deletion eligibility rules:
  - A media record is deletable by policy when it has an ``expires_at``
    that has passed (swept by the cleanup worker).
  - Evidence under preservation (``legal_hold`` class, or metadata flag
    ``preservation_hold == "true"``) is NEVER deleted by policy or by
    user-initiated deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar

from contracts.media.models import MediaCategory

# Metadata key an operator may set to place preservation on a record.
PRESERVATION_HOLD_KEY = "preservation_hold"
PRESERVATION_HOLD_TRUE = "true"

# Policy duration for records whose retention_class is not configured.
_DEFAULT_DURATION = timedelta(days=90)


@dataclass(frozen=True)
class RetentionPolicy:
    """An approved, centrally-defined retention policy."""

    retention_class: str
    description: str
    duration: timedelta
    protected: bool = False


class RetentionPolicyRegistry:
    """Central registry of approved retention policies."""

    # Approved classes (project policy — extend only via review).
    POLICIES: ClassVar[dict[str, RetentionPolicy]] = {
        "cctv_30_days": RetentionPolicy(
            retention_class="cctv_30_days",
            description="Standard CCTV recording retention (30 days)",
            duration=timedelta(days=30),
        ),
        "evidence_365_days": RetentionPolicy(
            retention_class="evidence_365_days",
            description="Visual evidence retention (365 days)",
            duration=timedelta(days=365),
        ),
        "report_730_days": RetentionPolicy(
            retention_class="report_730_days",
            description="Operational/executive report retention (730 days)",
            duration=timedelta(days=730),
        ),
        "analytics_90_days": RetentionPolicy(
            retention_class="analytics_90_days",
            description="Analytical artifact retention (90 days)",
            duration=timedelta(days=90),
        ),
        "standard_90_days": RetentionPolicy(
            retention_class="standard_90_days",
            description="Default retention (90 days)",
            duration=_DEFAULT_DURATION,
        ),
        # Preservation class — never expires, never policy-deleted.
        "legal_hold": RetentionPolicy(
            retention_class="legal_hold",
            description="Legal/preservation hold — indefinite retention",
            duration=timedelta.max,
            protected=True,
        ),
    }

    # Per-category default classes when the caller supplies none.
    CATEGORY_DEFAULTS: ClassVar[dict[MediaCategory, str]] = {
        MediaCategory.RECORDINGS: "cctv_30_days",
        MediaCategory.EVIDENCE: "evidence_365_days",
        MediaCategory.REPORTS: "report_730_days",
        MediaCategory.ANALYTICS: "analytics_90_days",
        MediaCategory.TEMPORARY: "standard_90_days",
    }

    @classmethod
    def resolve_class(
        cls,
        category: MediaCategory,
        retention_class: str | None,
    ) -> str:
        """Return the effective retention class for a media registration.

        Unknown caller-supplied classes fall back to the category
        default (fail-safe: a typo must never disable retention).
        """
        if retention_class is not None and retention_class in cls.POLICIES:
            return retention_class
        return cls.CATEGORY_DEFAULTS[category]

    @classmethod
    def duration_for(cls, retention_class: str) -> timedelta | None:
        """The policy duration for a class (None = indefinite/preserved)."""
        policy = cls.POLICIES.get(retention_class)
        if policy is None or policy.duration == timedelta.max:
            return None
        return policy.duration

    @classmethod
    def is_protected(
        cls,
        retention_class: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """True when the record must never be deleted.

        Protection applies to the legal-hold class OR an explicit
        preservation flag in the record metadata (defense in depth).
        """
        if retention_class == "legal_hold":
            return True
        if metadata:
            hold = metadata.get(PRESERVATION_HOLD_KEY)
            if isinstance(hold, str) and hold.lower() == PRESERVATION_HOLD_TRUE:
                return True
            if hold is True:
                return True
        return False

    @classmethod
    def is_known_class(cls, retention_class: str) -> bool:
        """True when the class is an approved policy class."""
        return retention_class in cls.POLICIES
