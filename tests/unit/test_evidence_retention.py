"""Tests for Task 17.9 — evidence retention.

Connects the evidence lifecycle to the Task 9 retention policy:

- active evidence: ACTIVE, accessible, never deleted;
- expired evidence: EXPIRED, NOT accessible (no signed URLs), eligible
  for the two-phase deletion workflow;
- repeated cleanup: idempotent — DELETED is terminal, no re-deletion;
- failed deletion: storage failure leaves DELETION_PENDING (audited),
  never a false DELETED;
- retry: the next cleanup call retries and reaches DELETED;
- unauthorized deletion: EVIDENCE_MANAGE required (Task 17.8 policy);
- expired signed access: expired evidence cannot mint signed URLs;
- metadata/object consistency: lifecycle metadata and the storage
  object agree after every path (protected → object retained,
  deleted → object gone).

The policy layer is PURE and deterministic (``now`` injected); the
workflow is tested against the fake storage adapter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.app.application.services.evidence_retention import (
    EVENT_EVIDENCE_ACCESS_DENIED_EXPIRED,
    EVENT_EVIDENCE_CLEANUP_FAILED,
    EVENT_EVIDENCE_DELETED,
    EVENT_EVIDENCE_DELETION_REQUESTED,
    EvidenceRetentionAuditRecord,
    EvidenceRetentionService,
)
from backend.app.domain.evidence.retention import (
    EVIDENCE_DELETED_AT_KEY,
    EVIDENCE_EXPIRES_AT_KEY,
    EVIDENCE_LIFECYCLE_STATE_KEY,
    EVIDENCE_RETENTION_CLASS_KEY,
    EvidenceLifecycleState,
)
from backend.app.infrastructure.auth.evidence import EvidenceAuthorizer
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.storage.exceptions import StorageError
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from contracts.common import (
    CameraId,
    EventId,
    EvidenceId,
    TenantId,
    VenueId,
    VideoSessionId,
)
from contracts.identity import ActorContext, Permission, RoleName, permissions_for_role

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("60000000-0000-0000-0000-000000000001"))

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _actor(
    *,
    tenant_id: TenantId = _TENANT,
    venue_scope: set[VenueId] | None = None,
    role: RoleName = RoleName.ADMIN,
    permissions: frozenset[Permission] | None = None,
) -> ActorContext:
    scope = venue_scope if venue_scope is not None else {_VENUE}
    return ActorContext(
        actor_id=uuid.UUID("70000000-0000-0000-0000-000000000001"),
        tenant_id=tenant_id,
        role_name=role,
        permissions=permissions or permissions_for_role(role),
        venue_scope=frozenset(scope),
        authenticated_at=_NOW,
        active=True,
    )


def _make_ref(
    *,
    ref_id: EvidenceId = _REF,
    tenant_id: TenantId = _TENANT,
    venue_id: VenueId = _VENUE,
    created_at: datetime = _NOW - timedelta(days=400),
    captured_at: datetime | None = None,
    retention_class: str | None = None,
    expires_at: str | None = None,
    lifecycle_state: str | None = None,
    deleted_at: str | None = None,
    object_key: str | None = None,
) -> EvidenceRefModel:
    """An evidence ref whose created_at predates the 365-day policy.

    Defaults: created 400 days ago, no recorded class → the approved
    evidence default (evidence_365_days) → already EXPIRED at _NOW.
    """
    key = object_key or f"tenants/{tenant_id}/venues/{venue_id}/evidence/{ref_id}.mp4"
    metadata: dict[str, Any] = {}
    if retention_class is not None:
        metadata[EVIDENCE_RETENTION_CLASS_KEY] = retention_class
    if expires_at is not None:
        metadata[EVIDENCE_EXPIRES_AT_KEY] = expires_at
    if lifecycle_state is not None:
        metadata[EVIDENCE_LIFECYCLE_STATE_KEY] = lifecycle_state
    if deleted_at is not None:
        metadata[EVIDENCE_DELETED_AT_KEY] = deleted_at
    return EvidenceRefModel(
        ref_id=uuid.UUID(str(ref_id)),
        schema_version="1.0",
        tenant_id=uuid.UUID(str(tenant_id)),
        venue_id=uuid.UUID(str(venue_id)),
        ref_type="video_clip",
        ref_uri=key,
        event_id=uuid.UUID(str(_EVENT)),
        event_time=_NOW,
        session_id=uuid.UUID(str(_SESSION)),
        camera_id=uuid.UUID(str(_CAMERA)),
        metadata_=metadata or None,
        captured_at=captured_at,
        created_at=created_at,
    )


class _AuditRecorder:
    """Records retention audit records for assertions."""

    def __init__(self) -> None:
        self.records: list[EvidenceRetentionAuditRecord] = []

    async def record(self, record: EvidenceRetentionAuditRecord) -> None:
        self.records.append(record)

    @property
    def events(self) -> list[str]:
        return [r.event_type for r in self.records]


def _service(
    storage: FakeStorageAdapter,
    *,
    audit: _AuditRecorder | None = None,
    now: datetime = _NOW,
) -> EvidenceRetentionService:
    return EvidenceRetentionService(
        storage,
        authorizer=EvidenceAuthorizer(),
        audit_sink=audit.record if audit is not None else None,
        now=lambda: now,
    )


async def _seed_object(storage: FakeStorageAdapter, key: str) -> None:
    payload = b"evidence-bytes"
    await storage.put_object_stream(
        key,
        _stream(payload),
        content_type="video/mp4",
        size_bytes=len(payload),
    )


async def _stream(payload: bytes) -> Any:
    yield payload


# =============================================================================
# 1. Active evidence
# =============================================================================


async def test_active_evidence_not_expired_and_accessible() -> None:
    ref = _make_ref(created_at=_NOW - timedelta(days=30))
    storage = FakeStorageAdapter()
    service = _service(storage)
    status = service.evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.ACTIVE
    assert status.is_access_allowed is True
    assert status.is_deletion_eligible is False
    assert status.expires_at is not None

    # Active evidence may be signed.
    await service.assert_signed_url_allowed(_actor(), ref, now=_NOW)

    # Active evidence is never deleted by cleanup.
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False


async def test_active_evidence_retention_class_defaults_to_approved() -> None:
    ref = _make_ref(created_at=_NOW - timedelta(days=30))
    status = _service(FakeStorageAdapter()).evaluate(ref, now=_NOW)
    assert status.retention_class == "evidence_365_days"
    assert status.expires_at == ref.created_at + timedelta(days=365)


# =============================================================================
# 2. Expired evidence
# =============================================================================


async def test_expired_evidence_state_and_inaccessibility() -> None:
    ref = _make_ref()  # created 400 days ago → EXPIRED at _NOW
    service = _service(FakeStorageAdapter())
    status = service.evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.EXPIRED
    assert status.is_access_allowed is False
    assert status.is_deletion_eligible is True


async def test_expired_evidence_cannot_mint_signed_url() -> None:
    ref = _make_ref()
    service = _service(FakeStorageAdapter())
    with pytest.raises(AuthorizationError, match="not accessible"):
        await service.assert_signed_url_allowed(_actor(), ref, now=_NOW)


async def test_expired_evidence_signed_url_denial_is_audited() -> None:
    ref = _make_ref()
    audit = _AuditRecorder()
    service = _service(FakeStorageAdapter(), audit=audit)
    with pytest.raises(AuthorizationError):
        await service.assert_signed_url_allowed(_actor(), ref, now=_NOW)
    assert EVENT_EVIDENCE_ACCESS_DENIED_EXPIRED in audit.events


async def test_expired_evidence_is_deleted_with_object() -> None:
    ref = _make_ref()
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    service = _service(storage)
    assert await service.delete_expired(_actor(), ref, now=_NOW) is True

    # State metadata is terminal DELETED with deleted_at.
    assert ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETED.value
    assert EVIDENCE_DELETED_AT_KEY in ref.metadata_
    # The storage object is gone (metadata/object consistency).
    assert await storage.object_exists(ref.ref_uri) is False


# =============================================================================
# 3. Repeated cleanup — idempotent
# =============================================================================


async def test_repeated_cleanup_is_idempotent() -> None:
    ref = _make_ref()
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    service = _service(storage)
    assert await service.delete_expired(_actor(), ref, now=_NOW) is True
    # A second cleanup call is a no-op — DELETED is terminal.
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False
    assert ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETED.value


async def test_already_deleted_ref_never_resurrected() -> None:
    ref = _make_ref(
        lifecycle_state=EvidenceLifecycleState.DELETED.value, deleted_at=_NOW.isoformat()
    )
    service = _service(FakeStorageAdapter())
    status = service.evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.DELETED
    assert status.is_deletion_eligible is False
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False


# =============================================================================
# 4. Failed deletion
# =============================================================================


async def test_failed_deletion_leaves_pending_and_is_audited() -> None:
    ref = _make_ref()
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)

    class _BrokenStorage(FakeStorageAdapter):
        async def delete_object(self, object_key: str) -> bool:
            raise StorageError("simulated storage failure")

    broken = _BrokenStorage()
    await _seed_object(broken, ref.ref_uri)
    audit = _AuditRecorder()
    service = _service(broken, audit=audit)
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False

    # The ref is DELETION_PENDING — never a false DELETED.
    assert (
        ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETION_PENDING.value
    )
    assert EVENT_EVIDENCE_DELETION_REQUESTED in audit.events
    assert EVENT_EVIDENCE_CLEANUP_FAILED in audit.events
    # The object still exists (not deleted).
    assert await broken.object_exists(ref.ref_uri) is True


# =============================================================================
# 5. Retry
# =============================================================================


async def test_retry_after_failed_delete_reaches_deleted() -> None:
    ref = _make_ref()
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    audit = _AuditRecorder()

    fail_once = {"failed": False}

    class _FlakyStorage(FakeStorageAdapter):
        async def delete_object(self, object_key: str) -> bool:
            if not fail_once["failed"]:
                fail_once["failed"] = True
                raise StorageError("transient failure")
            return await super().delete_object(object_key)

    flaky = _FlakyStorage()
    await _seed_object(flaky, ref.ref_uri)
    service = _service(flaky, audit=audit)

    # First attempt fails → DELETION_PENDING.
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False
    assert (
        ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETION_PENDING.value
    )

    # Retry succeeds → DELETED, object gone.
    assert await service.delete_expired(_actor(), ref, now=_NOW) is True
    assert ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETED.value
    assert await flaky.object_exists(ref.ref_uri) is False
    assert EVENT_EVIDENCE_DELETED in audit.events


async def test_retry_does_not_duplicate_deletion_requested_audit() -> None:
    ref = _make_ref()
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    audit = _AuditRecorder()
    fail_once = {"failed": False}

    class _FlakyStorage(FakeStorageAdapter):
        async def delete_object(self, object_key: str) -> bool:
            if not fail_once["failed"]:
                fail_once["failed"] = True
                raise StorageError("transient failure")
            return await super().delete_object(object_key)

    flaky = _FlakyStorage()
    await _seed_object(flaky, ref.ref_uri)
    service = _service(flaky, audit=audit)

    await service.delete_expired(_actor(), ref, now=_NOW)
    await service.delete_expired(_actor(), ref, now=_NOW)

    # The deletion was requested once; the retry transitions straight to
    # DELETED without re-requesting (idempotent audit).
    assert audit.events.count(EVENT_EVIDENCE_DELETION_REQUESTED) == 1


# =============================================================================
# 6. Unauthorized deletion
# =============================================================================


async def test_operator_cannot_delete_evidence() -> None:
    ref = _make_ref()
    service = _service(FakeStorageAdapter())
    with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
        await service.delete_expired(_actor(role=RoleName.OPERATOR), ref, now=_NOW)


async def test_cross_tenant_deletion_denied() -> None:
    ref = _make_ref()
    service = _service(FakeStorageAdapter())
    other = _actor(tenant_id=TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001")))
    with pytest.raises(AuthorizationError, match="Tenant mismatch"):
        await service.delete_expired(other, ref, now=_NOW)


async def test_cross_venue_deletion_denied() -> None:
    ref = _make_ref()
    service = _service(FakeStorageAdapter())
    other = _actor(venue_scope={VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001"))})
    with pytest.raises(AuthorizationError, match="No access to venue"):
        await service.delete_expired(other, ref, now=_NOW)


async def test_cross_tenant_signed_url_denied() -> None:
    ref = _make_ref(created_at=_NOW - timedelta(days=30))  # active, so the retention gate passes
    service = _service(FakeStorageAdapter())
    other = _actor(tenant_id=TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001")))
    with pytest.raises(AuthorizationError, match="Tenant mismatch"):
        await service.assert_signed_url_allowed(other, ref, now=_NOW)


# =============================================================================
# 7. Expired signed access
# =============================================================================


async def test_expired_signed_access_denied_even_for_admin() -> None:
    ref = _make_ref()
    service = _service(FakeStorageAdapter())
    with pytest.raises(AuthorizationError, match="not accessible"):
        await service.assert_signed_url_allowed(_actor(role=RoleName.ADMIN), ref, now=_NOW)


async def test_preserved_evidence_signed_access_allowed() -> None:
    ref = _make_ref(retention_class="legal_hold", created_at=_NOW - timedelta(days=400))
    service = _service(FakeStorageAdapter())
    status = service.evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.PRESERVED
    assert status.is_access_allowed is True
    await service.assert_signed_url_allowed(_actor(), ref, now=_NOW)


# =============================================================================
# 8. Metadata/object consistency
# =============================================================================


async def test_protected_evidence_object_never_deleted() -> None:
    ref = _make_ref(retention_class="legal_hold", created_at=_NOW - timedelta(days=400))
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    service = _service(storage)

    status = service.evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.PRESERVED
    assert status.is_deletion_eligible is False
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False
    # The object survives — never deleted merely because time passed.
    assert await storage.object_exists(ref.ref_uri) is True


async def test_preservation_hold_flag_protects_evidence() -> None:
    ref = _make_ref(created_at=_NOW - timedelta(days=400))
    ref.metadata_ = {**(ref.metadata_ or {}), "preservation_hold": "true"}
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    service = _service(storage)
    status = service.evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.PRESERVED
    assert await service.delete_expired(_actor(), ref, now=_NOW) is False
    assert await storage.object_exists(ref.ref_uri) is True


async def test_deleted_evidence_object_is_consistent_with_metadata() -> None:
    ref = _make_ref()
    storage = FakeStorageAdapter()
    await _seed_object(storage, ref.ref_uri)
    service = _service(storage)
    await service.delete_expired(_actor(), ref, now=_NOW)

    # Metadata says DELETED, object is gone — consistency holds.
    assert ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETED.value
    assert await storage.object_exists(ref.ref_uri) is False
    # The ref evaluates as terminal DELETED (idempotent, consistent).
    assert service.evaluate(ref, now=_NOW).state is EvidenceLifecycleState.DELETED


async def test_missing_object_delete_still_marks_deleted() -> None:
    # The object is already gone (e.g. purged) — delete_object is
    # idempotent; the ref still reaches a consistent DELETED.
    ref = _make_ref()
    storage = FakeStorageAdapter()  # object never seeded
    service = _service(storage)
    assert await service.delete_expired(_actor(), ref, now=_NOW) is True
    assert ref.metadata_[EVIDENCE_LIFECYCLE_STATE_KEY] == EvidenceLifecycleState.DELETED.value


# =============================================================================
# Deadline semantics
# =============================================================================


async def test_deadline_derived_from_approved_policy_not_metadata() -> None:
    # A ref whose metadata claims a far-future expiry is still governed
    # by the approved policy deadline (created + 365d), not the claim.
    ref = _make_ref(
        created_at=_NOW - timedelta(days=400),
        expires_at=(_NOW + timedelta(days=999)).isoformat(),
    )
    status = _service(FakeStorageAdapter()).evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.EXPIRED  # policy deadline passed


async def test_unknown_retention_class_falls_back_to_approved() -> None:
    ref = _make_ref(retention_class="not_a_real_class", created_at=_NOW - timedelta(days=30))
    status = _service(FakeStorageAdapter()).evaluate(ref, now=_NOW)
    assert status.retention_class == "evidence_365_days"
    assert status.state is EvidenceLifecycleState.ACTIVE


async def test_legal_hold_has_no_deadline() -> None:
    ref = _make_ref(retention_class="legal_hold")
    status = _service(FakeStorageAdapter()).evaluate(ref, now=_NOW)
    assert status.expires_at is None
    assert status.state is EvidenceLifecycleState.PRESERVED


# =============================================================================
# Boundary: exact deadline
# =============================================================================


async def test_exact_deadline_is_expired() -> None:
    # Boundary: now == expires_at → EXPIRED (>= semantics, matching the
    # media policy: expires_at <= now is deletable).
    ref = _make_ref(created_at=_NOW - timedelta(days=365))
    status = _service(FakeStorageAdapter()).evaluate(ref, now=_NOW)
    assert status.expires_at == _NOW
    assert status.state is EvidenceLifecycleState.EXPIRED


async def test_one_second_before_deadline_is_active() -> None:
    ref = _make_ref(created_at=_NOW - timedelta(days=365) + timedelta(seconds=1))
    status = _service(FakeStorageAdapter()).evaluate(ref, now=_NOW)
    assert status.state is EvidenceLifecycleState.ACTIVE
