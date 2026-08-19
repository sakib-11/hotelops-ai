"""Unit tests for media lifecycle audit events (Task 9.15).

Verifies the transactional outbox wiring: every lifecycle transition
persists an audit row AND an outbox row atomically with the media state
change, carrying bounded, non-sensitive metadata.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from backend.app.application.services.media_audit import (
    EVENT_AVAILABLE,
    EVENT_UPLOAD_COMPLETED,
    EVENT_UPLOAD_INITIATED,
    enqueue_media_audit_event,
    system_actor,
)
from backend.app.infrastructure.database.models.audit_outbox_inbox import (
    AuditEventModel,
    OutboxEventModel,
)
from contracts.common import UserId
from contracts.identity import ActorContext, Permission, RoleName
from tests.unit.fakes import make_media


@pytest.fixture
def session() -> AsyncMock:
    s = AsyncMock()
    # session.add() is synchronous on SQLAlchemy's AsyncSession — prevent
    # AsyncMock from wrapping it as a coroutine.
    from unittest.mock import MagicMock

    s.add = MagicMock()
    return s


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("c7a10f82-84b2-4d7a-b50a-bdfd189196b0")


@pytest.fixture
def venue_id() -> uuid.UUID:
    return uuid.UUID("4a87265a-063a-4a6c-9c70-7613768b4ad3")


@pytest.fixture
def actor(tenant_id: uuid.UUID, venue_id: uuid.UUID) -> ActorContext:
    return ActorContext(
        actor_id=UserId(uuid.uuid4()),
        tenant_id=tenant_id,
        role_name=RoleName.ADMIN,
        permissions=frozenset(Permission),
        venue_scope=frozenset({venue_id}),
        authenticated_at=datetime.now(UTC),
    )


def _added_objects(session: AsyncMock) -> list[object]:
    return [call.args[0] for call in session.add.call_args_list]


class TestAuditEnqueue:
    """Audit + outbox rows are written atomically via the shared session."""

    async def test_enqueue_writes_audit_and_outbox_rows(
        self,
        session: AsyncMock,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = make_media(tenant_id=tenant_id, venue_id=venue_id)

        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_UPLOAD_INITIATED,
            media=media,
            correlation_id="corr-123",
        )

        added = _added_objects(session)
        audit_rows = [o for o in added if isinstance(o, AuditEventModel)]
        outbox_rows = [o for o in added if isinstance(o, OutboxEventModel)]

        assert len(audit_rows) == 1
        assert len(outbox_rows) == 1

        audit = audit_rows[0]
        assert audit.action == EVENT_UPLOAD_INITIATED
        assert str(audit.tenant_id) == str(tenant_id)
        assert str(audit.venue_id) == str(venue_id)
        assert audit.correlation_id == "corr-123"
        assert audit.metadata_["media_id"] == str(media.media_id)

        outbox = outbox_rows[0]
        assert outbox.event_type == EVENT_UPLOAD_INITIATED
        # The outbox payload is the serialized EventEnvelope — the media
        # identifiers ride inside the envelope's payload dict.
        envelope_payload = outbox.payload["payload"]
        assert envelope_payload["media_id"] == str(media.media_id)
        assert envelope_payload["object_key"] == media.object_key

    async def test_completed_event_carries_bounded_payload(
        self,
        session: AsyncMock,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = make_media(tenant_id=tenant_id, venue_id=venue_id, size_bytes=2048)
        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_UPLOAD_COMPLETED,
            media=media,
            extra_payload={"size_bytes": 2048},
        )

        outbox_row = next(o for o in _added_objects(session) if isinstance(o, OutboxEventModel))
        envelope_payload = outbox_row.payload["payload"]
        assert envelope_payload["size_bytes"] == 2048
        assert envelope_payload["state"] == media.lifecycle_state
        # The payload must never contain media bytes or secrets.
        assert all(key not in envelope_payload for key in ("password", "token", "secret"))

    async def test_no_duplicate_audit_infrastructure(
        self,
        session: AsyncMock,
        actor: ActorContext,
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID,
    ) -> None:
        media = make_media(tenant_id=tenant_id, venue_id=venue_id)
        await enqueue_media_audit_event(
            session,
            actor=actor,
            event_type=EVENT_AVAILABLE,
            media=media,
        )
        # Exactly one audit + one outbox row per event — no new tables.
        added = _added_objects(session)
        assert sum(1 for o in added if isinstance(o, AuditEventModel)) == 1
        assert sum(1 for o in added if isinstance(o, OutboxEventModel)) == 1


class TestSystemActor:
    """The worker's synthetic system identity."""

    def test_system_actor_is_tenant_scoped(self, tenant_id: uuid.UUID) -> None:
        actor = system_actor(tenant_id)
        assert str(actor.tenant_id) == str(tenant_id)
        assert actor.permissions  # full capability set for system operations

    def test_system_actor_id_is_reserved(self) -> None:
        actor = system_actor(uuid.uuid4())
        assert str(actor.actor_id) == "00000000-0000-0000-0000-000000000001"
