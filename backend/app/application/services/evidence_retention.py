"""Evidence retention workflow (Task 17.9).

Connects the evidence lifecycle to the Task 9 retention policy via the
pure ``EvidenceRetentionPolicy`` domain module and the Task 9 storage +
audit infrastructure:

- Signed-URL gating: expired (or deleted) evidence is NEVER accessible
  through active signed URLs — ``assert_signed_url_allowed`` runs the
  authorization policy AND the retention state check before any signing.
- Two-phase deletion workflow (mirrors the Task 9 media cleanup worker
  semantics): EXPIRED → DELETION_PENDING (atomic, idempotent) → object
  delete (outside any transaction) → DELETED (terminal). A failed
  object delete leaves DELETION_PENDING so the next cleanup cycle
  RETRIES — never skipped, never stuck.
- Idempotency: DELETED is terminal (never re-deleted, never
  resurrected); repeated cleanup of an already-pending/expired ref is a
  no-op; protected evidence (legal_hold / preservation_hold) is never
  deleted by policy.
- Authorization: every deletion is authorized through the canonical
  ``EvidenceAuthorizer`` (EVIDENCE_MANAGE) against the RESOLVED row's
  tenant/venue — never client-supplied scope.
- Audit: every state transition enqueues an audit record through the
  Task 9 audit pipeline (``AuditEventBuilder`` + ``OutboxService``,
  EVIDENCE category, system actor for worker-driven deletes).

The service owns the workflow; the evidence ref's variable metadata
(JSONB) records the canonical lifecycle keys (retention class, deadline,
state, deleted-at) per the evidence model's documented JSONB policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.app.application.services.media_audit import system_actor
from backend.app.domain.evidence.retention import (
    EVIDENCE_DELETED_AT_KEY,
    EVIDENCE_EXPIRES_AT_KEY,
    EVIDENCE_LIFECYCLE_STATE_KEY,
    EvidenceLifecycleState,
    EvidenceRetentionPolicy,
    EvidenceRetentionStatus,
)
from backend.app.infrastructure.audit.context import AuditEventBuilder
from backend.app.infrastructure.auth.evidence import (
    EvidenceAuthorizer,
    EvidenceOperation,
)
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.storage.exceptions import StorageError
from backend.app.infrastructure.storage.protocol import StoragePort
from contracts.audit import AuditActionCategory
from contracts.common import TenantId, VenueId
from contracts.identity import ActorContext

# Audit event types (evidence domain — mirrors the media vocabulary).
EVENT_EVIDENCE_EXPIRED = "evidence.expired"
EVENT_EVIDENCE_DELETION_REQUESTED = "evidence.deletion.requested"
EVENT_EVIDENCE_DELETED = "evidence.deleted"
EVENT_EVIDENCE_CLEANUP_FAILED = "evidence.cleanup.failed"
EVENT_EVIDENCE_ACCESS_DENIED_EXPIRED = "evidence.access.denied.expired"


@dataclass
class EvidenceRetentionAuditRecord:
    """One auditable retention transition (test/observability record)."""

    event_type: str
    ref_id: uuid.UUID
    tenant_id: uuid.UUID
    venue_id: uuid.UUID
    state: str
    reason: str | None = None
    actor_id: uuid.UUID | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# The audit sink: the production implementation enqueues AuditEvent +
# outbox rows (Task 9 pipeline, async); tests inject a recorder.
EvidenceAuditSink = Callable[[EvidenceRetentionAuditRecord], Awaitable[None]]


class EvidenceRetentionService:
    """Authorized, auditable, idempotent evidence retention workflow."""

    def __init__(
        self,
        storage: StoragePort,
        *,
        authorizer: EvidenceAuthorizer | None = None,
        audit_sink: EvidenceAuditSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._authorizer = authorizer or EvidenceAuthorizer()
        self._audit_sink = audit_sink
        self._now = now or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        ref: EvidenceRefModel,
        *,
        now: datetime | None = None,
    ) -> EvidenceRetentionStatus:
        """Deterministically evaluate a ref's retention state."""
        metadata = ref.metadata_
        retention_class = EvidenceRetentionPolicy.retention_class_of(metadata)
        created_at = ref.captured_at if ref.captured_at is not None else ref.created_at
        return EvidenceRetentionPolicy.evaluate(
            retention_class=retention_class,
            created_at=created_at,
            now=now if now is not None else self._now(),
            metadata=metadata,
            state=EvidenceRetentionPolicy.state_of(metadata),
            expires_at=EvidenceRetentionPolicy.expires_at_of(metadata),
        )

    # ------------------------------------------------------------------
    # Signed-URL gating — expired evidence must NOT remain accessible
    # ------------------------------------------------------------------

    async def assert_signed_url_allowed(
        self,
        actor: ActorContext,
        ref: EvidenceRefModel,
        *,
        now: datetime | None = None,
    ) -> None:
        """Refuse signed access to expired/deleted evidence.

        Authorization FIRST (EVIDENCE_READ against the resolved row's
        scope), then the retention gate: only ACTIVE/PRESERVED evidence
        may be signed. This runs at URL generation time, so an expired
        ref can never mint an active signed URL.
        """
        self._authorizer.authorize(
            actor,
            EvidenceOperation.SIGNED_URL,
            _tenant_of(ref),
            _venue_of(ref),
        )
        status = self.evaluate(ref, now=now)
        if not status.is_access_allowed:
            if self._audit_sink is not None:
                await self._audit_sink(
                    EvidenceRetentionAuditRecord(
                        event_type=EVENT_EVIDENCE_ACCESS_DENIED_EXPIRED,
                        ref_id=ref.ref_id,
                        tenant_id=uuid.UUID(str(ref.tenant_id)),
                        venue_id=uuid.UUID(str(ref.venue_id)),
                        state=status.state.value,
                        actor_id=uuid.UUID(str(actor.actor_id)),
                        extra={"retention_class": status.retention_class},
                    )
                )
            msg = (
                f"Evidence {ref.ref_id} is not accessible: state="
                f"{status.state.value} (retention_class={status.retention_class})"
            )
            raise AuthorizationError(msg)

    # ------------------------------------------------------------------
    # Two-phase idempotent deletion workflow
    # ------------------------------------------------------------------

    async def delete_expired(
        self,
        actor: ActorContext,
        ref: EvidenceRefModel,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Delete one expired ref (idempotent, retry-safe, audited).

        Phases:
        1. Authorize (EVIDENCE_MANAGE, resolved row scope) + evaluate —
           only EXPIRED is eligible; protected/DELETED are never touched.
        2. Mark DELETION_PENDING (metadata) — already pending (a prior
           failed delete) is retried, not skipped.
        3. Delete the object from storage (outside any transaction).
        4. Mark DELETED (terminal) + audit.

        Returns True when the ref reached DELETED in this call; False
        when not eligible (no-op) or the delete failed (will retry).
        """
        # Phase 0 — authorization + eligibility (deterministic).
        self._authorizer.authorize(
            actor,
            EvidenceOperation.DELETE,
            _tenant_of(ref),
            _venue_of(ref),
        )
        status = self.evaluate(ref, now=now)
        if not status.is_deletion_eligible:
            return False

        metadata = EvidenceRetentionPolicy.read_metadata(ref.metadata_)
        if (
            EvidenceRetentionPolicy.state_of(metadata)
            is not EvidenceLifecycleState.DELETION_PENDING
        ):
            metadata[EVIDENCE_LIFECYCLE_STATE_KEY] = EvidenceLifecycleState.DELETION_PENDING.value
            metadata[EVIDENCE_EXPIRES_AT_KEY] = (
                status.expires_at.isoformat() if status.expires_at is not None else None
            )
            ref.metadata_ = metadata
            await self._audit(
                EVENT_EVIDENCE_DELETION_REQUESTED,
                ref,
                status,
                actor,
                reason="retention_expiry",
                extra={"trigger": "retention_expiry"},
            )

        # Phase 2 — external object delete (retry-safe).
        try:
            await self._storage.delete_object(ref.ref_uri)
        except StorageError as exc:
            await self._audit(
                EVENT_EVIDENCE_CLEANUP_FAILED,
                ref,
                status,
                actor,
                reason=f"storage delete failed: {exc}",
                extra={"trigger": "retention_expiry"},
            )
            return False

        # Phase 3 — terminal DELETED + audit.
        deleted_at = self._now()
        metadata = EvidenceRetentionPolicy.read_metadata(ref.metadata_)
        metadata[EVIDENCE_LIFECYCLE_STATE_KEY] = EvidenceLifecycleState.DELETED.value
        metadata[EVIDENCE_DELETED_AT_KEY] = deleted_at.isoformat()
        ref.metadata_ = metadata
        await self._audit(
            EVENT_EVIDENCE_DELETED,
            ref,
            status,
            actor,
            reason="retention_expiry",
            extra={"trigger": "retention_expiry", "deleted_at": deleted_at.isoformat()},
        )
        return True

    # ------------------------------------------------------------------

    async def _audit(
        self,
        event_type: str,
        ref: EvidenceRefModel,
        status: EvidenceRetentionStatus,
        actor: ActorContext,
        *,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._audit_sink is None:
            return
        await self._audit_sink(
            EvidenceRetentionAuditRecord(
                event_type=event_type,
                ref_id=ref.ref_id,
                tenant_id=uuid.UUID(str(ref.tenant_id)),
                venue_id=uuid.UUID(str(ref.venue_id)),
                state=status.state.value,
                reason=reason,
                actor_id=uuid.UUID(str(actor.actor_id)),
                extra=extra or {},
            )
        )


def _tenant_of(ref: EvidenceRefModel) -> TenantId:
    return TenantId(ref.tenant_id)


def _venue_of(ref: EvidenceRefModel) -> VenueId:
    return VenueId(ref.venue_id)


def outbox_evidence_audit_sink(
    session: Any,
) -> EvidenceAuditSink:
    """Production audit sink: enqueue AuditEvent + outbox rows.

    The caller's session transaction owns the commit (Task 7 outbox
    contract) — the retention state change, audit row, and outbox row
    are atomic. The sink uses a system actor for worker-driven deletes
    and the EVIDENCE audit category (Task 8).
    """

    from backend.app.application.services.outbox import OutboxService
    from contracts.common import EventId, VenueId
    from contracts.events import EventEnvelope

    async def _record(record: EvidenceRetentionAuditRecord) -> None:
        now = datetime.now(UTC)
        actor = system_actor(record.tenant_id)
        envelope = EventEnvelope[dict[str, Any]](
            event_id=EventId(uuid.uuid4()),
            event_type=record.event_type,
            event_time=now,
            produced_at=now,
            source="hotelops.evidence",
            payload={
                "ref_id": str(record.ref_id),
                "tenant_id": str(record.tenant_id),
                "venue_id": str(record.venue_id),
                "state": record.state,
                **record.extra,
            },
        )
        audit = AuditEventBuilder.from_actor(
            actor=actor,
            action=record.event_type,
            action_category=AuditActionCategory.EVIDENCE,
            venue_id=VenueId(record.venue_id),
            metadata={
                "ref_id": str(record.ref_id),
                "state": record.state,
                **({} if record.reason is None else {"reason": record.reason[:512]}),
            },
        )
        # The outbox service persists both rows atomically in the caller
        # session (Task 7 outbox contract); the returned row is discarded
        # (sink contract).
        await OutboxService().enqueue_event(
            session,
            actor=actor,
            envelope=envelope,
            audit=audit,
            venue_id=record.venue_id,
        )

    return _record
