"""Evidence retention policy (Task 17.9).

Connects the evidence lifecycle to the Task 9 retention policy without
duplicating it. ``RetentionPolicyRegistry`` is the single source of
truth for approved classes, durations, and protection — this module
resolves the EVIDENCE category through it and adds the evidence-specific
state machine:

    ACTIVE ──policy deadline passed──▶ EXPIRED
      │                                   │  (cleanup worker)
      │                                   ▼
      │                            DELETION_PENDING ──object deleted──▶ DELETED
      │
      └── legal_hold / preservation_hold ──▶ PRESERVED (never expires,
                                              never policy-deleted)

Deterministic rules:

- The retention deadline is computed from the APPROVED policy duration
  (``RetentionPolicyRegistry.duration_for``), never from arbitrary
  metadata: evidence is NOT deleted merely because some metadata field
  looks old — only when the approved policy's deadline has passed.
- ``legal_hold`` (and any record flagged ``preservation_hold``) is
  PRESERVED: no deadline, never eligible for deletion, still accessible.
- EXPIRED evidence is NOT accessible (no signed URLs) and IS eligible
  for the two-phase deletion workflow.
- DELETED evidence is terminal and never re-deleted (idempotency).

The module is PURE and deterministic: every evaluation takes ``now``
explicitly (event/policy time), no wall clock, no database, no network.
Lifecycle state is recorded in the evidence ref's variable metadata
(JSONB) — the evidence model's documented JSONB policy — via the
canonical keys below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from backend.app.domain.media.retention import RetentionPolicyRegistry
from contracts.media.models import MediaCategory

__all__ = [
    "EVIDENCE_DELETED_AT_KEY",
    "EVIDENCE_EXPIRES_AT_KEY",
    "EVIDENCE_LIFECYCLE_STATE_KEY",
    "EVIDENCE_RETENTION_CLASS_KEY",
    "EvidenceLifecycleState",
    "EvidenceRetentionPolicy",
    "EvidenceRetentionStatus",
]

# Canonical metadata keys on an evidence ref's variable metadata (JSONB).
EVIDENCE_RETENTION_CLASS_KEY = "retention_class"
EVIDENCE_EXPIRES_AT_KEY = "expires_at"
EVIDENCE_LIFECYCLE_STATE_KEY = "lifecycle_state"
EVIDENCE_DELETED_AT_KEY = "deleted_at"

# The approved default class for evidence artifacts (Task 9 category
# default — never hard-coded as a duration).
_DEFAULT_EVIDENCE_CLASS = "evidence_365_days"


class EvidenceLifecycleState(StrEnum):
    """Deterministic lifecycle state of an evidence ref (Task 17.9)."""

    ACTIVE = "active"
    EXPIRED = "expired"
    PRESERVED = "preserved"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


@dataclass(frozen=True)
class EvidenceRetentionStatus:
    """Deterministic retention evaluation of one evidence ref."""

    retention_class: str
    expires_at: datetime | None  # None = preserved/indefinite
    state: EvidenceLifecycleState
    protected: bool

    @property
    def is_access_allowed(self) -> bool:
        """Expired/deleted evidence must NOT be accessible."""
        return self.state in (EvidenceLifecycleState.ACTIVE, EvidenceLifecycleState.PRESERVED)

    @property
    def is_deletion_eligible(self) -> bool:
        """Only EXPIRED (not preserved, not already deleted) is eligible."""
        return self.state is EvidenceLifecycleState.EXPIRED


class EvidenceRetentionPolicy:
    """Pure evidence retention policy (Task 17.9) over Task 9 registry."""

    # ------------------------------------------------------------------
    # Class resolution
    # ------------------------------------------------------------------

    @classmethod
    def resolve_class(cls, requested_class: str | None) -> str:
        """The approved retention class for evidence.

        Reuses ``RetentionPolicyRegistry.resolve_class`` with the
        EVIDENCE category — unknown classes fall back to the approved
        evidence default; a typo can never disable retention.
        """
        return RetentionPolicyRegistry.resolve_class(
            MediaCategory.EVIDENCE,
            requested_class,
        )

    @classmethod
    def default_class(cls) -> str:
        """The approved default evidence retention class."""
        return _DEFAULT_EVIDENCE_CLASS

    @classmethod
    def is_known_class(cls, retention_class: str) -> bool:
        """True when the class is an approved policy class (Task 9)."""
        return RetentionPolicyRegistry.is_known_class(retention_class)

    # ------------------------------------------------------------------
    # Deadline
    # ------------------------------------------------------------------

    @classmethod
    def deadline_for(
        cls,
        retention_class: str,
        created_at: datetime,
    ) -> datetime | None:
        """The approved-policy deadline for an evidence ref.

        Returns None for preserved/indefinite classes (legal_hold).
        The deadline is derived from the POLICY duration — evidence is
        never expired from arbitrary metadata.
        """
        duration = RetentionPolicyRegistry.duration_for(retention_class)
        if duration is None:
            return None
        return created_at + duration

    # ------------------------------------------------------------------
    # Protection
    # ------------------------------------------------------------------

    @classmethod
    def is_protected(
        cls,
        retention_class: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """True when the evidence must never be policy-deleted.

        Delegates to the Task 9 registry (legal_hold class or an
        explicit ``preservation_hold`` flag) — never duplicated.
        """
        return RetentionPolicyRegistry.is_protected(retention_class, metadata)

    # ------------------------------------------------------------------
    # Evaluation (the deterministic state machine)
    # ------------------------------------------------------------------

    @classmethod
    def evaluate(
        cls,
        *,
        retention_class: str,
        created_at: datetime,
        now: datetime,
        metadata: dict[str, Any] | None = None,
        state: EvidenceLifecycleState | None = None,
        expires_at: datetime | None = None,
    ) -> EvidenceRetentionStatus:
        """Deterministically evaluate an evidence ref's retention state.

        Order:
        1. A terminal DELETED state is preserved (idempotency — never
           resurrected, never re-deleted).
        2. Protected evidence (legal_hold / preservation_hold) is
           PRESERVED regardless of any stored deadline.
        3. Otherwise the state is ACTIVE until the approved-policy
           deadline has passed → EXPIRED.

        ``now`` is the policy/evaluation time (event-time semantics) —
        never wall clock inside the module.
        """
        protected = cls.is_protected(retention_class, metadata)

        if state is EvidenceLifecycleState.DELETED:
            return EvidenceRetentionStatus(
                retention_class=retention_class,
                expires_at=expires_at,
                state=EvidenceLifecycleState.DELETED,
                protected=protected,
            )

        if protected:
            return EvidenceRetentionStatus(
                retention_class=retention_class,
                expires_at=None,
                state=EvidenceLifecycleState.PRESERVED,
                protected=True,
            )

        # The deadline is ALWAYS derived from the approved policy
        # (created_at + policy duration), never from a stored metadata
        # claim: evidence is deleted only when the approved retention
        # policy requires it — a contradictory recorded expires_at is
        # treated as untrusted (the recorded value is audit-only).
        deadline = cls.deadline_for(retention_class, created_at)
        if deadline is not None and now >= deadline:
            return EvidenceRetentionStatus(
                retention_class=retention_class,
                expires_at=deadline,
                state=EvidenceLifecycleState.EXPIRED,
                protected=False,
            )

        # An in-flight deletion (DELETION_PENDING) with a passed deadline
        # remains pending — the cleanup workflow drives it to DELETED.
        if state is EvidenceLifecycleState.DELETION_PENDING:
            return EvidenceRetentionStatus(
                retention_class=retention_class,
                expires_at=deadline,
                state=EvidenceLifecycleState.DELETION_PENDING,
                protected=False,
            )

        return EvidenceRetentionStatus(
            retention_class=retention_class,
            expires_at=deadline,
            state=EvidenceLifecycleState.ACTIVE,
            protected=False,
        )

    # ------------------------------------------------------------------
    # Metadata accessors (the evidence model's JSONB policy)
    # ------------------------------------------------------------------

    @staticmethod
    def read_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        """A safe copy of the evidence variable metadata (never None)."""
        return dict(metadata or {})

    @classmethod
    def retention_class_of(cls, metadata: dict[str, Any] | None) -> str:
        """The recorded retention class (approved default when absent)."""
        recorded = (metadata or {}).get(EVIDENCE_RETENTION_CLASS_KEY)
        return cls.resolve_class(str(recorded) if recorded else None)

    @classmethod
    def expires_at_of(cls, metadata: dict[str, Any] | None) -> datetime | None:
        """The recorded deadline (None when absent/preserved)."""
        value = (metadata or {}).get(EVIDENCE_EXPIRES_AT_KEY)
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    @classmethod
    def state_of(
        cls,
        metadata: dict[str, Any] | None,
    ) -> EvidenceLifecycleState | None:
        """The recorded lifecycle state (None when never recorded)."""
        value = (metadata or {}).get(EVIDENCE_LIFECYCLE_STATE_KEY)
        if value is None:
            return None
        try:
            return EvidenceLifecycleState(str(value))
        except ValueError:
            return None
