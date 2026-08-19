# Architecture Documentation

## Module Boundaries

### Application Layers

```
Desktop (Tauri + React + TypeScript)
   │
   │  REST / WebSocket
   ▼
Backend API (FastAPI)
   │
   ▼
Application Layer
   │
   ▼
Domain Layer (business logic)
   ^
   │
Infrastructure Layer (PostgreSQL, Redis, S3, adapters)
```

**Rules:**
- Desktop communicates with Backend API only — never directly with databases or infrastructure
- Domain depends on abstractions (ports), not concrete infrastructure (adapters)
- Infrastructure implements ports defined by Domain
- Application orchestrates flows between Domain and Infrastructure

---

### Future Video Intelligence Architecture

```
Live RTSP ──────────┐
                     │
Recorded Video ──────┤
                     │
                     ▼
                 FrameSource          ← Adapter interface
                     │
                     ▼
               Detector Adapter       ← YOLO adapter
                     │
                     ▼
               Tracker Adapter        ← ByteTrack adapter
                     │
                     ▼
                 Spatial Intelligence  ← Zone definitions, area calculations
                     │
                     ▼
                 Temporal Intelligence ← Duration tracking, sequence detection
                     │
                     ▼
                  Rules Engine         ← Deterministic operational rules
                     │
                     ▼
              Evidence / Events        → PostgreSQL / TimescaleDB
```

> **⚠️ Planned:** This architecture is a target. Components are not yet implemented.

---

### Future AI Reasoning Architecture

```
Deterministic Data (PostgreSQL, TimescaleDB)
        │
        ▼
   Evidence Package
        │
        ▼
   LangGraph Workflow       ← Bounded workflow, not free-form LLM
        │
        ▼
   ModelGateway             ← Routes to appropriate LLM provider
        │
        ▼
   Verification             ← Output verified against evidence
        │
        ▼
   Recommendation           ← Always linked to source evidence
```

**Principle:** Evidence → LLM Reasoning → Verification → Recommendation.
LLMs are never treated as authoritative sources of operational truth.

> **⚠️ Planned:** This architecture is a target. Components are not yet implemented.

---

## Directory Structure

| Path | Purpose | Status |
|------|---------|--------|
| `backend/` | Python FastAPI backend | Implemented (Tasks 1-7) |
| `backend/app/workers/` | Reliability workers: outbox publisher, inbox consumer, ingress bridge (Task 7) | Implemented |
| `desktop/` | Tauri + React + TypeScript desktop | Initialized |
| `video-intelligence/` | Deterministic video intelligence | Planned |
| `contracts/` | Cross-module schemas and contracts | Implemented (Task 4) |
| `database/` | Migrations and DB tooling | Implemented (Tasks 6-7) |
| `infrastructure/` | Docker, deployment, monitoring | Implemented (Task 3) |

## Architecture Decision Records

ADRs are stored in `docs/architecture/adr/`:

| ADR | Title | Status |
|-----|-------|--------|
| ADR-000 | Template | Active |
| ADR-001 | Desktop Application Stack: Tauri + React + TypeScript | Accepted |
| ADR-002 | Deterministic Core with LLM Last | Accepted |
| ADR-003 | PostgreSQL as the Source of Truth | Accepted |
| ADR-004 | Redis as Event Transport (not source of truth) | Accepted |
| ADR-005 | Shared Live + Recorded Pipeline | Accepted |
| ADR-006 | Object Storage & Media Lifecycle Architecture | Accepted |

## Reliability & Event Integration (Task 7)

The transactional outbox / inbox / idempotency layer connects the Task 3
infrastructure (PostgreSQL, Redis), the Task 4 EventEnvelope contract, the
Task 5 ActorContext/RBAC model, and the Task 6 Alembic migration governance:

```
API / service transaction (Task 5 ActorContext + Task 4 EventEnvelope)
    │  business state + audit + outbox row (one COMMIT, Task 6 schema)
    ▼
outbox_events ── OutboxPublisherWorker (lease → Redis stream, ADR-004)
    ▼
Redis stream ── InboxIngressBridge (consumer group, dedup insert + ack)
    ▼
inbox_messages ── InboxConsumerWorker (claim → effect + processed, atomic)
idempotency_records ── service-level replay / 409 conflict / concurrency
```

See [Task 7 — Outbox, Inbox & Idempotency](task-7-outbox-inbox-idempotency.md)
for the full design, transaction boundaries, retry policy, security
behavior, and acceptance matrix.

## Temporal Intelligence (Task 15)

Task 15 converts canonical spatial observations into deterministic
presence / dwell / occupancy / movement / waiting intelligence:

```
TrackObservation → SpatialObservation
    → MovementMeasurement (15.5.1)
    → MovementClassification (15.5.2, hysteresis + qualification)
    → Waiting (15.5.3, context + qualification)
    → TemporalFact (MovementMeasurement / ClassificationTransition / WaitingInterval)
```

The core is pure and deterministic (no DB / Redis / HTTP / LLM, no
current-time reads); event-time is authoritative and configuration is
pinned per session. See
[Task 15.5 — Movement & Waiting Temporal Intelligence](task-15-movement-waiting-temporal-intelligence.md)
for the full design, state machines, hysteresis/qualification semantics,
checkpoint discipline, and acceptance matrix.

## Related Documentation

- [Product Charter](../product/product-charter.md) — Product purpose and principles
- [Production Scope](../product/production-scope.md) — v1.0 boundaries
- [Integration Scope](../product/integration-scope.md) — External system integration
- [Privacy Baseline](../security/privacy-baseline.md) — Data privacy principles
- [Operations](../operations/) — Ownership, runbooks, release policy
- [Database Governance](database-governance.md) — Source-of-truth policy, schema domains, migration governance, TimescaleDB policy (Task 6.1)
- [Task 7 — Outbox, Inbox & Idempotency](task-7-outbox-inbox-idempotency.md) — Transactional outbox, inbox dedup, idempotency, and reliability workers (Task 7)
