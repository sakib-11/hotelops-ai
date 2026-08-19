"""Unit tests for the ConfigurationService lifecycle (Task 10.10-10.13).

Uses an in-memory fake version repository so the service logic (state
machine, stale-validation rejection, atomic publish semantics) is tested
deterministically without a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.configuration.service import (
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    ConfigurationService,
    ConfigurationStaleValidationError,
)
from backend.app.domain.configuration.validation import ConfigurationValidationEngine
from backend.app.infrastructure.database.models.configuration import ConfigurationVersionModel
from contracts.common import CameraId
from contracts.configuration import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationStatus,
    TableModel,
    ValidationResultModel,
    ZoneModel,
    ZoneType,
)
from contracts.configuration import (
    ConfigurationVersionModel as ContractVersion,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryScope, GeometryType
from contracts.identity import ActorContext, Permission, RoleName

TENANT = uuid.uuid4()
VENUE = uuid.uuid4()


def _actor(tenant=TENANT) -> ActorContext:
    return ActorContext(
        actor_id=uuid.uuid4(),
        tenant_id=tenant,
        role_name=RoleName.ADMIN,
        permissions=frozenset(Permission),
        venue_scope=frozenset({VENUE}),
        authenticated_at=datetime.now(UTC),
    )


def _polygon(coords):
    return GeometryModel(
        geometry_id=f"g-{uuid.uuid4()}",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=CoordinateSpace.VENUE_LOCAL,
        geometry_scope=GeometryScope.VENUE,
        coordinates=[*coords, coords[0]],
    )


def _zone(pid="z1"):
    return ZoneModel(
        profile_id=pid,
        name=pid,
        zone_type=ZoneType.LOBBY,
        geometry=_polygon([[0, 0], [10, 0], [10, 10], [0, 10]]),
    )


def _camera(pid="cam-1", detection_zones=None):
    return CameraProfileModel(
        profile_id=pid,
        camera_id=CameraId(uuid.uuid4()),
        camera_reference=pid,
        resolution_width=1920,
        resolution_height=1080,
        mount_type=CameraMountType.CEILING,
        detection_zones=detection_zones or [],
    )


class _ActiveCameraResolver:
    """All cameras active — new publishable versions are valid."""

    def camera_status(self, camera_id: object) -> str:
        return "active"


class FakeVersionRepo:
    """In-memory stand-in for ConfigurationVersionRepository.

    Tracks status transitions so the service's state-machine integration
    can be tested without SQL. ``replace_entities`` is a no-op.
    """

    def __init__(self) -> None:
        self.versions: dict[uuid.UUID, ConfigurationVersionModel] = {}
        self.contracts: dict[uuid.UUID, ContractVersion] = {}
        self.configs: dict[uuid.UUID, dict[str, Any]] = {}
        self.session_pins: dict[uuid.UUID, uuid.UUID] = {}
        self.next_version = 1

    # --- configuration repo shims ---

    async def get_or_create(self, session, actor, *, venue_id, name):
        venue = uuid.UUID(str(venue_id))
        tenant = uuid.UUID(str(actor.tenant_id))
        # Idempotent: reuse an existing config for the venue+tenant.
        for cfg in self.configs.values():
            if cfg["venue_id"] == venue and cfg["tenant_id"] == tenant:
                return _ConfigShim(cfg)
        cid = uuid.uuid4()
        self.configs[cid] = {
            "configuration_id": cid,
            "venue_id": venue,
            "tenant_id": tenant,
            "name": name,
            "current_published_version_id": None,
        }
        return _ConfigShim(self.configs[cid])

    async def get_for_actor(self, session, actor, identifier):
        # One fake backs both the configuration repo and the version repo:
        # configuration lookups key on configuration_id, version lookups
        # key on configuration_version_id — both UUIDs are distinct.
        ident = uuid.UUID(str(identifier))
        row = self.versions.get(ident)
        if row is not None and row.tenant_id == uuid.UUID(str(actor.tenant_id)):
            return row
        cfg = self.configs.get(ident)
        if cfg is not None and cfg["tenant_id"] == uuid.UUID(str(actor.tenant_id)):
            return _ConfigShim(cfg)
        return None

    async def set_current_published_version(
        self, session, actor, *, configuration_id, configuration_version_id
    ):
        cid = uuid.UUID(str(configuration_id))
        cfg = self.configs.get(cid)
        if cfg is None:
            return False
        cfg["current_published_version_id"] = uuid.UUID(str(configuration_version_id))
        return True

    # --- version repo shims ---

    async def get_latest_version(self, session, actor, configuration_id):
        matching = [
            v
            for v in self.versions.values()
            if v.configuration_id == uuid.UUID(str(configuration_id))
            and v.tenant_id == uuid.UUID(str(actor.tenant_id))
        ]
        if not matching:
            return None
        return max(matching, key=lambda v: v.version)

    async def create(self, session, actor, *, configuration_id, venue_id, version_number):
        vid = uuid.uuid4()
        now = datetime.now(UTC)
        row = ConfigurationVersionModel(
            configuration_version_id=vid,
            configuration_id=uuid.UUID(str(configuration_id)),
            venue_id=uuid.UUID(str(venue_id)),
            tenant_id=uuid.UUID(str(actor.tenant_id)),
            version=version_number,
            status=ConfigurationStatus.DRAFT.value,
            created_at=now,
            updated_at=now,
        )
        self.versions[vid] = row
        return row

    async def replace_entities(self, session, version, contract):
        # Hold the contract snapshot for later hydration.
        self.contracts[version.configuration_version_id] = contract
        return None

    async def load_contract(self, session, row):
        # Return the stored snapshot (fall back to a construct-built copy
        # that skips the published-immutable validator — the real repo
        # loads entities from the DB).
        stored = self.contracts.get(row.configuration_version_id)
        if stored is not None:
            return stored
        return ContractVersion.model_construct(
            configuration_version_id=row.configuration_version_id,
            configuration_id=row.configuration_id,
            venue_id=row.venue_id,
            tenant_id=row.tenant_id,
            version=row.version,
            status=ConfigurationStatus(row.status),
            validation_result=(
                ValidationResultModel.model_validate(row.validation_result)
                if row.validation_result
                else None
            ),
            validated_at=row.validated_at,
            validated_by=row.validated_by,
            published_at=row.published_at,
            published_by=row.published_by,
            replaced_version_id=row.replaced_version_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def update_status(
        self, session, actor, *, version_id, from_status, to_status, extra_updates=None
    ):
        vid = uuid.UUID(str(version_id))
        row = self.versions.get(vid)
        if row is None or row.tenant_id != uuid.UUID(str(actor.tenant_id)):
            return False
        if row.status != from_status:
            return False
        row.status = to_status
        if extra_updates:
            for k, v in extra_updates.items():
                setattr(row, k, v)
        # Keep the stored contract snapshot in sync so hydration returns
        # the same state the row holds.
        stored = self.contracts.get(row.configuration_version_id)
        if stored is not None:
            self.contracts[row.configuration_version_id] = stored.model_copy(
                update={"status": ConfigurationStatus(to_status)}
            )
        return True

    async def lock_venue_configuration(self, session, actor, configuration_id):
        cid = uuid.UUID(str(configuration_id))
        cfg = self.configs.get(cid)
        if cfg is None:
            return None
        return _ConfigShim(cfg)

    async def get_by_configuration(self, session, actor, configuration_id):
        return [
            v
            for v in self.versions.values()
            if v.configuration_id == uuid.UUID(str(configuration_id))
            and v.tenant_id == uuid.UUID(str(actor.tenant_id))
        ]

    async def get_current_published_for_venue(self, session, actor, venue_id):
        vid = uuid.UUID(str(venue_id))
        for v in self.versions.values():
            if v.venue_id == vid and v.status == ConfigurationStatus.PUBLISHED.value:
                return v
        return None

    async def get_latest_published_version(self, session, actor, configuration_id):
        cid = uuid.UUID(str(configuration_id))
        published = [
            v
            for v in self.versions.values()
            if v.configuration_id == cid
            and v.tenant_id == uuid.UUID(str(actor.tenant_id))
            and v.status == ConfigurationStatus.PUBLISHED.value
        ]
        if not published:
            return None
        return max(published, key=lambda v: v.version)

    async def get_published_for_session(self, session, actor, session_id):
        pinned = self.session_pins.get(uuid.UUID(str(session_id)))
        if pinned is None:
            return None
        row = self.versions.get(pinned)
        if row is None or row.tenant_id != uuid.UUID(str(actor.tenant_id)):
            return None
        if row.status != ConfigurationStatus.PUBLISHED.value:
            return None
        return row

    def pin_session(self, session_id, configuration_version_id):
        self.session_pins[uuid.UUID(str(session_id))] = uuid.UUID(str(configuration_version_id))


@dataclass
class _ConfigShim:
    _data: dict[str, Any] = field(default_factory=dict)

    @property
    def configuration_id(self):
        return self._data["configuration_id"]

    @property
    def venue_id(self):
        return self._data["venue_id"]

    @property
    def tenant_id(self):
        return self._data["tenant_id"]

    @property
    def current_published_version_id(self):
        return self._data.get("current_published_version_id")


def _service(repo: FakeVersionRepo) -> ConfigurationService:
    engine = ConfigurationValidationEngine(camera_resolver=_ActiveCameraResolver())
    return ConfigurationService(
        configuration_repository=repo,  # type: ignore[arg-type]
        version_repository=repo,  # type: ignore[arg-type]
        validation_engine=engine,
    )


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


class TestLifecycle:
    async def test_full_lifecycle_draft_to_published(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()

        draft, _ = await svc.create_draft(
            session, actor, venue_id=VENUE, name="Venue Config", created_by="u1"
        )
        assert draft.status == ConfigurationStatus.DRAFT

        updated = await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            cameras=[_camera(detection_zones=["z1"])],
            zones=[_zone("z1")],
        )
        assert updated.status == ConfigurationStatus.DRAFT

        validating = await svc.start_validation(
            session, actor, version_id=draft.configuration_version_id
        )
        assert validating.status == ConfigurationStatus.VALIDATING

        validated, result = await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        assert validated.status == ConfigurationStatus.VALIDATED
        assert result.valid is True
        assert result.content_revision == validated.content_revision()

        published = await svc.publish(
            session, actor, version_id=draft.configuration_version_id, published_by="u1"
        )
        assert published.success
        assert published.previous_published_version_id is None

        # Published version is immutable — updates are rejected.
        with pytest.raises(ConfigurationConflictError):
            await svc.update_draft(
                session, actor, version_id=draft.configuration_version_id, cameras=[]
            )

    async def test_validation_failure_returns_to_draft(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()

        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        # Two overlapping tables — blocking error.
        await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            tables=[
                TableModel(
                    profile_id="t1", name="T1", geometry=_polygon([[0, 0], [2, 0], [2, 2], [0, 2]])
                ),
                TableModel(
                    profile_id="t2", name="T2", geometry=_polygon([[1, 1], [3, 1], [3, 3], [1, 3]])
                ),
            ],
        )
        await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
        failed, result = await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        assert failed.status == ConfigurationStatus.DRAFT
        assert result.valid is False
        assert any(e.code == "TABLE_OVERLAP" for e in result.errors)
        # Still editable after failure.
        again = await svc.update_draft(
            session, actor, version_id=draft.configuration_version_id, tables=[]
        )
        assert again.status == ConfigurationStatus.DRAFT

    async def test_publish_requires_validated_state(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        with pytest.raises(ConfigurationConflictError):
            await svc.publish(
                session, actor, version_id=draft.configuration_version_id, published_by="u1"
            )

    async def test_stale_validation_rejected(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            cameras=[_camera(detection_zones=["z1"])],
            zones=[_zone("z1")],
        )
        await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
        validated, result = await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        assert validated.status == ConfigurationStatus.VALIDATED

        # Mutate the version's content (this resets validation_result in
        # the service, but simulate a stale result that was NOT reset —
        # e.g. concurrent DB write) by forcing the result revision mismatch.
        row = repo.versions[draft.configuration_version_id]
        row.validation_result = result.model_dump(mode="json")
        # Content changed after validation: add a zone to the DB row's
        # snapshot via replace (fake no-op), then re-hydrate revision.
        validated.model_copy(update={"zones": [_zone("z1"), _zone("z2")]})
        # Force the stored result's revision to differ from current content.
        row.validation_result["content_revision"] = "0" * 64

        with pytest.raises(ConfigurationStaleValidationError):
            await svc.publish(
                session, actor, version_id=draft.configuration_version_id, published_by="u1"
            )

    async def test_publish_is_idempotent(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            cameras=[_camera(detection_zones=["z1"])],
            zones=[_zone("z1")],
        )
        await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
        await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        first = await svc.publish(
            session, actor, version_id=draft.configuration_version_id, published_by="u1"
        )
        second = await svc.publish(
            session, actor, version_id=draft.configuration_version_id, published_by="u1"
        )
        assert first.success and second.success
        assert first.configuration_version_id == second.configuration_version_id
        assert first.published_at == second.published_at

    async def test_out_of_order_publish_rejected(self, session) -> None:
        """Publishing an older VALIDATED version after a newer one is
        already published must be rejected — the current-version pointer
        must never regress."""
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()

        async def _make_validated() -> ContractVersion:
            draft, _cfg = await svc.create_draft(
                session, actor, venue_id=VENUE, name="C", created_by="u1"
            )
            await svc.update_draft(
                session,
                actor,
                version_id=draft.configuration_version_id,
                cameras=[_camera(detection_zones=["z1"])],
                zones=[_zone("z1")],
            )
            await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
            await svc.run_validation(
                session, actor, version_id=draft.configuration_version_id, validated_by="u1"
            )
            return draft

        # v1 stays VALIDATED (not yet published); v2 is published first.
        v1 = await _make_validated()
        v2 = await _make_validated()
        assert v1.version == 1 and v2.version == 2
        await svc.publish(session, actor, version_id=v2.configuration_version_id, published_by="u1")

        # Publishing the OLDER v1 after v2 is current must be rejected.
        with pytest.raises(ConfigurationConflictError):
            await svc.publish(
                session, actor, version_id=v1.configuration_version_id, published_by="u1"
            )

    async def test_clone_from_published_creates_new_draft(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            cameras=[_camera(detection_zones=["z1"])],
            zones=[_zone("z1")],
        )
        await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
        await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        await svc.publish(
            session, actor, version_id=draft.configuration_version_id, published_by="u1"
        )

        clone = await svc.create_draft_from_version(
            session, actor, source_version_id=draft.configuration_version_id, created_by="u2"
        )
        assert clone.status == ConfigurationStatus.DRAFT
        assert clone.version == 2
        assert clone.configuration_version_id != draft.configuration_version_id
        assert len(clone.zones) == 1  # content cloned

    async def test_cross_tenant_access_denied(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")

        other_actor = _actor(tenant=uuid.uuid4())
        result = await svc.get_version(session, other_actor, draft.configuration_version_id)
        assert result is None  # tenant-scoped lookup returns nothing

        # Mutations across tenants raise NotFound.
        with pytest.raises(ConfigurationNotFoundError):
            await svc.start_validation(
                session, other_actor, version_id=draft.configuration_version_id
            )

    async def test_unknown_version_raises_not_found(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        with pytest.raises(ConfigurationNotFoundError):
            await svc.start_validation(session, _actor(), version_id=uuid.uuid4())

    async def test_validation_results_recorded_and_retrievable(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            cameras=[_camera(detection_zones=["z1"])],
            zones=[_zone("z1")],
        )
        await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
        _validated, result = await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        assert result.valid is True
        assert result.validator_version == "10.1.0"
        assert result.configuration_version_id == draft.configuration_version_id
        assert result.content_revision  # bound to exact revision

    async def test_content_revision_changes_on_edit(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        rev1 = draft.content_revision()
        updated = await svc.update_draft(
            session, actor, version_id=draft.configuration_version_id, cameras=[_camera()]
        )
        rev2 = updated.content_revision()
        assert rev1 != rev2


class TestSessionPinning:
    async def _publish_fully(self, session, repo, svc, actor, name="C") -> ContractVersion:
        draft, _ = await svc.create_draft(
            session, actor, venue_id=VENUE, name=name, created_by="u1"
        )
        await svc.update_draft(
            session,
            actor,
            version_id=draft.configuration_version_id,
            cameras=[_camera(detection_zones=["z1"])],
            zones=[_zone("z1")],
        )
        await svc.start_validation(session, actor, version_id=draft.configuration_version_id)
        await svc.run_validation(
            session, actor, version_id=draft.configuration_version_id, validated_by="u1"
        )
        await svc.publish(
            session, actor, version_id=draft.configuration_version_id, published_by="u1"
        )
        return draft

    async def test_session_resolves_exact_pinned_version_not_latest(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()

        v1 = await self._publish_fully(session, repo, svc, actor, name="C")
        # Publish a newer version of the same configuration.
        v2 = await self._publish_fully(session, repo, svc, actor, name="C")
        assert v2.version == 2
        _ = v2  # newer version exists but must not be substituted

        # A session pinned to v1 must resolve v1, NOT v2 (latest).
        session_id = uuid.uuid4()
        repo.pin_session(session_id, v1.configuration_version_id)
        resolved = await svc.resolve_session_configuration(session, actor, session_id)
        assert resolved is not None
        assert resolved.configuration_version_id == v1.configuration_version_id
        assert resolved.version == 1
        assert resolved.status == ConfigurationStatus.PUBLISHED

    async def test_unpinned_session_returns_none(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        resolved = await svc.resolve_session_configuration(session, actor, uuid.uuid4())
        assert resolved is None

    async def test_session_pinned_to_draft_rejected(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        draft, _ = await svc.create_draft(session, actor, venue_id=VENUE, name="C", created_by="u1")
        session_id = uuid.uuid4()
        repo.pin_session(session_id, draft.configuration_version_id)
        resolved = await svc.resolve_session_configuration(session, actor, session_id)
        assert resolved is None  # draft cannot be pinned to a session

    async def test_cross_tenant_session_pin_invisible(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        v1 = await self._publish_fully(session, repo, svc, actor, name="C")
        session_id = uuid.uuid4()
        repo.pin_session(session_id, v1.configuration_version_id)
        other = _actor(tenant=uuid.uuid4())
        resolved = await svc.resolve_session_configuration(session, other, session_id)
        assert resolved is None  # tenant-scoped — invisible to other tenants

    async def test_historical_replay_after_newer_publish(self, session) -> None:
        repo = FakeVersionRepo()
        svc = _service(repo)
        actor = _actor()
        v1 = await self._publish_fully(session, repo, svc, actor, name="C")
        v2 = await self._publish_fully(session, repo, svc, actor, name="C")
        session_id = uuid.uuid4()
        repo.pin_session(session_id, v1.configuration_version_id)
        # Later, v3 is published — historical replay must still pin v1.
        v3 = await self._publish_fully(session, repo, svc, actor, name="C")
        assert v3.version == 3
        assert v2.version == 2
        resolved = await svc.resolve_session_configuration(session, actor, session_id)
        assert resolved is not None
        assert resolved.configuration_version_id == v1.configuration_version_id
        assert resolved.version == 1
