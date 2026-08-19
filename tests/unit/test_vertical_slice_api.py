"""Task 18.12 — API vertical slice (retrieve the occupancy fact/event).

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 15
chain + REGISTERED Task 16 rule (18.8) to produce the canonical fact and
event, which are seeded as the authoritative rows (the 18.10 shapes),
and THIS slice exposes them through the minimal FastAPI surface:

    GET /operational/events/{event_id}  → OccupancyEventResponse
    GET /operational/facts/{fact_id}    → OccupancyFactResponse

The endpoint functions are exercised directly (the codebase's route-test
convention) with the REAL route → REAL service → REAL repository chain
against a session fake faithful to the repository's SQL semantics, plus
the REAL authentication boundary (``get_token_data`` / ``verify_token``)
for the credential scenarios.

Authorization is enforced server-side in layers and is NEVER frontend
filtering:

    authenticated actor  → get_actor_context (JWT; 401 on missing/
                           invalid/expired);
    permission           → require_permission(ANALYTICS_READ);
    tenant context       → the actor's server-side tenant is the ONLY
                           readable tenant — the route has no client
                           tenant parameter (no tenant bypass);
    venue authorization  → the repository checks the record's venue
                           against the actor's venue scope;
    repository filtering → tenant_id is a WHERE predicate (out-of-scope
                           == nonexistent → 404, no enumeration);
    PostgreSQL RLS       → the route scopes the request session to the
                           actor's tenant (SET LOCAL app.tenant_id) so
                           the FORCE RLS policies apply.

Tests (the task's list):

1. authorized manager → 200 canonical DTO;
2. authorized operator → 200 canonical DTO;
3. wrong venue       → DENY (404, indistinguishable from nonexistent);
4. wrong tenant      → DENY (404);
5. expired token     → DENY (401 — the real verify_token boundary);
6. invalid token     → DENY (401);
7. missing actor     → DENY (401 — no Authorization header);
8. nonexistent event → 404.

STOP conditions: authorization never relies on frontend filtering (the
route's signature has no tenant/venue input, and the response is always
a canonical DTO — internal ORM models are never exposed).
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from backend.app.api.routes.operational import (
    get_operational_event,
    get_operational_event_evidence,
    get_operational_fact,
)
from backend.app.application.services.operational_errors import OperationalNotFoundError
from backend.app.application.services.operational_read import (
    FACT_TYPE_OCCUPANCY_SNAPSHOT,
    OperationalReadService,
)
from backend.app.infrastructure.auth.deps import get_token_data, require_permission
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.service import verify_token
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.database.models.temporal import TemporalFactModel
from contracts.common import (
    EventId,
    TenantId,
    UserId,
    VenueId,
    utc_now,
)
from contracts.identity import (
    ActorContext,
    Permission,
    RoleName,
    permissions_for_role,
)
from contracts.operational import (
    EvidenceAvailabilityResponse,
    OccupancyEventResponse,
    OccupancyFactResponse,
)
from contracts.rules import OccupancySessionPhase, RuleEventType
from tests.unit.test_vertical_slice_rule import (
    _identities,
    _load_manifest,
    _run_full_slice,
)

_TEST_SECRET = "test-secret-key-32-chars-long-ok!!!"


# =============================================================================
# Session fake — faithful to the repository's SQL semantics
# =============================================================================


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if len(self._rows) == 1 else None


class _FakeConnection:
    """The session's connection — records the RLS SET LOCAL statements."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement) -> None:
        self.statements.append(str(statement))


def _table_name(statement) -> str:
    src = statement.get_final_froms()[0]
    table = getattr(src, "__table__", src)
    return table.name


def _where_eq(statement) -> dict[str, object]:
    """Extract the equality predicates of a select's WHERE clause."""
    conditions: dict[str, object] = {}
    clause = statement.whereclause
    if clause is None:
        return conditions
    items = clause.clauses if hasattr(clause, "clauses") else [clause]
    for cond in items:
        name = getattr(getattr(cond, "left", None), "name", None)
        right = getattr(cond, "right", None)
        # Comparison values arrive as BindParameters — unwrap to the value.
        if hasattr(right, "value"):
            right = right.value
        if name and right is not None:
            conditions[name] = right
    return conditions


class FakeSession:
    """Transaction session supporting the repository's two selects.

    Faithful to the SQL the repository emits: every select must carry the
    actor's tenant as a WHERE predicate (mirroring the tenant filter),
    and the row is only returned when it exists AND matches that tenant.
    """

    def __init__(
        self,
        *,
        events: dict | None = None,
        facts: dict | None = None,
        evidence: dict | None = None,
    ) -> None:
        self.events = events or {}  # event_id → OperationalEventModel
        self.facts = facts or {}  # fact_id → TemporalFactModel
        self.evidence = evidence or {}  # event_id → EvidenceRefModel
        self.connection_ = _FakeConnection()
        self.last_conditions: dict[str, object] = {}

    async def connection(self):
        return self.connection_

    async def execute(self, statement):
        table = _table_name(statement)
        conditions = _where_eq(statement)
        self.last_conditions = conditions
        if table == "operational_events":
            rows = self._scoped_select(conditions, self.events, "event_id")
        elif table == "temporal_facts":
            rows = self._scoped_select(conditions, self.facts, "fact_id")
        elif table == "evidence_refs":
            rows = self._scoped_select(conditions, self.evidence, "event_id")
        else:
            raise AssertionError(f"unexpected statement for table {table!r}")
        return _FakeResult(rows)

    @staticmethod
    def _scoped_select(conditions, store: dict, id_column: str) -> list:
        tenant = conditions.get("tenant_id")
        target = conditions.get(id_column)
        if tenant is None or target is None:
            raise AssertionError(f"query missing tenant/id scope: {conditions}")
        return [
            row
            for row in store.values()
            if row.tenant_id == tenant and getattr(row, id_column) == target
        ]


# =============================================================================
# Slice fixtures — the REAL 18.8 fact + event as authoritative rows
# =============================================================================


def _slice_rows() -> tuple[OperationalEventModel, TemporalFactModel]:
    """The real 18.8 slice's first event + fact as durable rows."""
    manifest = _load_manifest()
    ids = _identities(manifest)
    outcome = _run_full_slice(manifest, ids)
    event = outcome.events[0]
    snapshot = outcome.snapshots[0]
    key = snapshot.key

    event_row = OperationalEventModel(
        event_id=uuid.UUID(str(event.event_id)),
        event_type=event.event_type,
        schema_version=event.schema_version,
        tenant_id=uuid.UUID(str(key.tenant_id)),
        venue_id=uuid.UUID(str(key.venue_id)),
        session_id=uuid.UUID(str(key.session_id)),
        camera_id=uuid.UUID(str(key.camera_id)),
        event_time=event.event_time,
        produced_at=event.produced_at,
        source=event.source,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        payload=event.payload.model_dump(mode="json"),
    )
    fact_row = TemporalFactModel(
        fact_id=uuid.UUID(str(snapshot.snapshot_id)),
        fact_type=FACT_TYPE_OCCUPANCY_SNAPSHOT,
        fsm_kind=snapshot.fsm_kind,
        schema_version=snapshot.schema_version,
        tenant_id=uuid.UUID(str(key.tenant_id)),
        venue_id=uuid.UUID(str(key.venue_id)),
        session_id=uuid.UUID(str(key.session_id)),
        camera_id=uuid.UUID(str(key.camera_id)),
        configuration_version_id=uuid.UUID(str(key.configuration_version_id)),
        event_time=snapshot.event_time,
        source_transition_id=uuid.UUID(str(snapshot.source_transition_id)),
        fsm_version=snapshot.fsm_version,
        policy_revision=snapshot.policy_revision,
        payload=snapshot.model_dump(mode="json"),
    )
    return event_row, fact_row


def _actor(
    *,
    tenant_id: uuid.UUID,
    role: RoleName = RoleName.MANAGER,
    venue_ids: frozenset[VenueId] = frozenset(),
) -> ActorContext:
    """A server-built actor (empty venue scope = ALL_VENUES)."""
    return ActorContext(
        actor_id=UserId(uuid.uuid4()),
        tenant_id=TenantId(tenant_id),
        role_name=role,
        permissions=permissions_for_role(role),
        venue_scope=venue_ids,
        authenticated_at=utc_now(),
        active=True,
    )


def _session_with(event_row: OperationalEventModel, fact_row: TemporalFactModel) -> FakeSession:
    return FakeSession(
        events={event_row.event_id: event_row},
        facts={fact_row.fact_id: fact_row},
    )


def _settings() -> Settings:
    return Settings(
        app_env="test",
        SECRET_KEY=_TEST_SECRET,
        JWT_ALGORITHM="HS256",
        JWT_EXPIRATION_MINUTES=60,
        _env_file=None,
    )


def _expired_token(settings: Settings) -> str:
    payload = {
        "iss": "hotelops-ai",
        "sub": str(uuid.uuid4()),
        "iat": datetime.now(UTC) - timedelta(hours=2),
        "exp": datetime.now(UTC) - timedelta(minutes=1),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


# =============================================================================
# 1 + 2. AUTHORIZED MANAGER / OPERATOR — 200 canonical DTO
# =============================================================================


class TestAuthorizedAccess:
    async def test_authorized_manager_retrieves_event(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        # The permission gate admits the manager.
        await require_permission(Permission.ANALYTICS_READ)(actor)

        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )

        assert isinstance(response, OccupancyEventResponse)
        assert response.event_id == EventId(event_row.event_id)
        assert response.event_type == RuleEventType.OCCUPANCY_SESSION.value
        assert response.tenant_id == TenantId(event_row.tenant_id)
        assert response.venue_id == VenueId(event_row.venue_id)
        assert response.session_id is not None
        assert response.source == "rule:occupancy_session:v1"
        assert response.event_time == event_row.event_time
        assert response.payload.phase is OccupancySessionPhase.STARTED
        assert response.payload.occupancy_count == 1
        # The request session was scoped to the actor's tenant for RLS.
        assert (
            f"SET LOCAL app.tenant_id = '{event_row.tenant_id}'" in session.connection_.statements
        )

    async def test_authorized_operator_retrieves_event(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.OPERATOR)
        await require_permission(Permission.ANALYTICS_READ)(actor)

        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert response.tenant_id == TenantId(event_row.tenant_id)
        assert response.event_id == EventId(event_row.event_id)

    async def test_authorized_manager_retrieves_fact(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=fact_row.tenant_id, role=RoleName.MANAGER)

        response = await get_operational_fact(
            fact_id=EventId(fact_row.fact_id), actor=actor, _perm=None, session=session
        )

        assert isinstance(response, OccupancyFactResponse)
        assert response.fact_id == EventId(fact_row.fact_id)
        assert response.fact_type == FACT_TYPE_OCCUPANCY_SNAPSHOT
        assert response.fsm_kind == "occupancy"
        assert response.tenant_id == TenantId(fact_row.tenant_id)
        assert response.venue_id == VenueId(fact_row.venue_id)
        assert response.payload.occupancy_count == 1
        assert response.payload.snapshot_id == EventId(fact_row.fact_id)

    async def test_specific_venue_scope_with_access_succeeds(self) -> None:
        """A SPECIFIC_VENUES actor who IS scoped to the record's venue reads it."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(
            tenant_id=event_row.tenant_id,
            role=RoleName.OPERATOR,
            venue_ids=frozenset({VenueId(event_row.venue_id)}),
        )
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert response.venue_id == VenueId(event_row.venue_id)


# =============================================================================
# Evidence availability (Task 18.13) — a server-derived fact, never client logic
# =============================================================================


class TestEvidenceAvailability:
    async def test_evidence_available_for_linked_event(self) -> None:
        """An event with a durable evidence request row (Task 18.9) answers
        available=True with the deterministic request identity."""
        event_row, fact_row = _slice_rows()
        ref_row = EvidenceRefModel(
            ref_id=uuid.uuid4(),
            schema_version="1.0",
            tenant_id=event_row.tenant_id,
            venue_id=event_row.venue_id,
            ref_type="frame",
            ref_uri="memory://evidence/request",
            event_time=event_row.event_time,
            event_id=event_row.event_id,
        )
        session = FakeSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
            evidence={event_row.event_id: ref_row},
        )
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        await require_permission(Permission.ANALYTICS_READ)(actor)

        response = await get_operational_event_evidence(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )

        assert isinstance(response, EvidenceAvailabilityResponse)
        assert response.event_id == EventId(event_row.event_id)
        assert response.available is True
        assert response.evidence_ref_id == EventId(ref_row.ref_id)

    async def test_evidence_not_available_is_a_valid_answer(self) -> None:
        """An in-scope event with no evidence row answers available=False —
        a legitimate answer, not an error."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.OPERATOR)

        response = await get_operational_event_evidence(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )

        assert response.available is False
        assert response.evidence_ref_id is None

    async def test_evidence_availability_out_of_scope_is_denied(self) -> None:
        """An event of another venue/tenant is 404 — availability is never
        answered for an event the actor cannot see."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(
            tenant_id=event_row.tenant_id,
            role=RoleName.OPERATOR,
            venue_ids=frozenset({VenueId(uuid.uuid4())}),
        )
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event_evidence(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_evidence_availability_missing_event_denied(self) -> None:
        """A nonexistent event is 404 (never an availability answer)."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event_evidence(
                event_id=EventId(uuid.uuid4()), actor=actor, _perm=None, session=session
            )


# =============================================================================
# 3 + 4 + 8. UNAUTHORIZED / MISSING — DENY (404, indistinguishable)
# =============================================================================


class TestUnauthorizedAccess:
    async def test_wrong_venue_denied(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        # Same tenant, but the actor is scoped to a DIFFERENT venue.
        actor = _actor(
            tenant_id=event_row.tenant_id,
            role=RoleName.OPERATOR,
            venue_ids=frozenset({VenueId(uuid.uuid4())}),
        )
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_wrong_tenant_denied(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        # A different tenant with ALL_VENUES scope still cannot read it.
        actor = _actor(tenant_id=uuid.uuid4(), role=RoleName.MANAGER)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_wrong_tenant_fact_denied(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=uuid.uuid4(), role=RoleName.MANAGER)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_fact(
                fact_id=EventId(fact_row.fact_id), actor=actor, _perm=None, session=session
            )

    async def test_nonexistent_event_denied(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(uuid.uuid4()), actor=actor, _perm=None, session=session
            )

    async def test_nonexistent_fact_denied(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=fact_row.tenant_id, role=RoleName.OPERATOR)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_fact(
                fact_id=EventId(uuid.uuid4()), actor=actor, _perm=None, session=session
            )


# =============================================================================
# 5 + 6 + 7. AUTHENTICATION BOUNDARY — expired / invalid / missing actor
# =============================================================================


class TestAuthenticationBoundary:
    def test_expired_token_denied(self) -> None:
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(_expired_token(_settings()), _settings())

    def test_invalid_token_denied(self) -> None:
        with pytest.raises(AuthenticationError, match="Invalid token format"):
            verify_token("not-a-jwt-token", _settings())

    def test_tampered_token_denied(self) -> None:
        settings = _settings()
        parts = jwt.encode(
            {
                "iss": "hotelops-ai",
                "sub": "user-1",
                "iat": utc_now(),
                "exp": utc_now() + timedelta(hours=1),
            },
            settings.secret_key,
            algorithm="HS256",
        ).split(".")
        tampered = parts[0] + ".INVALID_PAYLOAD." + parts[2]
        with pytest.raises(AuthenticationError, match="token"):
            verify_token(tampered, settings)

    async def test_missing_actor_denied(self) -> None:
        """No Authorization header → the authenticated-actor dependency denies."""
        with pytest.raises(AuthenticationError, match="Missing Authorization header"):
            await get_token_data(credentials=None, settings=_settings())


# =============================================================================
# Repository-level filtering + permission gate + RLS
# =============================================================================


class TestServerSideEnforcement:
    async def test_repository_emits_actor_tenant_filter(self) -> None:
        """The repository's query carries the actor's tenant as a WHERE
        predicate — repository-level filtering, never client input."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)

        result = await OperationalReadService().get_event(
            session=session, actor=actor, event_id=event_row.event_id
        )
        assert result is not None
        assert session.last_conditions["tenant_id"] == event_row.tenant_id
        assert session.last_conditions["event_id"] == event_row.event_id

    async def test_permission_gate_admits_authorized_roles(self) -> None:
        """ANALYTICS_READ admits manager and operator; a forged actor
        without the permission is rejected (403 semantics)."""
        gate = require_permission(Permission.ANALYTICS_READ)
        for role in (RoleName.MANAGER, RoleName.OPERATOR):
            await gate(_actor(tenant_id=uuid.uuid4(), role=role))  # no exception

        forged = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),  # no ANALYTICS_READ
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await gate(forged)


# =============================================================================
# Canonical DTO + STOP-condition guards
# =============================================================================


class TestCanonicalDto:
    def _event_dto_keys(self) -> set[str]:
        return {
            "event_id",
            "event_type",
            "schema_version",
            "tenant_id",
            "venue_id",
            "session_id",
            "camera_id",
            "event_time",
            "produced_at",
            "source",
            "correlation_id",
            "causation_id",
            "payload",
        }

    async def test_response_is_canonical_dto_never_the_orm_row(self) -> None:
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        response = await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )

        assert not isinstance(response, OperationalEventModel)
        data = response.model_dump(mode="json")
        # The wire shape is EXACTLY the canonical DTO — no ORM-internal
        # columns (ingestion_time, created_at, ...) leak through.
        assert set(data) == self._event_dto_keys()
        for banned in ("ingestion_time", "created_at", "updated_at", "last_error"):
            assert banned not in data
        # The canonical payload is the typed Task 16 contract.
        assert data["payload"]["phase"] == OccupancySessionPhase.STARTED.value

    async def test_non_occupancy_event_is_not_retrievable(self) -> None:
        """The occupancy surface never returns a different event type as an
        occupancy DTO — it is treated as not found (deterministic)."""
        event_row, fact_row = _slice_rows()
        other = OperationalEventModel(
            event_id=uuid.uuid4(),
            event_type="dwell_threshold",
            schema_version="1.0",
            tenant_id=event_row.tenant_id,
            venue_id=event_row.venue_id,
            event_time=event_row.event_time,
            produced_at=event_row.produced_at,
            source="rule:dwell_threshold:v1",
            payload={"event_time": event_row.event_time.isoformat()},
        )
        session = FakeSession(events={other.event_id: other}, facts={fact_row.fact_id: fact_row})
        actor = _actor(tenant_id=event_row.tenant_id, role=RoleName.MANAGER)
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(other.event_id), actor=actor, _perm=None, session=session
            )


class TestNoClientTenantBypass:
    def test_route_signature_has_no_tenant_or_venue_input(self) -> None:
        """STOP condition: authorization never relies on frontend filtering —
        the route accepts ONLY the resource id; tenant/venue come from the
        server-side ActorContext, so a client can never select a tenant."""
        for endpoint in (get_operational_event, get_operational_fact):
            params = set(inspect.signature(endpoint).parameters)
            assert "tenant_id" not in params
            assert "venue_id" not in params
            # The server-side auth dependencies are wired in the signature.
            assert "actor" in params
            assert "session" in params
