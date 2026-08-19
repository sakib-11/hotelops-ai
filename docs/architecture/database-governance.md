# Database Governance & Schema Design Freeze — Task 6.1

## Status

**Active — governance baseline.** This document defines the authoritative PostgreSQL + TimescaleDB database architecture for HotelOps AI *before* any additional schemas are implemented. It is the governing reference for all future schema work (Task 6.2 and beyond).

| Field | Value |
|-------|-------|
| **Task** | 6.1 — Database Governance & Schema Design Freeze |
| **Builds on** | ADR-003 — this document operationalizes the accepted decision into executable policy |
| **Date** | 2026-08-08 |
| **Owner** | Backend Engineer (schema & queries) — see [Ownership](../operations/ownership.md) |
| **Approval required for changes** | Tech Lead (schema changes affecting existing data → Tech Lead + Product) |

## 0. Scope and Freeze Statement

This document:

- **Establishes policy only.** It does not create tables, schemas, ORM models, repositories, or migrations.
- **Freezes the schema design process.** No new tables are added to the database until the domain boundaries in [Section 3](#3-schema-domains) are approved.
- **Extends the existing Task 5 architecture.** The identity/tenancy tables already exist and are the foundation. Task 6 must *extend*, never duplicate them.
- **Defines the migration and compatibility machinery** that all future schema changes must follow.

The database is currently at migration head **`015_constraint_index_review`** (verified `2026-08-09`).

---

## 1. Source-of-Truth Policy

**PostgreSQL + TimescaleDB is the authoritative persistent source of truth.** Every other store has a strictly bounded role:

| Store | Role | Never |
|-------|------|-------|
| **PostgreSQL + TimescaleDB** | Authoritative business, event, config, and analytics state (ADR-003) | — |
| **Redis** | Transport / cache / ephemeral coordination only (ADR-004) | Authoritative business storage |
| **Object storage (MinIO/S3)** | Binary/video/evidence *artifacts* only | Metadata, indexes, or any authoritative state |
| **Application memory** | Working state during a request | Anything that must survive restart |
| **AI / LLM output** | Derived intelligence only (ADR-002: deterministic core, LLM-last) | Operational truth; recommendations must remain linked to deterministic evidence |

Rules that follow:

1. Any value that must survive a restart or be shared between components **must** be written to PostgreSQL first.
2. Redis contents are **reconstructible** from PostgreSQL; PostgreSQL contents are never reconstructible from Redis.
3. Object storage holds bytes; PostgreSQL holds the references (`EvidenceRef.ref_uri`, asset keys) and provenance.
4. LLM/AI output is stored **as derived records** with links to their source evidence — never trusted as the system of record.

---

## 2. Database Architecture

### 2.1 Stack (current, verified)

| Component | Technology | Evidence |
|-----------|-----------|----------|
| Database server | **TimescaleDB 2.19.0 on PostgreSQL 17** | `infrastructure/docker/compose.yaml` |
| Driver | **asyncpg** (`postgresql+asyncpg://`) | `backend/app/infrastructure/config.py` (`database_url`) |
| ORM | **SQLAlchemy 2.0 async** (`create_async_engine`, `async_sessionmaker`) | `backend/app/infrastructure/database/client.py` |
| Declarative base | Single `Base(DeclarativeBase)` — one metadata registry | `backend/app/infrastructure/database/base.py` |
| Migrations | **Alembic 1.18** (`script_location = database/migrations`) | `database/alembic.ini` |
| Pool | `pool_size=5`, `max_overflow=10`, `pool_pre_ping=True`, `expire_on_commit=False` | `client.py` |

### 2.2 Layers

```
Application authorization (ActorContext)
        ↓
Repository scope (WHERE tenant_id = :actor_tenant)          backend/app/infrastructure/database/repositories/
        ↓
PostgreSQL RLS (policy via app.tenant_id, SET LOCAL)        backend/app/infrastructure/database/rls.py
```

- **ORM models** live in `backend/app/infrastructure/database/models/` and must inherit `Base`.
- **Repositories** own every query; repository methods are actor-scoped (`get_for_actor`, `list_for_actor`, `update_for_actor`, `delete_for_actor`, `count_for_actor`) and return `None`/empty rather than leaking existence of foreign rows.
- **RLS** is a fail-closed, defense-in-depth layer beneath application authorization, enforced by the `hotelops_app` runtime role (`NOBYPASSRLS`) and `FORCE ROW LEVEL SECURITY`.
- **Migrations** are the only way schema changes reach the database (see [Section 12](#12-migration-policy)).

### 2.3 Migration layout (corrected in this task)

Standard Alembic layout is now used:

```
database/
  alembic.ini
  migrations/
    env.py                      # async env, loads identity models into Base.metadata
    script.py.mako              # revision template
    versions/                   # ← version scripts live here (previously at migrations/ root)
      001_create_identity_tables.py
      002_enable_rls.py
```

**Verified** (`2026-08-08`): `alembic heads` → single head `007_operational_config_schema`; empty-database `alembic upgrade head` succeeds; `alembic current` reports the head; schema verified against scratch TimescaleDB databases by `tests/integration/test_migrations.py` (head validation, full inventory, drift check).

---

## 3. Schema Domains

Each domain below defines a **boundary**, not a table list. No table is created by this task. For every domain the following must be answerable before schema work begins: purpose, owner, primary entity, dependencies, tenant scope, write pattern, read pattern, whether time-series storage is appropriate, and whether relational storage is sufficient.

Conventions used below:

- **Tenant scope — DIRECT**: table carries a `tenant_id` FK column (RLS policy on `tenant_id`).
- **Tenant scope — DERIVED**: no `tenant_id` column; ownership flows through a required FK to a tenant-scoped parent (RLS policy via subquery, e.g. `membership_venues_all`).
- **Tenant scope — GLOBAL**: platform-level catalog, no tenant ownership (isolation is logical via membership).

### 3.1 Tenancy (Task 5 — exists, frozen)

| Attribute | Value |
|-----------|-------|
| Purpose | Tenant/venue/identity/RBAC core |
| Owner | Backend Engineer (schema) + Security Lead (auth model) |
| Primary entity | `tenants` |
| Entities (existing) | `tenants`, `venues`, `users`, `roles`, `permissions`, `role_permissions`, `memberships`, `membership_venues` |
| Dependencies | None — root of the ownership graph |
| Tenant scope | DIRECT: `tenants`, `venues`, `memberships`, `membership_venues` (RLS). GLOBAL: `users`, `roles`, `permissions`, `role_permissions` (isolation via membership) |
| Write pattern | Low volume, admin-driven, transactional |
| Read pattern | Authorization hot path (membership/role lookups per request) |
| Time-series? | **No** |
| Relational sufficient? | **Yes** — already implemented |

### 3.2 Video

| Attribute | Value |
|-----------|-------|
| Purpose | Video asset, camera, stream, and session metadata (bytes live in object storage; PG holds references) |
| Owner | Backend Engineer + CV Engineer |
| Primary entity | `video_sessions` |
| Candidate entities | `video_assets`, `cameras`, `streams`, `video_sessions`, recorded-source metadata |
| Dependencies | Tenancy (venue) — session/asset belong to a venue |
| Tenant scope | DERIVED via venue (`camera.venue_id → venue.tenant_id`); or DIRECT `tenant_id` on session tables |
| Write pattern | Medium: session lifecycle open/close, asset registration, ingest records |
| Read pattern | Operational (session state) + playback metadata |
| Time-series? | Session/asset state: **No**. Per-frame/stream telemetry: candidate (see Section 11) |
| Relational sufficient? | **Yes** for metadata. Video bytes never in PG |

### 3.3 Configuration

| Attribute | Value |
|-----------|-------|
| Purpose | Camera configuration, analysis configuration, thresholds, operational configuration |
| Owner | Backend Engineer (schema); Product (value semantics) |
| Primary entity | `camera_configs` / `analysis_configs` |
| Dependencies | Tenancy (venue/tenant) |
| Tenant scope | DIRECT `tenant_id` (tenant-owned); venue-level config derives through venue |
| Write pattern | Low, admin UI, versioned |
| Read pattern | Hot path — pipeline reads config per session/frame |
| Time-series? | **No** (current-state). Config change history is relational |
| Relational sufficient? | **Yes**. `JSONB` reserved for adapter-specific flexible parameters only |

**Implemented — Task 6.5 (`007_operational_config_schema`).** Two *typed* tables (no generic key-value store): `camera_configs` (per-camera analysis configuration: `analysis_enabled`, `frame_rate`, `width`/`height`, `detection_sensitivity`) and `analysis_configs` (per-venue typed analysis profiles with thresholds: `confidence_threshold`, `occupancy_threshold`, `dwell_time_seconds`, `queue_length_threshold`, `wait_time_seconds`). Effective-state via a `config_status` enum (`draft`/`active`/`archived`); relational change history via a `version` column unique per scope (`(camera_id, version)`, `(venue_id, name, version)`). Unique-active rules are partial unique indexes `WHERE status = 'active'` (at most one active config per camera; at most one active profile per `(venue, name)`). JSONB `parameters` columns exist only for genuinely variable adapter/zone data. Both tables carry direct `tenant_id`, composite FKs to `cameras`/`venues`, `created_at` + `updated_at`, and RLS policies in the same migration.

### 3.4 Events

| Attribute | Value |
|-----------|-------|
| Purpose | Durable operational event stream: event-envelope persistence, operational events, detections/tracks where persistence is required |
| Owner | Backend Engineer |
| Primary entity | `operational_events` |
| Dependencies | Video (session), tenancy, configuration (rule trace) |
| Tenant scope | DIRECT `tenant_id` |
| Write pattern | **HIGH** — the primary write-heavy workload (up to 16 simultaneous streams per instance; events per second) |
| Read pattern | Time-range dashboard queries, filtered by zone/type; aggregation for rollups |
| Time-series? | **Yes — primary hypertable candidate** (event_time / ingestion_time) |
| Relational sufficient? | No alone — requires TimescaleDB hypertable + compression + continuous aggregates |
| Timestamps | Must preserve `event_time` vs `ingestion_time` vs `processing_time` (see Section 6) |

**Implemented — Task 6.6 (`008_operational_events`).** `operational_events` is a TimescaleDB hypertable partitioned on `event_time` (single time dimension). It persists the Task 4 `EventEnvelope` exactly — no competing event structures: envelope metadata is typed columns (`event_id`, `event_type`, `schema_version`, `event_time`, `produced_at`, `source`, `correlation_id`, `causation_id`); the generic envelope `payload` (detection/track observations travel inside it) is JSONB. Explicit `ingestion_time` (server `now()`, the row-creation timestamp for the append-only log) and nullable `processing_time`; CHECKs guarantee ingestion/processing never precede `event_time`. Direct `tenant_id` + composite FKs (venue/camera/session) with a new `uq_video_sessions_session_tenant` target. RLS + grants in the same migration. **No retention/compression policies are configured** — client-configurable per the privacy baseline; continuous aggregates remain future work (raw hypertable is source of truth).

### 3.5 Evidence

| Attribute | Value |
|-----------|-------|
| Purpose | Evidence references, evidence packages, frames/clips/artifacts, provenance |
| Owner | Backend Engineer + Security Lead |
| Primary entity | `evidence_packages` / `evidence_refs` |
| Dependencies | Events, video sessions, object storage (artifacts) |
| Tenant scope | DIRECT `tenant_id`; provenance chain stays in-tenant |
| Write pattern | Low–medium, on-demand (evidence generation per rule hit / package request) |
| Read pattern | Evidence review UI, export |
| Time-series? | **No** for metadata (low volume, referential) |
| Relational sufficient? | **Yes** for metadata + provenance. Artifact bytes never in PG |

**Implemented — Task 6.7 (`009_evidence_persistence`).** Three tables persist the Task 4 evidence contracts — `evidence_refs` (one row per artifact reference: object key `ref_uri`, `ref_type` enum, typed content metadata `content_type`/`size_bytes`/`checksum` sha256-CHECKed, provenance FKs to `operational_events` (`(event_time, event_id)` hypertable PK pair, both-or-neither CHECK), `video_sessions`, `cameras`, `captured_at`/`created_at`), `evidence_packages` (bounded collections), and the composite-PK M2M join `package_evidence_refs`. Artifact bytes never enter PG — only references. Direct `tenant_id` + composite FKs everywhere (cross-tenant links rejected), RLS + grants in the same migration, cascade orphan prevention. `video_assets.evidence_ref` deliberately stays a bare-UUID forward reference: wiring it would create a table dependency cycle (evidence_refs -> video_sessions -> video_assets -> evidence_refs) that SQLAlchemy cannot sort — provenance to video context flows through `evidence_refs.session_id`/`camera_id` instead.

### 3.6 Analytics

| Attribute | Value |
|-----------|-------|
| Purpose | Metrics, aggregations, opportunities (occupancy, dwell, queue length, wait time — see Production Scope) |
| Owner | Backend Engineer (schema); Product (metric definitions) |
| Primary entity | `metrics` (time-series), `opportunities` (relational records) |
| Dependencies | Events, video sessions |
| Tenant scope | DIRECT `tenant_id` |
| Write pattern | High for metrics (derived rollups); low for opportunities |
| Read pattern | Dashboards, historical trends |
| Time-series? | **Yes** for metrics (hypertable + continuous aggregates); opportunities relational |
| Relational sufficient? | Hybrid — metrics need TimescaleDB; opportunity records are relational |

**Implemented — Task 6.8 (`010_analytics_storage`).** Three layers stay strictly separated: raw `operational_events` (008) / derived `metrics` (this migration, hypertable on `event_time`) / relational `opportunities` (candidate records). Persists the Task 4 contracts exactly: `MetricValue` → `metrics` (explicit metric identity `metric_name`+`metric_id`, typed `value` DOUBLE PRECISION + `unit`, sample `event_time` partition column, optional both-or-neither `window_start`/`window_end` CHECK, `ingestion_time` receipt timestamp, direct `tenant_id` + composite FKs to venues/sessions/cameras); `OpportunityCandidate` → `opportunities` plus M2M links `opportunity_metrics` (via the hypertable PK pair `(event_time, metric_id)`) and `opportunity_evidence_refs`. Continuous aggregates are deliberately NOT created (no premature optimization — the raw hypertable is the source of truth; rollups wait for real dashboard query patterns, 11.4 rule 5). RLS + grants in the same migration.

### 3.7 AI

| Attribute | Value |
|-----------|-------|
| Purpose | Findings, recommendations, AI execution metadata (ADR-002: derived intelligence only) |
| Owner | AI Engineer |
| Primary entity | `findings` / `recommendations` |
| Dependencies | Evidence, events |
| Tenant scope | DIRECT `tenant_id` |
| Write pattern | Low — LLM-derived, bounded workflow, always linked to source evidence |
| Read pattern | Recommendation review UI |
| Time-series? | **No** (stateful, low volume) |
| Relational sufficient? | **Yes**; execution metadata relational or event-logged |

**Implemented — Task 6.9 (`011_ai_domain_storage`).** Three tables persist the Task 4 intelligence contracts as DERIVED data (ADR-002: LLM-last): `findings` (finding identity, `evidence_package_id` source link, `finding_type`, `description`, bounded `confidence` [0,1] CHECK, `event_time`) and `recommendations` (recommendation identity, optional `opportunity_id` link, `description`, `priority` enum matching the contract Priority) plus the composite-PK M2M join `recommendation_findings` (the contract `finding_ids`). Both carry DB-level review workflow state (`finding_status` proposed/accepted/rejected/archived; `recommendation_status` pending/accepted/rejected/implemented/archived) with `updated_at` transitions, `schema_version` (two-axis versioning), and nullable `model_name`/`model_version` provenance. AI outputs reference their source context via real composite FKs and can never modify authoritative operational data — no write-back path exists; arbitrary LLM conversations are never stored as business truth (JSONB `metadata` for variable model context only). Evidence/opportunity links are `ON DELETE RESTRICT` — cited source context is never silently destroyed (composite FKs with a denormalized `tenant_id` cannot `SET NULL`; retention tooling must unlink first). Direct `tenant_id` + composite FKs everywhere, RLS + grants in the same migration. Relational, not hypertables (governance 3.7 / 11.3).

### 3.8 Alerts

| Attribute | Value |
|-----------|-------|
| Purpose | Alerts, alert state, alert delivery information |
| Owner | Backend Engineer + Security Lead |
| Primary entity | `alerts` |
| Dependencies | Events / findings |
| Tenant scope | DIRECT `tenant_id` |
| Write pattern | Medium, stateful lifecycle (raised → acknowledged → resolved/expired) |
| Read pattern | Alert-center dashboard, hot path |
| Time-series? | Current state: **No**. Alert *history* for reporting: defer decision until volume is known |
| Relational sufficient? | **Yes** for state + delivery records |

**Implemented — Task 6.10 (`012_alert_approval_storage`).** `alerts` persists the Task 4 `Alert` contract (alert identity, `alert_type`, `severity` enum matching the contract `Severity`, `title`, `description`, `event_time`, polymorphic `source_ref` as two real composite FKs to `findings`/`recommendations` with an at-most-one CHECK) plus direct tenant/venue ownership and an explicit `alert_status` lifecycle enum (raised/acknowledged/resolved/expired). **Explicit state transitions** — no boolean combinations: transition legality is DB-enforced by `BEFORE UPDATE` triggers (a CHECK cannot compare OLD/NEW); raised→acknowledged/resolved/expired and acknowledged→resolved/expired are legal, terminal states are immutable, illegal updates RAISE and roll back (tested).

### 3.9 Approvals

| Attribute | Value |
|-----------|-------|
| Purpose | Approval requests and immutable approval decisions |
| Owner | Backend Engineer |
| Primary entity | `approval_requests` |
| Dependencies | Alerts / findings / recommendations / configuration changes |
| Tenant scope | DIRECT `tenant_id` |
| Write pattern | Low, transactional workflow; decisions append-only |
| Read pattern | Approval UI |
| Time-series? | **No** |
| Relational sufficient? | **Yes** (state machine + decision history rows) |

**Implemented — Task 6.10 (`012_alert_approval_storage`).** `approval_requests` persists the Task 4 `ApprovalRequest` contract — request identity, subject (`recommendation_id` composite FK), actor/context (`requested_by` FK → users), explicit `approval_status` enum (pending/approved/rejected/cancelled, the contract `ApprovalStatus`), `requested_at`/`resolved_at`/`reason` timestamps. `approval_decisions` is the APPEND-ONLY decision history (governance 3.9: decisions append-only) — actor, decision, reason, decided_at — with a partial unique index (`uq_approval_decisions_terminal`) guaranteeing at most one terminal decision per request (duplicate-approval guard). Transition legality is DB-enforced by a `BEFORE UPDATE` trigger (pending→approved/rejected/cancelled; terminal states immutable).

### 3.10 Integrations

| Attribute | Value |
|-----------|-------|
| Purpose | External systems (POS/PMS/staffing per integration scope), configuration metadata, execution records |
| Owner | Backend Engineer |
| Primary entity | `integrations` / `integration_executions` |
| Dependencies | Tenancy; outbox for outbound publications |
| Tenant scope | DIRECT `tenant_id` where tenant-owned; global entries only for platform-level catalogs |
| Write pattern | Low–medium |
| Read pattern | Low (config + execution status) |
| Time-series? | Config: **No**. Execution records: only if volume demands — defer |
| Relational sufficient? | **Yes** |

**Implemented — Task 6.11 (`013_integration_storage`).** `integrations` persists one row per external integration (POS/PMS/staffing/storage adapter families from integration-scope.md) — integration identity, direct `tenant_id`/venue ownership (composite FK), `provider_type`/`provider_name`, explicit `integration_status` lifecycle enum (pending/active/disabled/error, **no boolean flags**) with trigger-enforced transitions (pending→active/disabled/error; active→disabled/error; error→active/disabled; disabled→active; illegal transitions RAISE), non-sensitive `config_metadata` JSONB, `external_identifier`, `created_at`/`updated_at` timestamps. **Secrets posture (security architecture task 5.1): NO secrets are stored in relational columns** — `secret_ref` is a *reference* to the credential location (e.g. an env-var name or external secret-store key), resolved from the existing Settings/environment at runtime; no secrets-management platform and no second encryption system were invented. A DB CHECK via an IMMUTABLE helper function rejects secret-like keys in `config_metadata` using the audit contract's exact blocked-term vocabulary and first-segment semantics ('secret_key' blocked, 'api_key' allowed). Duplicate-provider constraint: at most one ACTIVE integration per `(tenant_id, provider_name)` via a partial unique index (007 pattern). Relational, not hypertables; execution records deliberately not created (governance 3.10 defers until volume demands). RLS + grants in the same migration.

### 3.11 Audit

| Attribute | Value |
|-----------|-------|
| Purpose | Security/operational audit events (privacy baseline: immutable, append-only; ≥90 days recommended) |
| Owner | Security Lead |
| Primary entity | `audit_events` |
| Dependencies | **None** — self-contained. Must survive tenant deletion; `tenant_id` recorded as a *value*, never a cascade-deleting FK |
| Tenant scope | Recorded tenant (value), but the table is **globally append-only**, written by the service |
| Write pattern | Medium–high, append-only |
| Read pattern | Security review, export/SIEM |
| Time-series? | Candidate (append-only, retention + compression) — must preserve immutability |
| Relational sufficient? | Append-only log; hypertable only if volume warrants |

**Implemented — Task 6.12 (`014_audit_outbox_inbox`).** `audit_events` persists the Task 4 `AuditEvent` contract — **trusted actor context only**: `actor_id`/`tenant_id`/`membership_id`/`venue_id` are recorded as VALUES from the server-side ActorContext (never client-supplied; no FKs so the log survives tenant/user deletion, governance 10.3), plus `action`, `action_category` (enum matching the contract `AuditActionCategory`), `correlation_id`, `timestamp`, and `metadata` — **no secrets**: a CHECK reuses the shared IMMUTABLE `integration_config_has_secret` helper (migration 013) with the contract's exact first-segment blocked-term semantics (`secret_key` blocked, `api_key` allowed). **Append-only enforced by grants**: `hotelops_app` gets `SELECT, INSERT` only — no UPDATE/DELETE grants at all (verified: the app role cannot tamper). Globally readable (no RLS — platform infrastructure), Security-Auditor access is application RBAC.

### 3.12 Outbox

| Attribute | Value |
|-----------|-------|
| Purpose | Transactional outbox for reliable publication (Redis transport / integrations) |
| Owner | Backend Engineer |
| Primary entity | `outbox_events` |
| Dependencies | Any transactional write that must publish an event |
| Tenant scope | `tenant_id` recorded for scoping/claims |
| Write pattern | One row per transaction that must publish; row written **in the same transaction** as the business change |
| Read pattern | Worker poll → mark delivered → prune |
| Time-series? | **No** — short-lived rows (processed/pruned) |
| Relational sufficient? | **Yes**; idempotent delivery via unique event ID + `processed_at` |

**Implemented — Task 6.12 (`014_audit_outbox_inbox`).** `outbox_events` is the transactional outbox: domain state + outbox row COMMIT **atomically** in one transaction (verified — the publisher never touches Redis before the DB commit). Idempotent delivery via `uq_outbox_events_event_id` (at most one outbox row per event) + explicit trigger-enforced lifecycle (`outbox_status` pending→processing→published, failed→pending retry, published terminal) and lease-based worker claims (`claimed_by`/`claimed_until` for crash recovery). Partial index `ix_outbox_events_pending` on the worker's hot pending subset (governance 9 rule 3). `tenant_id` is a recorded value for scoping — NOT an RLS-scoped table (workers poll across all tenants; documented in the migration header).

### 3.13 Inbox

| Attribute | Value |
|-----------|-------|
| Purpose | Idempotent inbound message/event processing from external systems |
| Owner | Backend Engineer |
| Primary entity | `inbox_messages` |
| Dependencies | Integrations |
| Tenant scope | `tenant_id` recorded |
| Write pattern | Low–medium |
| Read pattern | Worker poll + deduplication lookup |
| Time-series? | **No** — short-lived rows |
| Relational sufficient? | **Yes**; idempotency via unique key on `(source, source_message_id)` |

**Implemented — Task 6.12 (`014_audit_outbox_inbox`).** `inbox_messages` gives idempotent inbound processing: **deduplication via `uq_inbox_messages_source_message_id` on `(source, source_message_id)`** — duplicate delivery is rejected at the unique key (verified), and processing is idempotent (claim → process → `processed_at` stamped; re-delivery detected). Explicit trigger-enforced lifecycle (`inbox_status` pending→processing→processed, failed→pending retry, processed terminal) + lease-based claims. Partial index `ix_inbox_messages_pending` on the worker's hot subset. `tenant_id` recorded for scoping — NOT an RLS-scoped table (worker must poll all tenants; documented in the migration header).

---

## 4. Naming Conventions

| Item | Convention | Examples (existing) |
|------|-----------|---------------------|
| Table names | `snake_case`, plural | `tenants`, `memberships`, `membership_venues` |
| Column names | `snake_case` | `created_at`, `tenant_id` |
| Primary key | `<entity>_id` (`UUID`) | `tenant_id`, `venue_id` |
| Foreign key | `<referenced_entity>_id` | `venue.tenant_id`, `membership.user_id` |
| Index names | `ix_<table>_<column(s)>` | `ix_venues_tenant_id`, `ix_memberships_tenant_user` |
| Unique constraint names | `uq_<table>_<column(s)>` | `uq_users_email`, `uq_roles_name`, `uq_permissions_name` |
| Enum type names | `snake_case` singular | `tenant_status`, `membership_scope`, `role_name` |
| Association tables | `<entity_a>_<entity_b>` (composite PK) | `role_permissions`, `membership_venues` |
| Migration revisions | `<NNN>_<snake_case_description>` | `001_create_identity_tables`, `002_enable_rls` |
| JSONB columns | `metadata` (nullable) unless domain requires more | `tenants.metadata`, `users.metadata` |

**Association tables** use the two FKs as a composite primary key (e.g., `role_permissions(role_id, permission_id)`) plus indexes for the query side.

---

## 5. UUID Convention

- **Single ID system.** Every primary key is a PostgreSQL `uuid` backed by `UUID(as_uuid=True)` in SQLAlchemy, generated as **UUIDv4** via `contracts.common.ids.new_uuid()` (`uuid4()`).
- **Typed NewTypes.** Contract layer wraps IDs as NewTypes (`TenantId`, `VenueId`, `EventId`, `EvidenceId`, …) — see `contracts/common/ids.py`. Domain code and repositories use the typed IDs; the DB stores raw UUIDs.
- **No integer sequences, no surrogate auto-increment keys** anywhere in the schema.
- **No multiple ID systems** without an architectural justification and ADR. A second ID (e.g., external system identifiers, camera serials) is a *business attribute* column (unique where required), not a replacement primary key.
- RLS session context carries tenant UUIDs as strings via `SET LOCAL app.tenant_id`; values are validated UUIDs (never user input) — see `backend/app/infrastructure/database/rls.py`.

---

## 6. UTC Convention

- **All persisted timestamps are `timestamptz`** — SQLAlchemy `DateTime(timezone=True)`, PostgreSQL `TIMESTAMP WITH TIME ZONE`. Local server time is never stored.
- **Server-side defaults**: `server_default=func.now()` for `created_at` (PostgreSQL `now()` is UTC-aware). Application-side defaults must use `datetime.now(UTC)` (never `datetime.now()`).
- **Contract validation**: canonical datetimes are validated timezone-aware via `contracts.common.time.validate_utc`; naive datetimes are rejected. Serialization uses `serialize_utc` (ISO-8601 with `+00:00`).
- **Timestamp semantics are distinct and must not be collapsed** (see `contracts/common/time.py` and `EventEnvelope`):

| Column concept | Meaning |
|----------------|---------|
| `event_time` | When the represented real-world event occurred (camera time, corrected to UTC) |
| `ingestion_time` / `ingested_at` | When HotelOps received the data |
| `processing_time` / `processed_at` | When processing occurred |
| `created_at` | When the canonical object/row was created |
| `updated_at` | When the row was last modified (only where mutation is legal) |

Recorded video may be processed days after the event — never silently treat processing time as event time.

- **Timestamps are NOT used as primary keys** or for equality joins; they are ordering/query dimensions.

---

## 7. JSONB Policy

- **JSONB is for genuinely flexible/semi-structured data only.** Existing use: nullable `metadata` columns on identity tables — acceptable for extensible annotations.
- **JSONB is not an excuse to avoid relational design.** If a value is queried, filtered, joined, or constrained, it must be a typed column.
- **Rules:**
  1. No JSONB column may hold data that is the subject of a `WHERE`, `JOIN`, or `CHECK` that relational columns should express.
  2. JSONB columns are nullable by default; a JSONB column must justify itself in schema review.
  3. Keys/values in JSONB are contract-documented, not ad-hoc.
  4. Sensitive data (credentials, PII beyond policy) is never stored in JSONB (see audit metadata validation in `contracts/audit/models.py`).

---

## 8. FK & Constraint Policy

- **Relationships are explicit.** Every cross-table reference is a real `FOREIGN KEY` — never an unconstrained ID column.
  - **Documented exceptions (bare-UUID forward references):**
    - `video_assets.evidence_ref` (Task 6.7): a composite FK would create a table dependency cycle (`evidence_refs → video_sessions → video_assets → evidence_refs`) that SQLAlchemy cannot sort. Provenance from evidence to its video context flows through `evidence_refs.session_id` / `camera_id` / `event_id` instead; the asset→evidence direction is an advisory link until a deliberate design decision (with ADR if wired).
    - `metrics.source_ref` (Task 6.8): the analysis-jobs table does not exist yet; the column becomes a real FK when that schema lands.
- **`NOT NULL`** is applied whenever the domain requires the value (all FK columns, names, statuses, timestamps are `NOT NULL` in the existing schema).
- **`ON DELETE` semantics are explicit per relationship:**
  - Ownership cascade: `ondelete="CASCADE"` (e.g., `venues.tenant_id`, `memberships.tenant_id`).
  - Restrict/`SET NULL` where deletion must be prevented or provenance preserved — chosen deliberately, never defaulted silently.
- **CHECK constraints** are used for important invariants at the database level (enum-like ranges not covered by types, cross-column invariants). Enum columns use native PostgreSQL `ENUM` types (created by migrations) — see Section 12 for the enum lifecycle rule.
- **Unique constraints** define business uniqueness explicitly (e.g., `uq_users_email`, `uq_roles_name`, `uq_permissions_name`); inbox idempotency keys are unique constraints as well.
- Every constraint must exist in **both** the ORM model and a migration; the migration is authoritative.

---

## 9. Indexing Policy

- **Indexes are justified by query patterns, not by column count.** Never index every column.
- Existing indexes (verified in the live schema) follow the convention:

| Index | Justification |
|-------|---------------|
| `ix_venues_tenant_id` | Every venue query is tenant-scoped |
| `ix_memberships_user_id`, `ix_memberships_tenant_id`, `ix_memberships_role_id` | Membership lookups per request |
| `ix_memberships_tenant_user` (composite) | Auth resolution: user → tenant membership |
| `ix_membership_venues_venue` | Venue-scoped membership lookups |

- **Rules:**
  1. Every proposed index must cite the query pattern it serves (schema review checklist item).
  2. Composite indexes are preferred over multiple single-column indexes for common multi-column predicates.
  3. Partial indexes are allowed where a subset of rows is hot (e.g., `WHERE status = 'pending'` on outbox/inbox).
  4. Indexes are added via migrations (`op.create_index`), never inline DDL in application code.
  5. B-tree is the default; use GIN for JSONB only when the JSONB column is genuinely queried and justified.

### 9.1 Constraint & Index Review (Task 6.13, verified against the live catalog 2026-08-09)

Every Task 6 table (migrations 005–014) was audited against the live PostgreSQL
catalog with a left-prefix redundancy analysis. Each constraint was accepted for
the invariant it protects; each index was accepted only for a real query pattern.

**Constraints — what each protects:**

| Constraint class | Invariant protected |
|------------------|---------------------|
| Composite PK `(event_time, id)` on hypertables | TimescaleDB requires the partition column inside every unique constraint; the PK is also the partitioning index (008, 010) |
| Composite unique `(id, tenant_id)` | FK *target* for composite foreign keys (migration 003 pattern) — enables cross-tenant-reference rejection in every child table |
| `uq_*_version` (`(camera_id, version)`, `(venue_id, name, version)`) | Versioned change history per scope — no duplicate version numbers |
| Partial unique `WHERE status = 'active'` | At most one active config per camera / per (venue, name) / per (tenant, provider) / one terminal approval decision |
| `uq_*_name` / `uq_users_email` | Global name/email uniqueness (platform catalogs) |
| CHECK not-empty / positive ranges | Empty strings and invalid measurements are rejected at the database |
| CHECK timestamp ordering (`ingestion_time >= event_time`, `window_end >= window_start`, etc.) | Event-time semantics (Section 6) — distinct timestamps, never collapsed |
| CHECK source consistency (`ck_video_assets_source_consistent`, `ck_video_sessions_status_consistent`, `ck_alerts_source_single`) | Mutually exclusive / paired columns cannot drift |
| NOT NULL `tenant_id`/`venue_id` | DIRECT ownership (Section 10.1) — every Task 6 table is tenant-scoped |

**Redundant indexes dropped (migration 015) — single-column left-prefixes of composites:**

| Dropped index | Served by | Query pattern preserved |
|---------------|-----------|-------------------------|
| `ix_camera_configs_camera_id` | `uq_camera_configs_version (camera_id, version)` | camera_id-scoped config lookup |
| `ix_analysis_configs_venue_id` | `uq_analysis_configs_version (venue_id, name, version)` + partial `uq_analysis_configs_active` | venue_id-scoped profile lookup |
| `ix_operational_events_event_time` | hypertable PK `(event_time, event_id)` (partitioning index) | global event_time range queries |

**Rejected as redundant (left-prefix detector false positives):**

- `*_pkey(id)` vs `uq_*_tenant(id, tenant_id)`: the composite uniques are FK
  *targets* for composite FKs (migration 003) — the PK alone cannot serve as a
  composite FK target. Both are required.
- `ix_integrations_tenant_id`: the covering `uq_integrations_active_provider` is
  **partial** (`WHERE status = 'active'`) and only indexes active rows; the full
  tenant index is required for pending/disabled/error rows.
- `ix_memberships_tenant_id`: a genuine left-prefix redundancy with
  `ix_memberships_tenant_user`, but memberships is a Task 2/3 identity table
  (migration 001) — OUT OF SCOPE for the Task 6 review; flagged for a future
  identity-schema cleanup.

**Important non-obvious indexes (documented deliberately):**

- Hypertable PKs `(event_time, event_id)` / `(event_time, metric_id)` are the
  time-dimension indexes (partitioning indexes); no separate single-column
  event_time index is needed (dropped in 015).
- Partial `ix_outbox_events_pending` / `ix_inbox_messages_pending` cover the
  workers' hot pending subset (rule 3); `tenant_id` on those tables is a
  recorded value, NOT an RLS column (workers poll across tenants).
- `ix_approval_decisions_request_id` is kept alongside the partial
  `uq_approval_decisions_terminal` — the partial unique only covers terminal
  decisions; the full index serves full decision-history per request.

---

## 10. Tenant Ownership Policy

Every table must have an unambiguous tenant ownership path. Two modes are permitted:

### 10.1 Direct ownership (preferred)

- The table carries `tenant_id UUID NOT NULL` with an FK to `tenants.tenant_id` (`ON DELETE CASCADE` unless the domain forbids it).
- RLS policy: `tenant_id = current_setting('app.tenant_id')::uuid` (see `002_enable_rls.py`).

### 10.2 Derived ownership (allowed only where the FK chain is mandatory and unambiguous)

- The table has **no** `tenant_id` column; ownership flows through a required FK to a tenant-scoped parent.
- RLS policy uses a subquery through the parent — the established pattern is `membership_venues_all` (scoped via `memberships`).
- Example (future): `cameras.venue_id → venues.tenant_id`; a camera can never exist without a venue.
- **Forbidden:** a table whose tenant ownership depends on an *optional* FK or a nullable chain — that is ambiguous ownership and is rejected in review.

### 10.3 Global tables

- `users`, `roles`, `permissions`, `role_permissions` are platform catalogs with **no** tenant ownership. Tenant isolation for users is enforced through `memberships` (logical), never RLS.
- Audit: globally append-only; `tenant_id` recorded as a value, no cascade FK (survives tenant deletion).

### 10.4 RLS enforcement rules (existing, binding)

1. Every **direct** or **derived** tenant-scoped table gets `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + policies `TO hotelops_app`.
2. Policies are **fail-closed**: when `app.tenant_id` is unset, the policy resolves to the zero-UUID and matches nothing.
3. The application runtime role `hotelops_app` is `NOBYPASSRLS`; migrations run as an admin role that bypasses RLS.
4. `SET LOCAL app.tenant_id` is transaction-scoped — cleared on commit/rollback, preventing pool leakage (verified by `tests/integration/test_rls.py`).
5. New tenant-scoped tables **must** ship with their RLS policies and grants in the same migration that creates them.

---

## 11. TimescaleDB Policy

TimescaleDB is **only** for high-volume time-series workloads. Not every table becomes a hypertable.

### 11.1 Decision framework

A table is a hypertable candidate only if it is **append-heavy**, queried **primarily by time ranges**, and expected to grow beyond what a plain table should hold without partitioning. Everything else stays relational.

### 11.2 Candidate tables (analysis — NOT implemented in this task)

| Candidate | Time dimension | Expected write volume | Query pattern | Compression / retention | Continuous aggregates | Decision |
|-----------|---------------|----------------------|---------------|-------------------------|----------------------|----------|
| `operational_events` | `event_time` (+ `ingestion_time`) | High — up to 16 streams, events per second | Time-range dashboards, zone/type filters | Compression after retention; retention **TBD client-configurable** (privacy baseline) | Yes — dashboard counts/rates | **✅ Hypertable (Task 6.6)** — compression/retention/continuous aggregates intentionally not yet configured |
| Detection/track observations (persisted subset) | `event_time` | High during active analysis | Time + zone queries, replay | Compression; retention TBD | Possible (zone counts) | **Hypertable** |
| `metrics` (occupancy, dwell, queue, wait) | sample time | High | Dashboards, historical trends | Compression; retention TBD | Yes — hourly/daily rollups | **Hypertable** |
| `audit_events` | event time | Medium–high, append-only | Security review, export | Compression; retention ≥90 days **recommended**, TBD client-configurable | No | Candidate — defer until volume known |

### 11.3 Explicitly NOT candidates

Tenancy core, configuration (current-state), evidence metadata, approvals, integrations, outbox/inbox (short-lived rows), alert *current state*.

### 11.4 Governance rules

1. **Hypertables are created only where the decision framework (11.1) approves.** Task 6.1 created none; Task 6.6 created `operational_events` (the primary candidate). Partition dimensions, compression and retention policies remain Task 6.2+ work — each requires a schema review entry and a migration, and retention is client-configurable.
2. **Do not invent retention periods.** Retention is client-configurable per deployment (see [Privacy Baseline](../security/privacy-baseline.md) — detection metadata and evidence retention are TBD; audit ≥90 days recommended). Retention policy constants must be added to configuration, not hard-coded in migrations.
3. Every hypertable candidate must record: time dimension, expected write volume, query pattern, retention consideration, and whether compression/continuous aggregates apply — before implementation.
4. Hypertables must not be used where relational storage suffices (e.g., config, approvals).
5. Continuous aggregates are used for rollups; raw hypertables remain the source of truth; aggregate freshness must be monitored.

---

## 12. Migration Policy

### 12.1 The migration workflow (binding)

```
Developer change
      ↓
Migration drafted (alembic revision — draft only, never blind-accepted)
      ↓
Migration reviewed (Section 13)
      ↓
Migration tested (Section 14)
      ↓
Upgrade from previous head (verified on scratch DB)
      ↓
Application compatibility tested
      ↓
CI validation: offline gate + single expected head + drift check
        (Task 6.14: make db-gate, migrations job in .github/workflows/ci.yml)
      ↓
Release
```

### 12.2 Rules

| Rule | Policy |
|------|--------|
| **Sole mechanism** | Every schema change is an Alembic migration under `database/migrations/versions/`. Never manually edit production schema as the normal workflow. |
| **Naming** | `<NNN>_<snake_case_description>.py` with `revision = "<NNN>_<snake_case_description>"`, linear chain (`down_revision` = previous head), no branches. Matches `001_…`/`002_…` style. |
| **One logical change per migration** | A migration does one thing (e.g., "create events tables" is one migration; "add column to events" is a separate migration). No unrelated schema in a migration. |
| **Upgrade** | `upgrade()` must be complete and self-contained; ENUM types are created by `op.create_table` from `sa.Enum` columns (each enum used by exactly one table) — do **not** pre-create them (duplicate `CREATE TYPE` fails). For an enum type shared by **multiple** tables, create the type once explicitly and use `create_type=False` on the `sa.Enum` columns of all but the first table. |
| **Downgrade** | Provide `downgrade()` where safe and cheap (existing 001/002 both do). Downgrade is a dev/rollback convenience, **not** the production recovery path. |
| **Roll-forward (primary)** | Production recovery is **roll-forward**: fix forward with a new migration. Destructive changes may omit downgrade when rollback is unsafe; document that decision in the migration header. |
| **Destructive migrations** | `DROP TABLE/COLUMN`, type changes that lose data, or rewrites require: (1) data migration first (copy/transform), (2) verified backup, (3) Tech Lead approval (+ Product for data-affecting per ownership), (4) ADR for architectural changes. Never ship a destructive migration in the same release as the application change that depends on it. |
| **Index creation** | Via `op.create_index` with a conventional name and a cited query pattern (Section 9). |
| **Constraint addition** | Backfill data before adding `NOT NULL`/`CHECK`; use `NOT VALID` + `VALIDATE` for large tables; constraints are named (`uq_…`/`ck_…`). |
| **Data migration** | Idempotent, batched, reversible where possible, tested against a copy of production data; never fused with the DDL in a way that loses roll-forward ability. |
| **Grants/RLS** | New tenant-scoped tables must include their `GRANT`s to `hotelops_app` and RLS policies in the same migration. Single-statement-per-`op.execute` (asyncpg rejects multi-statement strings). |
| **Cluster-wide objects** | Roles are cluster-wide. `hotelops_app` creation must be idempotent (`IF NOT EXISTS`); its downgrade requires that no other database in the cluster references it (verified behavior — leftover test policies in other DBs block `DROP ROLE`). |
| **Production review** | Schema changes affecting existing data require Tech Lead approval; breaking changes require an ADR (see [Ownership](../operations/ownership.md)). |

### 12.3 Migration header requirements

Every migration file must state: revision ID, `down_revision`, a one-line summary, and (for non-trivial or destructive migrations) a short notes block covering downgrade policy, data migration, and roll-forward considerations.

### 12.4 Destructive migration policy

A migration is **destructive** if it permanently removes or irrecoverably transforms data or schema: `DROP TABLE`/`DROP COLUMN`/`DROP CONSTRAINT` that discards information, `ALTER TYPE` on a column (rewrite + possible loss), removal of an enum value, data rewrites that discard values, or any operation that cannot be rolled forward from.

| Rule | Policy |
|------|--------|
| **Data first** | A destructive migration must be preceded (in an earlier migration) by a data migration that copies/transforms what is needed. Never drop before the successor path exists. |
| **Backup evidence** | A verified backup must exist and its verification must be recorded (who/when/restore-tested) before the destructive migration ships. |
| **Approval** | Tech Lead approval required; Product approval additionally required when business data is affected (see [Ownership](../operations/ownership.md)). Architectural changes also require an ADR. |
| **Release separation** | Never ship a destructive migration in the same release as the application change that depends on it. The dependency lands first, the removal lands later. |
| **Downgrade** | Destructive migrations may omit `downgrade()` when rollback is unsafe; the decision and rationale must be documented in the migration header. Rollback is not the recovery path — see 12.5. |
| **Review** | The destructive nature, backup evidence, and approval must be stated in the migration header (12.3) and checked in review (Section 13). |

### 12.5 Roll-forward policy

Production recovery is **roll-forward only**. Downgrades are a developer convenience for local work, never the production recovery path.

1. **Fix forward.** A failed or incorrect migration is repaired by a new migration on top of `head` — never by editing an already-shipped revision and never by `downgrade` + `upgrade` in production.
2. **Never rewrite shipped revisions.** Once a revision is merged (and certainly once applied to a shared or production environment), its `upgrade()` is immutable. Editing it would break the linear chain and drift detection. If it is wrong, the next migration corrects it.
3. **Atomicity is assumed.** Each migration applies atomically (single transaction); a failure leaves the database at the previous head, so the roll-forward fix starts from a known state (verified by the migration failure tests, Section 14).
4. **Data-migration idempotency.** Roll-forward data migrations must be idempotent so a partially observed run can be re-run safely.
5. **Downgrade scope.** `downgrade()` exists where safe and cheap (dev convenience, destructive-policy exceptions in 12.4); the CI rollback test exercises it, but operators must treat roll-forward as the only supported production path.

### 12.6 No blind auto-generation

`make db-revision` (`alembic revision -m`) produces a **draft only**. Autogenerated migrations are never merged without review: the draft must be inspected, corrected (indexes cited to query patterns, RLS/grants added for new tenant-scoped tables, constraint names, `server_default` parity), and pass the review checklist (Section 13) before merge. CI refuses to accept anything other than the reviewed linear head (Task 6.14 gate + Section 15 drift check).

---

## 13. Migration Review Policy

A migration is merged only after review — CI gates are a safety net, not a substitute for review (12.6). Review checklist:

- [ ] Linear revision chain; `down_revision` points at the current head; single head (`alembic heads`).
- [ ] One logical change only.
- [ ] `upgrade()` runs on an empty database; `alembic upgrade head` from the previous head verified.
- [ ] Downgrade/roll-forward path stated and (where safe) exercised.
- [ ] ENUM lifecycle correct (created by table, dropped after tables in downgrade).
- [ ] Every new tenant-scoped table: RLS policies + `FORCE RLS` + grants to `hotelops_app` in the same migration; ownership mode (direct/derived/global) declared.
- [ ] Naming conventions (Section 4); PKs are UUIDv4; timestamps are `timestamptz` with UTC semantics (Section 6).
- [ ] Indexes cited to query patterns (Section 9); JSONB justified (Section 7).
- [ ] Constraints named; `NOT NULL` only where the domain requires; business uniqueness explicit.
- [ ] No multi-statement `op.execute` (asyncpg).
- [ ] No inline DDL in application code; ORM models and migrations agree (`alembic check` in CI).
- [ ] Destructive/data migrations have approval + backup evidence per Section 12.
- [ ] Tech Lead sign-off (and Product where data is affected).

---

## 14. Testing Strategy

Implemented in `tests/integration/test_migrations.py` (221 tests; verified `2026-08-09`) plus the offline governance gate in `scripts/check_migrations.py` (12 tests in `tests/unit/test_migration_governance.py`). Integration tests run against per-test scratch TimescaleDB databases and are gated by the existing `INTEGRATION_TESTS=1` convention (same as `tests/integration/test_rls.py`):

    docker compose -f infrastructure/docker/compose.yaml up -d postgres
    INTEGRATION_TESTS=1 pytest tests/integration/test_migrations.py -v

CI runs both layers (Task 6.14): the **offline gate** (`make db-gate`, no database) on every push/PR, and the **database-backed suite** in the `migrations` job against a fresh TimescaleDB service container (`make db-integration`).

| Test | What it proves |
|------|----------------|
| **Empty database upgrade** | `alembic upgrade head` on a fresh database succeeds end-to-end (verified for current head on `2026-08-08`) |
| **Upgrade from previous migration** | Applying `head-1`, then `head`, succeeds; incremental path matches full path |
| **Migration head validation** | `alembic heads` is a single head equal to the repository's `EXPECTED_HEAD`; `alembic current` on CI DB matches |
| **Schema constraints** | `NOT NULL` and `CHECK` reject invalid rows |
| **Foreign keys** | Violating FK insert/update/delete is rejected; `ON DELETE` semantics behave as declared |
| **Unique constraints** | Business uniqueness enforced (email, role name, inbox idempotency keys) |
| **Indexes** | Expected indexes exist (via catalog inspection, mirroring `tests/unit/test_identity_models.py`) |
| **Tenant isolation** | RLS tests extended to every new tenant-scoped table (cross-tenant select/insert/update/delete denied; missing context fails closed) |
| **Timestamp correctness** | Columns are `timestamptz`; server default is UTC; naive datetimes rejected by contract validation |
| **Application/database compatibility** | Startup readiness check fails on unsupported schema head (Section 15) |
| **Migration failure** | A failing migration rolls back atomically — no partial schema, DB remains at the previous head |
| **Migration rollback (where supported)** | Non-destructive migrations downgrade cleanly |
| **Roll-forward (where rollback unsafe)** | Destructive migrations: upgrade path + data-migration idempotency tested; downgrade explicitly documented as unsafe |

---

## 15. Schema/Application Compatibility Policy

- **Schema version = Alembic revision.** The database's schema head is the value in `alembic_version`; the repository's schema head is the single `alembic heads` result. Today both are `015_constraint_index_review`.
- **CI verifies (Task 6.14, wired into `.github/workflows/ci.yml`):**
  1. Offline governance gate (`scripts/check_migrations.py`, `make db-gate`) — every migration file is syntactically valid Python; the graph has exactly one head equal to the repository's expected-head constant; the chain is linear; no missing/broken/orphan revisions; filename ordering ascends with the chain.
  2. `alembic heads` yields exactly one head, equal to the expected head constant in the repository.
  3. `alembic check` (drift) passes — ORM `Base.metadata` and migrations agree (no untracked schema changes).
  4. Migration tests (Section 14) run against an ephemeral TimescaleDB service: empty-DB upgrade, upgrade from the previous expected head, RLS, atomicity, rollback, roll-forward.
- **Application must not silently run against an unsupported schema.** The readiness/health layer must compare the live DB revision against the minimum supported head and fail (or refuse requests) on mismatch — the DB schema is part of the deployment contract.
- **Two independent version axes:**
  - *Database schema head* — Alembic revisions (this document).
  - *Contract schema version* — `SCHEMA_VERSION` (`contracts/common/versioning.py`, currently `1.0`), governing wire/event compatibility. They evolve together in releases but are tracked separately; a contract bump without a migration (or vice versa) is a release-blocking inconsistency.
- **Compatibility matrix rule:** application version N declares the minimum DB head it supports; releases document the mapping. Deploy database migrations **before** rolling application code.

---

## 16. Current-State Assessment (verified 2026-08-08)

| Area | State |
|------|-------|
| Identity/tenancy schema | ✅ `tenants`, `venues`, `users`, `roles`, `permissions`, `role_permissions`, `memberships`, `membership_venues` (migration `001`) |
| RLS + app role | ✅ `hotelops_app` (`NOBYPASSRLS`), fail-closed policies, `FORCE RLS` (migrations `002`, `006`, `007`) |
| Operational configuration schema | ✅ `camera_configs`, `analysis_configs` — typed config, version/effective-state, unique-active rules, composite-FK tenancy, RLS (migration `007`, Task 6.5) |
| Operational event storage | ✅ `operational_events` — TimescaleDB hypertable on `event_time`, envelope-shaped typed columns, explicit event/ingestion/processing times, composite-FK tenancy, RLS (migration `008`, Task 6.6) |
| Evidence persistence | ✅ `evidence_refs`, `evidence_packages`, `package_evidence_refs` — artifact references (bytes in object storage), typed content metadata + checksum validation, event/video provenance FKs, composite-FK tenancy, RLS (migration `009`, Task 6.7) |
| Analytics storage | ✅ `metrics` (hypertable on `event_time`), `opportunities` (relational), `opportunity_metrics` + `opportunity_evidence_refs` links — MetricValue/OpportunityCandidate contracts, composite-FK tenancy, RLS (migration `010`, Task 6.8) |
| AI domain storage | ✅ `findings`, `recommendations`, `recommendation_findings` — Finding/Recommendation contracts as derived records, evidence/opportunity RESTRICT links, review workflow status, model provenance, composite-FK tenancy, RLS (migration `011`, Task 6.9) |
| Alert & approval storage | ✅ `alerts`, `approval_requests`, `approval_decisions` — Alert/ApprovalRequest contracts, explicit state enums with trigger-enforced transitions (no boolean flags), append-only decisions with duplicate guard, composite-FK tenancy, RLS (migration `012`, Task 6.10) |
| Integration storage | ✅ `integrations` — provider/type/status/config-metadata/external-identifier persistence, trigger-enforced status lifecycle, duplicate-active-provider partial unique index, secret-ref-only secrets posture (no credential values in relational columns), composite-FK tenancy, RLS (migration `013`, Task 6.11) |
| Audit storage | ✅ `audit_events` — AuditEvent contract, trusted ActorContext identity (no client-supplied actor data), blocked-secret metadata CHECK, append-only via SELECT/INSERT-only grants, no FKs (survives tenant/user deletion), globally readable (migration `014`, Task 6.12) |
| Outbox storage | ✅ `outbox_events` — transactional outbox, atomic commit with domain state, unique event_id idempotency, trigger-enforced pending→processing→published lifecycle, lease-based worker claims, pending-subset partial index (migration `014`, Task 6.12) |
| Inbox storage | ✅ `inbox_messages` — idempotent inbound processing, `(source, source_message_id)` dedup key, trigger-enforced lifecycle + processed_at stamping, pending-subset partial index (migration `014`, Task 6.12) |
| Constraint & index review | ✅ **Task 6.13** — every Task 6 table audited against the live catalog; 3 redundant single-column indexes dropped (`ix_camera_configs_camera_id`, `ix_analysis_configs_venue_id`, `ix_operational_events_event_time`); detector false positives rejected (composite-unique FK targets, partial unique coverage); findings documented in Section 9.1 (migration `015`) |
| Alembic tooling | ✅ **Corrected** — version scripts moved to `database/migrations/versions/` (standard layout); `heads`/`history`/`upgrade`/`current` verified |
| Migration defects fixed | ✅ `sa.JSONB` → dialect `JSONB`; ENUM double-create removed; multi-statement `op.execute` split (asyncpg) |
| ORM model ↔ migration parity | ✅ Verified `2026-08-08`: `alembic check` reports zero drift against a scratch DB (models now carry the same `server_default` values as migrations); drift check to be wired into CI (Section 15) |
| Application/DB compatibility gate | ⏳ Readiness check on schema head to be implemented (Section 15); `alembic check` verified locally and enforced by `tests/integration/test_migrations.py` |
| Migration test suite (Section 14) | ✅ `tests/integration/test_migrations.py` — head validation, empty-DB upgrade, upgrade-from-previous, constraints, timestamps, indexes, drift, RLS on migrated schema, atomicity, rollback, roll-forward |
| Migration governance gate (Task 6.14) | ✅ **`scripts/check_migrations.py`** (`make db-gate`) — offline syntax/single-head/expected-head/linear-chain/missing-revision/ordering checks, head constant `015_constraint_index_review`; wired into CI quality job + new `migrations` job (TimescaleDB service: empty upgrade, prev-head upgrade, drift, integration suite); destructive-migration policy (12.4), roll-forward policy (12.5), no-blind-auto-generation (12.6) documented |
| Hypertables / TimescaleDB policies | ⏳ Task 6.2+ (Section 11) — none created in this task |
| New feature tables | ✅ Config schema (Task 6.5, migration `007`); Event storage (Task 6.6, migration `008`); Evidence (Task 6.7, migration `009`); Analytics (Task 6.8, migration `010`); AI domain (Task 6.9, migration `011`); Alert & approval (Task 6.10, migration `012`); Integrations (Task 6.11, migration `013`); Audit/outbox/inbox (Task 6.12, migration `014`) |

### 16.1 Known environmental note

`hotelops_app` is a cluster-wide role. `DROP ROLE` in the migration downgrade fails if any other database in the same cluster holds objects referencing it (the local dev `hotelops` database carries test-created RLS policies). This is expected PostgreSQL behavior, not a migration defect; documented for operators.

---

## References

- [ADR-002 — Deterministic Core / LLM-Last](adr/ADR-002-deterministic-core-llm-last.md)
- [ADR-003 — PostgreSQL/TimescaleDB as Source of Truth](adr/ADR-003-postgresql-source-of-truth.md)
- [ADR-004 — Redis as Transport, Not Source of Truth](adr/ADR-004-redis-transport.md)
- [ADR-005 — Shared Live/Recorded Pipeline](adr/ADR-005-shared-live-recorded-pipeline.md)
- [Architecture README](README.md)
- [Privacy Baseline](../security/privacy-baseline.md) — retention framework (TBD client-configurable)
- [Production Scope](../product/production-scope.md) — v1.0 boundaries, retention TBD
- [Acceptance Criteria](../product/acceptance-criteria.md) — 2.8: operational events stored correctly in TimescaleDB
- [Ownership](../operations/ownership.md) — schema owner and approval paths
- `contracts/common/ids.py`, `contracts/common/time.py`, `contracts/common/versioning.py`
- `backend/app/infrastructure/database/` — `base.py`, `client.py`, `models/identity.py`, `rls.py`, `repositories/identity.py`
- `database/migrations/versions/001_create_identity_tables.py`, `002_enable_rls.py`
