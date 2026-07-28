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
| `backend/` | Python FastAPI backend | Structure ready |
| `desktop/` | Tauri + React + TypeScript desktop | Initialized |
| `video-intelligence/` | Deterministic video intelligence | Planned |
| `workers/` | Background processing workers | Planned |
| `contracts/` | Cross-module schemas and contracts | Planned |
| `database/` | Migrations and DB tooling | Planned |
| `infrastructure/` | Docker, deployment, monitoring | Planned |

## Architecture Decision Records

ADRs are stored in `docs/architecture/adr/`:

| ADR | Title | Status |
|-----|-------|--------|
| ADR-000 | Template | Active |
| ADR-001 | Desktop Application Stack: Tauri + React + TypeScript | Accepted |

## Related Documentation

- [Product Charter](../product/product-charter.md) — Product purpose and principles
- [Production Scope](../product/production-scope.md) — v1.0 boundaries
- [Integration Scope](../product/integration-scope.md) — External system integration
- [Privacy Baseline](../security/privacy-baseline.md) — Data privacy principles
- [Operations](../operations/) — Ownership, runbooks, release policy
