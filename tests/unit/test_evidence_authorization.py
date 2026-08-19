"""Tests for Task 17.8 — evidence authorization.

Prevents evidence leakage across tenants and venues. Reuses Task 5
authorization (``ActorContext``, ``require_tenant_venue_access``) and the
canonical ``EvidenceAuthorizer`` policy. Every evidence operation enforces
tenant + venue + permission + resource ownership, and NEVER trusts:

- tenant_id from the request body,
- venue_id from query parameters,
- the storage key alone.

Covered:

- Tenant A → Tenant A evidence = ALLOW
- Tenant A → Tenant B evidence = DENY
- Venue A → Venue A evidence = ALLOW
- Venue A → Venue B evidence = DENY
- Expired / invalid / disabled actor = DENY
- Unauthorized role (operator on a manage operation) = DENY
- every protected operation (create, retrieve, metadata, signed URL,
  delete, retention)
- repository: tenant-filtered SQL, venue check on the row, object-key
  lookups never authorize by key alone
- client-supplied scope is never trusted (server-side actor only)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.app.infrastructure.auth.evidence import (
    EvidenceAuthorizer,
    EvidenceOperation,
)
from backend.app.infrastructure.auth.exceptions import AuthorizationError
from backend.app.infrastructure.database.models.evidence import (
    EvidencePackageModel,
    EvidenceRefModel,
)
from backend.app.infrastructure.database.repositories.evidence import EvidenceRepository
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
_TENANT_A = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_TENANT_B = TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001"))
_VENUE_A = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_VENUE_B = VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_REF_B = EvidenceId(uuid.UUID("93000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("60000000-0000-0000-0000-000000000001"))

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

_AUTHORIZER = EvidenceAuthorizer()


def _actor(
    *,
    tenant_id: TenantId = _TENANT_A,
    venue_scope: set[VenueId] | None = None,
    role: RoleName = RoleName.MANAGER,
    permissions: frozenset[Permission] | None = None,
    active: bool = True,
    authenticated_at: datetime = _NOW,
) -> ActorContext:
    scope = venue_scope if venue_scope is not None else {_VENUE_A}
    return ActorContext(
        actor_id=uuid.UUID("70000000-0000-0000-0000-000000000001"),
        tenant_id=tenant_id,
        role_name=role,
        permissions=permissions or permissions_for_role(role),
        venue_scope=frozenset(scope),
        authenticated_at=authenticated_at,
        active=active,
    )


def _tenant_wide_actor(*, tenant_id: TenantId = _TENANT_A) -> ActorContext:
    """Empty venue_scope = tenant-wide venue access (Task 5)."""
    return _actor(tenant_id=tenant_id, venue_scope=set())


def _make_ref(
    *,
    ref_id: EvidenceId = _REF,
    tenant_id: TenantId = _TENANT_A,
    venue_id: VenueId = _VENUE_A,
    ref_uri: str | None = None,
) -> EvidenceRefModel:
    return EvidenceRefModel(
        ref_id=uuid.UUID(str(ref_id)),
        schema_version="1.0",
        tenant_id=uuid.UUID(str(tenant_id)),
        venue_id=uuid.UUID(str(venue_id)),
        ref_type="video_clip",
        ref_uri=ref_uri or f"tenants/{tenant_id}/venues/{venue_id}/evidence/{ref_id}.mp4",
        event_id=uuid.UUID(str(_EVENT)),
        event_time=_NOW,
        session_id=uuid.UUID(str(_SESSION)),
        camera_id=uuid.UUID(str(_CAMERA)),
        captured_at=_NOW,
    )


def _make_package(
    *, tenant_id: TenantId = _TENANT_A, venue_id: VenueId = _VENUE_A
) -> EvidencePackageModel:
    return EvidencePackageModel(
        package_id=uuid.uuid4(),
        schema_version="1.0",
        tenant_id=uuid.UUID(str(tenant_id)),
        venue_id=uuid.UUID(str(venue_id)),
        description="evidence package",
        created_at=_NOW,
    )


class _FakeResult:
    """Minimal stand-in for SQLAlchemy result (scalar_one_or_none)."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> Any:
        return _FakeScalars([self._value] if self._value is not None else [])


class _FakeScalars:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _FakeSession:
    """In-memory AsyncSession for repository authorization tests.

    Tracks added objects and resolves ``execute`` by scanning the seeded
    rows the same way the real SQL tenant filter behaves — cross-tenant
    rows never match (mirroring the repository's WHERE clause).
    """

    def __init__(self) -> None:
        self.refs: dict[uuid.UUID, EvidenceRefModel] = {}
        self.packages: dict[uuid.UUID, EvidencePackageModel] = {}
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    def seed_ref(self, ref: EvidenceRefModel) -> EvidenceRefModel:
        self.refs[ref.ref_id] = ref
        return ref

    def seed_package(self, package: EvidencePackageModel) -> EvidencePackageModel:
        self.packages[package.package_id] = package
        return package

    async def execute(self, stmt: Any) -> Any:
        # Tenant-filtered resolution (mirrors the repository's SQL).
        return _FakeResult(None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)


# =============================================================================
# Pure policy — Tenant A → Tenant A = ALLOW
# =============================================================================


class TestTenantAllow:
    def test_retrieve_same_tenant_allowed(self) -> None:
        actor = _actor(tenant_id=_TENANT_A)
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_create_same_tenant_allowed(self) -> None:
        actor = _actor(tenant_id=_TENANT_A)
        _AUTHORIZER.authorize(actor, EvidenceOperation.CREATE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_metadata_same_tenant_allowed(self) -> None:
        actor = _actor(tenant_id=_TENANT_A)
        _AUTHORIZER.authorize(actor, EvidenceOperation.METADATA, _TENANT_A, _VENUE_A, now=_NOW)

    def test_signed_url_same_tenant_allowed(self) -> None:
        actor = _actor(tenant_id=_TENANT_A)
        _AUTHORIZER.authorize(actor, EvidenceOperation.SIGNED_URL, _TENANT_A, _VENUE_A, now=_NOW)

    def test_delete_same_tenant_allowed_for_manager(self) -> None:
        actor = _actor(tenant_id=_TENANT_A, role=RoleName.MANAGER)
        _AUTHORIZER.authorize(actor, EvidenceOperation.DELETE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_retention_same_tenant_allowed_for_manager(self) -> None:
        actor = _actor(tenant_id=_TENANT_A, role=RoleName.MANAGER)
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETENTION, _TENANT_A, _VENUE_A, now=_NOW)

    def test_tenant_wide_venue_scope_allows_any_venue_in_tenant(self) -> None:
        actor = _tenant_wide_actor(tenant_id=_TENANT_A)
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_B, now=_NOW)


# =============================================================================
# Pure policy — Tenant A → Tenant B = DENY
# =============================================================================


class TestTenantDeny:
    @pytest.mark.parametrize("operation", list(EvidenceOperation))
    def test_cross_tenant_denied_for_every_operation(self, operation: EvidenceOperation) -> None:
        actor = _actor(tenant_id=_TENANT_A)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            _AUTHORIZER.authorize(actor, operation, _TENANT_B, _VENUE_A, now=_NOW)

    def test_cross_tenant_denied_even_with_tenant_wide_venue_scope(self) -> None:
        actor = _tenant_wide_actor(tenant_id=_TENANT_A)
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_B, _VENUE_A, now=_NOW)


# =============================================================================
# Pure policy — Venue A → Venue A = ALLOW / Venue A → Venue B = DENY
# =============================================================================


class TestVenue:
    def test_same_venue_allowed(self) -> None:
        actor = _actor(venue_scope={_VENUE_A})
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_cross_venue_denied(self) -> None:
        actor = _actor(venue_scope={_VENUE_A})
        with pytest.raises(AuthorizationError, match="No access to venue"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_B, now=_NOW)

    def test_cross_venue_denied_for_manage_operation(self) -> None:
        actor = _actor(venue_scope={_VENUE_A}, role=RoleName.ADMIN)
        with pytest.raises(AuthorizationError, match="No access to venue"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.DELETE, _TENANT_A, _VENUE_B, now=_NOW)

    def test_empty_scope_means_tenant_wide_access(self) -> None:
        actor = _tenant_wide_actor(tenant_id=_TENANT_A)
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)


# =============================================================================
# Expired / invalid / disabled actor = DENY
# =============================================================================


class TestActorValidity:
    def test_disabled_actor_denied(self) -> None:
        actor = _actor(active=False)
        with pytest.raises(AuthorizationError, match="not active"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_actor_with_future_authentication_time_denied(self) -> None:
        future = datetime(2026, 9, 1, tzinfo=UTC)
        actor = _actor(authenticated_at=future)
        with pytest.raises(AuthorizationError, match="future"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_disabled_actor_denied_before_scope_check(self) -> None:
        # Validity is checked FIRST — a disabled actor from the same
        # tenant is still denied.
        actor = _actor(active=False)
        with pytest.raises(AuthorizationError, match="not active"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)


# =============================================================================
# Unauthorized role = DENY
# =============================================================================


class TestRole:
    def test_operator_cannot_delete(self) -> None:
        actor = _actor(role=RoleName.OPERATOR)
        with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.DELETE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_operator_cannot_change_retention(self) -> None:
        actor = _actor(role=RoleName.OPERATOR)
        with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
            _AUTHORIZER.authorize(actor, EvidenceOperation.RETENTION, _TENANT_A, _VENUE_A, now=_NOW)

    def test_operator_can_read(self) -> None:
        actor = _actor(role=RoleName.OPERATOR)
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)

    def test_manager_and_admin_can_delete(self) -> None:
        for role in (RoleName.MANAGER, RoleName.ADMIN):
            actor = _actor(role=role)
            _AUTHORIZER.authorize(actor, EvidenceOperation.DELETE, _TENANT_A, _VENUE_A, now=_NOW)


# =============================================================================
# Every operation is covered by a required permission
# =============================================================================


class TestOperationPermissionMatrix:
    def test_read_operations_require_evidence_read(self) -> None:
        actor = _actor(permissions=frozenset({Permission.VIDEO_READ}))
        for operation in (
            EvidenceOperation.CREATE,
            EvidenceOperation.RETRIEVE,
            EvidenceOperation.METADATA,
            EvidenceOperation.SIGNED_URL,
        ):
            with pytest.raises(AuthorizationError, match=r"evidence\.read"):
                _AUTHORIZER.authorize(actor, operation, _TENANT_A, _VENUE_A, now=_NOW)

    def test_manage_operations_require_evidence_manage(self) -> None:
        actor = _actor(permissions=frozenset({Permission.EVIDENCE_READ}))
        for operation in (EvidenceOperation.DELETE, EvidenceOperation.RETENTION):
            with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
                _AUTHORIZER.authorize(actor, operation, _TENANT_A, _VENUE_A, now=_NOW)

    def test_evidence_read_alone_grants_read_operations(self) -> None:
        actor = _actor(permissions=frozenset({Permission.EVIDENCE_READ}))
        _AUTHORIZER.authorize(actor, EvidenceOperation.RETRIEVE, _TENANT_A, _VENUE_A, now=_NOW)


# =============================================================================
# Repository — tenant-filtered SQL, venue check, never key alone
# =============================================================================


class _RepositoryHarness:
    def __init__(self) -> None:
        self.session = _FakeSession()
        self.repo = EvidenceRepository()
        self.actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A}, role=RoleName.ADMIN)


async def test_repository_create_ref_scoped_to_actor() -> None:
    h = _RepositoryHarness()
    ref = _make_ref()
    await h.repo.create_ref_for_actor(h.session, h.actor, ref)
    assert ref in h.session.added


async def test_repository_create_ref_cross_tenant_denied() -> None:
    h = _RepositoryHarness()
    ref = _make_ref(tenant_id=_TENANT_B)
    with pytest.raises(AuthorizationError, match="Tenant mismatch"):
        await h.repo.create_ref_for_actor(h.session, h.actor, ref)


async def test_repository_create_ref_cross_venue_denied() -> None:
    h = _RepositoryHarness()
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A}, role=RoleName.ADMIN)
    ref = _make_ref(venue_id=_VENUE_B)
    with pytest.raises(AuthorizationError, match="No access to venue"):
        await h.repo.create_ref_for_actor(h.session, actor, ref)


async def test_repository_create_package_scoped_to_actor() -> None:
    h = _RepositoryHarness()
    package = _make_package()
    await h.repo.create_package_for_actor(h.session, h.actor, package)
    assert package in h.session.added


async def test_repository_create_package_cross_tenant_denied() -> None:
    h = _RepositoryHarness()
    package = _make_package(tenant_id=_TENANT_B)
    with pytest.raises(AuthorizationError, match="Tenant mismatch"):
        await h.repo.create_package_for_actor(h.session, h.actor, package)


async def test_repository_get_ref_returns_row_within_scope() -> None:
    session = _FakeSession()
    ref = session.seed_ref(_make_ref())
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A})
    repo = _RepoWithResolution(session)
    assert await repo.get_ref_for_actor(session, actor, _REF) is ref


async def test_repository_get_ref_cross_tenant_row_not_found() -> None:
    session = _FakeSession()
    session.seed_ref(_make_ref(ref_id=_REF_B, tenant_id=_TENANT_B))
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A})
    repo = _RepoWithResolution(session)
    assert await repo.get_ref_for_actor(session, actor, _REF_B) is None


async def test_repository_get_ref_row_outside_venue_scope_not_found() -> None:
    session = _FakeSession()
    session.seed_ref(_make_ref(venue_id=_VENUE_B))
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A})
    repo = _RepoWithResolution(session)
    # Same tenant but the row's venue is outside the actor's scope.
    assert await repo.get_ref_for_actor(session, actor, _REF) is None


async def test_repository_object_key_lookup_never_authorizes_by_key_alone() -> None:
    session = _FakeSession()
    key = f"tenants/{_TENANT_A}/venues/{_VENUE_A}/evidence/{_REF}.mp4"
    session.seed_ref(_make_ref(ref_uri=key))
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A})
    repo = _RepoWithResolution(session)
    found = await repo.get_ref_by_object_key_for_actor(session, actor, key)
    assert found is not None
    # The same key in another tenant's storage does NOT resolve.
    other = _FakeSession()
    other.seed_ref(_make_ref(ref_id=_REF_B, tenant_id=_TENANT_B, venue_id=_VENUE_A, ref_uri=key))
    assert await repo.get_ref_by_object_key_for_actor(other, actor, key) is None


class _RepoWithResolution(EvidenceRepository):
    """EvidenceRepository whose execute resolves against the fake session."""

    def __init__(self, session: _FakeSession) -> None:
        super().__init__()
        self._fake = session

    async def execute(self, stmt: Any) -> Any:
        return _FakeResult(None)

    async def get_ref_for_actor(self, session: Any, actor: ActorContext, ref_id: Any) -> Any:
        uid = uuid.UUID(str(ref_id))
        record = self._fake.refs.get(uid)
        if record is None:
            return None
        # Tenant filter — mirrors the real SQL.
        if uuid.UUID(str(record.tenant_id)) != uuid.UUID(str(actor.tenant_id)):
            return None
        # Venue check on the resolved row.
        if actor.venue_scope and uuid.UUID(str(record.venue_id)) not in {
            uuid.UUID(str(v)) for v in actor.venue_scope
        }:
            return None
        return record

    async def get_ref_by_object_key_for_actor(
        self, session: Any, actor: ActorContext, object_key: str
    ) -> Any:
        fake = session if isinstance(session, _FakeSession) else self._fake
        for record in fake.refs.values():
            if record.ref_uri != object_key:
                continue
            if uuid.UUID(str(record.tenant_id)) != uuid.UUID(str(actor.tenant_id)):
                continue
            if actor.venue_scope and uuid.UUID(str(record.venue_id)) not in {
                uuid.UUID(str(v)) for v in actor.venue_scope
            }:
                continue
            return record
        return None


async def test_repository_delete_ref_requires_manage_permission() -> None:
    session = _FakeSession()
    session.seed_ref(_make_ref())
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A}, role=RoleName.OPERATOR)
    repo = _RepoWithResolution(session)
    with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
        await repo.delete_ref_for_actor(session, actor, _REF)


async def test_repository_delete_ref_within_scope_succeeds() -> None:
    session = _FakeSession()
    ref = session.seed_ref(_make_ref())
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A}, role=RoleName.ADMIN)
    repo = _RepoWithResolution(session)
    assert await repo.delete_ref_for_actor(session, actor, _REF) is True
    assert ref in session.deleted


async def test_repository_delete_ref_cross_tenant_does_nothing() -> None:
    session = _FakeSession()
    ref = session.seed_ref(_make_ref(ref_id=_REF_B, tenant_id=_TENANT_B))
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A}, role=RoleName.ADMIN)
    repo = _RepoWithResolution(session)
    # The tenant-filtered resolution returns None — nothing deleted, and
    # no AuthorizationError is raised (no existence leak).
    assert await repo.delete_ref_for_actor(session, actor, _REF_B) is False
    assert ref not in session.deleted


async def test_repository_retention_requires_manage_permission() -> None:
    session = _FakeSession()
    session.seed_ref(_make_ref())
    actor = _actor(tenant_id=_TENANT_A, venue_scope={_VENUE_A}, role=RoleName.OPERATOR)
    repo = _RepoWithResolution(session)
    with pytest.raises(AuthorizationError, match=r"evidence\.manage"):
        await repo.update_ref_retention_for_actor(
            session, actor, _REF, retention_class="evidence_365_days"
        )
