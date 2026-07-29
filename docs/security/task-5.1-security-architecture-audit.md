# Task 5.1 — Security Architecture Audit & Design Freeze

> **Project**: HotelOps AI
> **Phase**: Task 5 — Tenant, Venue, Identity & RBAC
> **Status**: Design Freeze (no implementation yet)

---

## 1. Repository Findings

### Current State

| Aspect | Finding |
|--------|---------|
| **Branch** | `main` — single initial commit `c60fb00` |
| **Tasks 1–4** | Complete: product docs, monorepo scaffold, backend infra (FastAPI + Postgres + Redis + MinIO), canonical contracts |
| **Task 4 contracts** | 16 canonical contracts in `contracts/` — fully typed, validated, tested (88 contract tests) |
| **Backend auth** | None — no authentication, authorization, or identity layer exists |
| **FastAPI app** | Single `app` with lifespan, one router (`api_router`), DI via `dependencies.py` |
| **SQLAlchemy** | Async engine via `DatabaseClient` (engine + session factory), no ORM models, no migrations |
| **PostgreSQL** | TimescaleDB 2.19/pg17 in Docker, single `hotelops` database, single user |
| **Redis** | Async client initialized, no streams/consumer groups/caching yet |
| **MinIO** | S3-compatible storage, single bucket `hotelops-development` |
| **Settings** | Pydantic `BaseSettings` loading from `.env` — no secrets management |
| **Testing** | 138 tests pass (88 contract + 9 integration + 41 unit), pytest markers for all tiers |
| **ADRs** | 5 ADRs exist (ADR-000 through ADR-005) following standard template |
| **docker-compose** | All services defined, no TLS, no auth on internal network |

### Key Observations

1. **No identity infrastructure exists** — no JWT, no session, no API keys
2. **Single database user** — `hotelops` has full access; no application-level role separation
3. **No migrations framework** — `database/migrations/` has only `.gitkeep`
4. **Dependencies are injected** via `app_state` singleton pattern — clean extension point for auth context
5. **No HTTPS/TLS** in local Docker Compose; production deployment will need reverse proxy
6. **All services on same Docker network** — no network segmentation

---

## 2. Baseline Test Result

```
make check — 138 passed, 0 failed
  ├── ruff format --check:  ✅ 88 files already formatted
  ├── ruff check:           ✅ All checks passed!
  ├── mypy:                 ✅ Success: no issues found
  └── pytest:               ✅ 138 passed
```

**Tasks 1–4 regression: GREEN.** No pre-existing failures.

---

## 3. Security Invariants

The following invariants **must** hold for Task 5 implementation.

| ID | Invariant | Scope |
|----|-----------|-------|
| **SEC-01** | Authentication identifies a principal but does not by itself grant access. | AuthN vs AuthZ boundary |
| **SEC-02** | `tenant_id` supplied by a client never establishes tenant authorization. | Anti-spoofing |
| **SEC-03** | `venue_id` supplied by a client never establishes venue authorization. | Anti-spoofing |
| **SEC-04** | Role supplied by a client is never trusted. | Anti-spoofing |
| **SEC-05** | Permissions supplied by a client are never trusted. | Anti-spoofing |
| **SEC-06** | Every protected operation has an authenticated `ActorContext`. | Mandatory review |
| **SEC-07** | Every tenant-scoped operation derives tenant scope from server-side authorization state. | Tenant isolation |
| **SEC-08** | Every venue-scoped operation validates venue membership/scope. | Venue isolation |
| **SEC-09** | Repository access is scoped. | Data access |
| **SEC-10** | PostgreSQL RLS provides an additional isolation boundary. | Defense in depth |
| **SEC-11** | Cross-tenant object IDs cannot bypass authorization. | IDOR prevention |
| **SEC-12** | Disabled/revoked identities cannot continue accessing protected resources. | Lifecycle |
| **SEC-13** | Realtime/WebSocket authorization follows the same identity and scope rules. | Real-time |
| **SEC-14** | Audit records can identify the authenticated actor, tenant and relevant venue. | Audit |

---

## 4. Threat Model

### Cross-Tenant IDOR

| Aspect | Detail |
|--------|--------|
| **Threat** | Tenant A accesses Tenant B's video sessions, detections, analytics, or evidence |
| **Attack path** | Attacker modifies `tenant_id` in request path/body/header to another tenant's ID |
| **Protection** | Server-side `ActorContext` derives tenant from auth token, never from request payload. All repository queries filter by `tenant_id` from context. |
| **Expected response** | 403 Forbidden. No data leakage in error message. |
| **Test strategy** | Create 2 tenants, authenticate as user from Tenant A, attempt to access Tenant B's resources via direct ID manipulation |

### Cross-Venue Access

| Aspect | Detail |
|--------|--------|
| **Threat** | User with access to Venue X accesses data from Venue Y within the same tenant |
| **Attack path** | Attacker modifies `venue_id` in request to a venue they don't have membership for |
| **Protection** | `ActorContext` contains resolved venue scope. Repository queries include venue scope filtering. Membership table validated on each request. |
| **Expected response** | 403 Forbidden |
| **Test strategy** | Authenticate as user with scope on Venue X, attempt to read/modify Venue Y resources |

### Tenant Spoofing

| Aspect | Detail |
|--------|--------|
| **Threat** | Attacker claims to belong to a tenant they are not associated with |
| **Attack path** | Attacker includes `tenant_id` in registration/login payload |
| **Protection** | Tenant is derived server-side from the authenticated user's membership record. Client-supplied tenant is never trusted. |
| **Expected response** | Registration/operation fails with 400/403 |
| **Test strategy** | Attempt registration with mismatched tenant claim; verify tenant always comes from server-side state |

### Role Escalation

| Aspect | Detail |
|--------|--------|
| **Threat** | OPERATOR escalates to MANAGER or ADMIN privileges |
| **Attack path** | Attacker claims a higher role in request payload or modifies stored role data |
| **Protection** | Role is resolved server-side from membership table. Roles are immutable at the API layer (only ADMIN can modify memberships through dedicated endpoints). |
| **Expected response** | 403 Forbidden |
| **Test strategy** | Authenticate as OPERATOR, attempt ADMIN-only endpoints; attempt to modify own role |

### Permission Injection

| Aspect | Detail |
|--------|--------|
| **Threat** | Attacker injects permissions they don't possess |
| **Attack path** | Attacker adds extra permissions to request context or modifies stored permissions |
| **Protection** | Permissions are computed server-side from role definition. Client-supplied permissions are never evaluated. |
| **Expected response** | 403 Forbidden |
| **Test strategy** | Verify permission checks use server-side role-permission mapping; attempt to call endpoint with inline permission claim |

### Expired / Tampered Credential

| Aspect | Detail |
|--------|--------|
| **Threat** | Attacker uses an expired or forge JWT/session token |
| **Attack path** | Replay stolen JWT after expiry; modify JWT claims; use token from revoked session |
| **Protection** | JWTs signed with server secret (HS256), short TTL (15 min access, 7 day refresh). Token validation on every request. Revoked token blacklist in Redis. |
| **Expected response** | 401 Unauthorized |
| **Test strategy** | Submit expired JWT; modify JWT claim and verify signature rejection; test revoked token rejection |

### Disabled User / Tenant / Venue

| Aspect | Detail |
|--------|--------|
| **Threat** | Disabled entity continues to access resources |
| **Attack path** | User account is disabled but session remains active; tenant subscription lapses |
| **Protection** | `ActorContext`` resolution checks user.active, membership.active, tenant.active, venue.active on every request. Cached with short TTL. |
| **Expected response** | 403 Forbidden |
| **Test strategy** | Create user, authenticate, disable user, verify subsequent requests are rejected |

### WebSocket Unauthorized Connection

| Aspect | Detail |
|--------|--------|
| **Threat** | Unauthenticated client establishes WebSocket connection and receives real-time events |
| **Attack path** | Direct WebSocket upgrade without valid auth token; connection with insufficient venue scope |
| **Protection** | WebSocket upgrade requires valid JWT as query parameter. Connection scope validated against venue subscription. |
| **Expected response** | 401 on upgrade attempt; 403 if scope insufficient |
| **Test strategy** | Attempt WebSocket connect without token, with expired token, with token lacking venue scope |

### WebSocket Scope Change After Connection

| Aspect | Detail |
|--------|--------|
| **Threat** | User's role/permissions change while WebSocket is connected; user continues receiving unauthorized events |
| **Attack path** | User demoted from MANAGER to OPERATOR, but WebSocket remains connected with MANAGER-level subscriptions |
| **Protection** | Periodic scope re-validation on WebSocket connection (every 60s). On scope change, connection receives close frame. |
| **Expected response** | Connection closed with 4001 close code, reconnection requires new auth |
| **Test strategy** | Establish WebSocket, change user role server-side, verify connection is terminated within revalidation interval |

---

## 5. Domain Relationships

### Entity Definitions

```
Tenant
  Represents a hotel property group/customer.
  Has its own venue(s), users, and isolated data.
  Example: "Marriott International", "Hilton Corporate"

Venue
  A specific physical location belonging to a tenant.
  Example: "Marriott Downtown NYC", "Hilton LAX"
  Scope: All video sessions, detections, analytics, evidence,
         recommendations, alerts for that location.

User
  A human operator or system account.
  Belongs to exactly one tenant.
  Can have memberships in multiple venues within that tenant.

Role
  A named set of permissions.
  System-defined for v1.0 (not tenant-customizable).
  Fixed roles: ADMIN, MANAGER, OPERATOR

Membership
  Associates a User with a Role and a set of Venues.
  A user may have multiple memberships (e.g., OPERATOR in Venue A,
  MANAGER in Venue B), but only one role per venue.
```

### Relationship Diagram

```
Tenant (1)
  │
  ├── Venue (0..N) — physical locations
  │
  └── Membership (0..N)
         │
         ├── User (1)
         ├── Role (1) — ADMIN | MANAGER | OPERATOR
         └── Venue (1..N) — scoped venues
```

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Roles** | Fixed system roles (3) | YAGNI — no requirement for tenant-defined custom roles in v1.0 |
| **User scope** | Users belong to exactly 1 tenant | Simplifies tenant isolation; cross-tenant user management is a future concern |
| **Venue scope** | Membership can cover multiple venues | A MANAGER may oversee multiple venues at the same hotel property |
| **Soft delete** | All entities use `active` flag, not hard DELETE | Preserves referential integrity and audit trail |

---

## 6. Authorization Flow

### Flow Diagram

```
Client Request
    │
    ▼
[1] Credential Extraction (Authorization: Bearer <JWT>)
    │
    ▼
[2] Authentication — JWT verification (signature, expiry, issuer)
    │
    ▼
[3] Subject Resolution — Extract user_id + tenant_id from JWT claims
    │
    ▼
[4] User Lookup — Query users table for user_id, verify active
    │
    ▼
[5] Membership Resolution — Load active memberships for user
    │   ├── Resolve roles from membership
    │   └── Resolve venue scope from membership
    │
    ▼
[6] Entity State Check — Verify user.active, tenant.active, venue.active
    │
    ▼
[7] ActorContext Construction — Frozen server-side context object:
    │   ├── actor_id: UserId
    │   ├── tenant_id: TenantId
    │   ├── role: Role
    │   ├── permissions: frozenset[Permission]
    │   ├── venue_scope: frozenset[VenueId]
    │   └── authenticated_at: datetime
    │
    ▼
[8] Authorization — Route/Repository checks against ActorContext:
        ├── Permission check (does role include this permission?)
        ├── Venue scope check (is target venue in venue_scope?)
        └── Tenant isolation (does target belong to tenant_id?)
```

### Key Rules

1. **ActorContext is constructed server-side.** Request payloads never influence ActorContext fields directly.
2. **ActorContext is immutable.** Once constructed for a request, it cannot be modified.
3. **Every protected endpoint receives ActorContext via FastAPI dependency.**
4. **Repository methods receive ActorContext** (or tenant_id + venue_scope) for scoped queries.
5. **Caching**: User/membership/tenant/venue state is cached with 60s TTL to avoid DB pressure on every request.

---

## 7. ActorContext Design

```python
# Future implementation — not yet coded

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ActorContext:
    """Server-constructed immutable authorization context.

    Every protected operation receives this via FastAPI dependency.
    Fields are frozen — clients cannot influence these values.
    """

    actor_id: UUID
    tenant_id: UUID
    role: str  # ADMIN | MANAGER | OPERATOR
    permissions: frozenset[str] = field(hash=False)
    venue_scope: frozenset[UUID] = field(default_factory=frozenset)
    authenticated_at: datetime = field(default_factory=datetime.utcnow)
    active: bool = True

    def has_permission(self, permission: str) -> bool:
        """Check if the actor has a specific permission."""
        return permission in self.permissions

    def has_venue_access(self, venue_id: UUID) -> bool:
        """Check if the actor has access to a specific venue."""
        return venue_id in self.venue_scope
```

---

## 8. RBAC Matrix

### Roles

| Role | Level | Description |
|------|-------|-------------|
| **ADMIN** | System | Full access to all tenant and venue resources. User management, membership management, tenant configuration. |
| **MANAGER** | Venue | Operational management within assigned venues. Can view analytics, manage alerts, approve actions. |
| **OPERATOR** | Venue | Day-to-day operations within assigned venues. Can view live video, read analytics, receive alerts. |

### Permissions

| Category | Permission | ADMIN | MANAGER | OPERATOR |
|----------|-----------|-------|---------|----------|
| **Venue** | `venue.read` | ✅ | ✅ | ✅ |
| **Venue** | `venue.manage` | ✅ | ❌ | ❌ |
| **Video** | `video.read` | ✅ | ✅ | ✅ |
| **Video** | `video.analyze` | ✅ | ✅ | ❌ |
| **Analytics** | `analytics.read` | ✅ | ✅ | ✅ |
| **Evidence** | `evidence.read` | ✅ | ✅ | ✅ |
| **Recommendation** | `recommendation.read` | ✅ | ✅ | ✅ |
| **Recommendation** | `recommendation.manage` | ✅ | ✅ | ❌ |
| **Alert** | `alert.read` | ✅ | ✅ | ✅ |
| **Alert** | `alert.manage` | ✅ | ✅ | ❌* |
| **User** | `user.read` | ✅ | ❌ | ❌ |
| **User** | `user.manage` | ✅ | ❌ | ❌ |
| **Membership** | `membership.read` | ✅ | ❌ | ❌ |
| **Membership** | `membership.manage` | ✅ | ❌ | ❌ |

> *MANAGER may acknowledge/dismiss alerts but cannot create or delete alert rules.

### Implementation Rule

Permissions are never checked by name in business logic (`if role == "ADMIN"`). Instead:

```python
# Correct
if not context.has_permission("analytics.read"):
    raise ForbiddenError()

# Incorrect
if context.role != "ADMIN":
    raise ForbiddenError()
```

---

## 9. Repository Scoping Strategy

### Pattern

Every repository method that returns tenant-scoped or venue-scoped data receives `ActorContext` (or at minimum `tenant_id`) and applies filters automatically.

```python
# Future pattern


class VideoSessionRepository:
    async def list_by_venue(
        self,
        context: ActorContext,
        venue_id: UUID,
    ) -> list[VideoSession]:
        # 1. Authorize
        if not context.has_permission("video.read"):
            raise ForbiddenError()
        if not context.has_venue_access(venue_id):
            raise ForbiddenError()

        # 2. Query with implicit tenant scope
        stmt = (
            select(VideoSessionModel)
            .where(VideoSessionModel.tenant_id == context.tenant_id)
            .where(VideoSessionModel.venue_id == venue_id)
        )
        result = await self._session.execute(stmt)
        return [row.to_contract() for row in result.scalars()]
```

### Scoping Rules

| Data Type | Tenant Scope | Venue Scope | Repository Filter |
|-----------|-------------|-------------|-------------------|
| VideoSession | ✅ | ✅ | `WHERE tenant_id = :tid AND venue_id = :vid` |
| DetectionObservation | ✅ | ✅ | Via session/asset relationship |
| TrackObservation | ✅ | ✅ | Via session/asset relationship |
| MetricValue | ✅ | ✅ | Via session/asset relationship |
| EvidencePackage | ✅ | ✅ | `WHERE tenant_id = :tid` + venue filter via evidence_refs |
| Finding | ✅ | ❌ | `WHERE tenant_id = :tid` |
| Recommendation | ✅ | ❌ | `WHERE tenant_id = :tid` |
| Alert | ✅ | ✅ | `WHERE tenant_id = :tid AND venue_id = :vid` |
| ActionCommand | ✅ | ❌ | `WHERE tenant_id = :tid` |
| User | ✅ | ❌ | `WHERE tenant_id = :tid` |
| Membership | ✅ | ❌ | `WHERE tenant_id = :tid` |

---

## 10. RLS Strategy

### Architecture

```
Request
    │
    ▼
Application Authorization (ActorContext → permission + scope check)
    │
    ▼
Repository Scope Filter (WHERE tenant_id = :tid [, venue_id = :vid])
    │
    ▼
PostgreSQL Row-Level Security (RLS) — defense in depth
```

### RLS Policy Design

```sql
-- Future implementation — not yet applied

-- Enable RLS on tenant-scoped tables
ALTER TABLE video_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE detections ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;

-- Tenant isolation policy
CREATE POLICY tenant_isolation ON video_sessions
    FOR ALL
    USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Venue isolation policy (for venue-scoped tables)
CREATE POLICY venue_isolation ON video_sessions
    FOR ALL
    USING (
        tenant_id = current_setting('app.tenant_id')::uuid
        AND venue_id = ANY(
            current_setting('app.venue_ids')::uuid[]
        )
    );
```

### Context Propagation

| Step | Mechanism |
|------|-----------|
| **Auth resolution** | `ActorContext` built in FastAPI middleware/dependency |
| **DB session creation** | `app.tenant_id` and `app.venue_ids` set via `SET SESSION` after acquiring connection from pool |
| **Connection pooling** | `reset_on_return` clears session-local settings before returning to pool |
| **Fail-closed** | If `app.tenant_id` is not set, RLS default-deny policy rejects all queries |
| **Migration/Admin** | Separate connection pool with elevated privileges (bypass RLS) |

### Connection Pool Strategy

```python
# Future implementation pattern

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection


@event.listens_for(AsyncConnection, "after_connect")
def set_rls_context(dbapi_connection, connection_record):
    """Reset any leftover RLS context when connection is returned to pool."""
    cursor = dbapi_connection.cursor()
    cursor.execute("RESET app.tenant_id")
    cursor.execute("RESET app.venue_ids")
    cursor.close()


async def get_scoped_session(context: ActorContext) -> AsyncConnection:
    """Get a database connection with RLS context set."""
    conn = await app_state.database.get_connection()
    await conn.execute(text(f"SET SESSION app.tenant_id = '{context.tenant_id}'"))
    if context.venue_scope:
        venue_ids = ",".join(str(v) for v in context.venue_scope)
        await conn.execute(text(f"SET SESSION app.venue_ids = ARRAY[{venue_ids}]::uuid[]"))
    return conn
```

---

## 11. WebSocket Authorization Strategy

### Connection Flow

```
Client                              Server
  │                                    │
  │  CONNECT ws://host/ws?token=<JWT>   │
  │───────────────────────────────────>│
  │                                    │ [1] Validate JWT signature + expiry
  │                                    │ [2] Resolve ActorContext
  │                                    │ [3] Verify tenant active
  │                                    │ [4] Verify user active
  │                                    │ [5] Resolve venue subscriptions
  │                                    │
  │  101 Switching Protocols           │
  │<───────────────────────────────────│
  │                                    │
  │  { type: "subscribe", venue: id }  │
  │───────────────────────────────────>│
  │                                    │ [6] Validate venue scope
  │                                    │
  │  { type: "subscribed", venue: id } │
  │<───────────────────────────────────│
```

### Scope Revalidation

- Every 60 seconds, server re-validates the WebSocket's ActorContext
- If user/membership/tenant/venue state changed: send close frame (code 4001) with reason
- Client must re-authenticate to reconnect

### Event-Level Authorization

Each event pushed to the WebSocket carries venue scope. The server filters events based on the connection's venue subscriptions. No client-side filtering.

---

## 12. Implementation Plan (Task 5.2 onward)

### Task 5.2 — Contract Extensions

- Add `TenantId`, `VenueId`, `UserId` to `contracts/common/ids.py`
- Add `ActorContext` to `contracts/common/` as a frozen dataclass
- Add canonical role/permission enums to `contracts/common/`
- Update all contracts where tenant/venue/user identity is relevant

### Task 5.3 — Database Models & Migrations

- Initialize Alembic for migration management
- Create SQLAlchemy ORM models: `tenants`, `venues`, `users`, `roles`, `memberships`
- Add migration for initial schema
- Add `tenant_id` foreign key to each domain table

### Task 5.4 — Authentication

- Add JWT creation/validation utilities (`python-jose` or `PyJWT`)
- Add `POST /auth/login` endpoint → returns access + refresh tokens
- Add `POST /auth/refresh` endpoint
- Add `POST /auth/logout` endpoint → revoke token in Redis blacklist
- Add FastAPI dependency `get_actor_context()` that validates JWT and resolves context

### Task 5.5 — Authorization Middleware

- Implement `require_permission(permission: str)` FastAPI dependency factory
- Implement `require_venue_access(venue_id: UUID)` FastAPI dependency
- Implement `require_tenant_access()` FastAPI dependency (implicit from ActorContext)
- Create permission-checking decorator/dependency for all protected routes

### Task 5.6 — Repository Scoping

- Refactor all repository methods to accept `ActorContext`
- Add tenant/venue filtering to all repository queries
- Add test fixtures for multi-tenant scenarios
- Implement scoped repository tests

### Task 5.7 — RLS Implementation

- Write RLS migration policies for each tenant-scoped table
- Implement PostgreSQL context propagation in `DatabaseClient`
- Add RLS bypass connection for admin/migration operations
- Test RLS with direct SQL (bypass application layer)

### Task 5.8 — User & Membership Management API

- `GET/POST /api/v1/users`
- `GET/PUT/DELETE /api/v1/users/{id}`
- `GET/POST /api/v1/memberships`
- `GET/PUT/DELETE /api/v1/memberships/{id}`
- `GET /api/v1/venues`
- `GET /api/v1/tenants/{id}/venues`

### Task 5.9 — WebSocket Auth Integration

- Add JWT validation to WebSocket upgrade
- Implement venue-scoped event subscriptions
- Add periodic scope revalidation
- Test unauthorized/scope-change disconnection

### Task 5.10 — Security Testing

- Integration tests for every threat model scenario
- Permission matrix tests (every role × every permission)
- Token lifecycle tests (issue, refresh, revoke, expiry)
- Multi-tenant isolation tests
- RLS bypass/confirm tests

---

## 13. Exit Criteria

- [ ] Task 5.1 document approved (this document)
- [ ] Security invariants reviewed
- [ ] Threat model reviewed
- [ ] RBAC matrix reviewed
- [ ] Domain relationship design approved
- [ ] Implementation plan sequenced and approved

**Task 5.1 is a design freeze — no code until this document is approved.**

---

*Document version: 1.0*
*Author: Engineering Team*
*Date: 2026-07-29*
