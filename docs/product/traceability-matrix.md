# Traceability Matrix

> **Principle**: Every implementation backlog item must map to at least one
> approved: Requirement ID, SLO ID, Risk mitigation, or Architecture/ADR decision.
>
> Items without traceability must be flagged before implementation.

## Known Tasks (1–3)

| Backlog ID | Task | Requirement IDs | SLO IDs | ADR | Risk IDs | Acceptance Criteria | Status |
|-----------|------|----------------|---------|-----|----------|-------------------|--------|
| TASK-001 | Engineering Charter & Production Scope | PROD-001, PROD-002, PROD-003, PROD-004, PROD-005, PROD-006, GOV-001, GOV-002 | — | ADR-000 (template) | All R01–R30 (baseline) | Level 4: 4.5, 4.6, 4.8 | **COMPLETE** |
| TASK-002 | Monorepo & Developer Bootstrap | ENG-001, ENG-002, ENG-003, ENG-004, OPS-001 | — | ADR-001 (desktop stack) | R28, R29 | Level 1: 1.1–1.8 | **COMPLETE** |
| TASK-003 | Local Infrastructure & Service Health | INFRA-001, INFRA-002, INFRA-003, INFRA-004, OPS-002, SEC-003 | SLO-003 (availability), SLO-006 (recovery) | — | R03, R04, R18, R19, R20, R21 | Level 4: 4.1, 4.3 | **COMPLETE** |

## Requirement ID Categories

| Prefix | Category |
|--------|----------|
| PROD | Product requirements (features, modes, outcomes) |
| LIVE | Live CCTV-specific requirements |
| REC | Recorded analysis-specific requirements |
| DESK | Desktop application requirements |
| DATA | Data/storage requirements |
| INT | Integration requirements |
| AI | AI/evidence/reasoning requirements |
| SEC | Security/privacy requirements |
| OPS | Operational/infrastructure requirements |
| ENG | Engineering/development requirements |
| GOV | Governance/documentation requirements |
| INFRA | Infrastructure requirements |

## Requirement Definitions

| ID | Description | Source |
|----|------------|--------|
| PROD-001 | Live CCTV analysis via RTSP streams | product-charter.md — Mode 1 |
| PROD-002 | Recorded video upload and analysis | product-charter.md — Mode 2 |
| PROD-003 | Shared deterministic video-intelligence pipeline | product-charter.md — Shared Core Intelligence |
| PROD-004 | Operational event generation (occupancy, dwell, queue) | product-charter.md — Outcomes |
| PROD-005 | Desktop application (Tauri + React + TypeScript) | product-charter.md — Desktop; ADR-001 |
| PROD-006 | Evidence-based AI reasoning (not LLM-as-truth) | product-charter.md — AI Reasoning Architecture |
| LIVE-001 | Up to 16 simultaneous RTSP streams | production-scope.md — Release Boundaries |
| REC-001 | Video upload up to 24 hours duration | production-scope.md — Release Boundaries |
| DATA-001 | PostgreSQL for relational operational data | production-scope.md — Storage |
| DATA-002 | TimescaleDB for time-series data | production-scope.md — Storage |
| DATA-003 | S3-compatible storage for recordings/evidence | production-scope.md — Storage |
| DATA-004 | Redis Streams for event transport | production-scope.md — Storage |
| INT-001 | Camera adapter interface (RTSP) | integration-scope.md — Camera |
| INT-002 | POS integration adapter (interface defined) | integration-scope.md — POS |
| INT-003 | PMS/booking integration adapter (interface defined) | integration-scope.md — Bookings |
| INT-004 | Staffing integration adapter (interface defined) | integration-scope.md — Staffing |
| INT-005 | Storage adapter (S3-compatible) | integration-scope.md — Storage |
| SEC-001 | No facial recognition | privacy-baseline.md — Principle 3 |
| SEC-002 | Authentication and authorization for API access | privacy-baseline.md — Access Control |
| SEC-003 | Audit logging for all data access | privacy-baseline.md — Logging & Audit |
| SEC-004 | Data retention enforcement | privacy-baseline.md — Data Retention |
| SEC-005 | Least-privilege access control | privacy-baseline.md — Least Privilege |
| SEC-006 | Client data ownership and separation | privacy-baseline.md — Client Data Handling |
| OPS-001 | Explicit ownership for all components | ownership.md — all areas |
| OPS-002 | Observable failures with health metrics | release-severity-policy.md — Incident Response |
| ENG-001 | Automated testing (unit, integration, E2E) | product-charter.md — Engineering Standard |
| ENG-002 | Architecture decisions recorded as ADRs | product-charter.md — Architecture Principle 10 |
| ENG-003 | Monorepo structure and developer bootstrap | Task 2 |
| ENG-004 | Quality gates (lint, typecheck, test, format) | Task 2 — Makefile |
| GOV-001 | Product charter and production scope documented | Task 1 |
| GOV-002 | Risk register maintained and reviewed | Task 1 — risk-register.md |
| INFRA-001 | Docker Compose local infrastructure | Task 3 — compose.yaml |
| INFRA-002 | FastAPI application with lifecycle management | Task 3 — backend/app/main.py |
| INFRA-003 | Typed configuration via Pydantic Settings | Task 3 — backend/app/infrastructure/config.py |
| INFRA-004 | Health/readiness endpoints (/health, /ready) | Task 3 — backend/app/api/routes/health.py |

## Template for Future Tasks

| Backlog ID | Task | Requirement IDs | SLO IDs | ADR | Risk IDs | Acceptance Criteria | Status |
|-----------|------|----------------|---------|-----|----------|-------------------|--------|
| TASK-XXX | *Task description* | *Link to requirements* | *Link to SLOs* | *ADR reference* | *Risk IDs mitigated* | *Acceptance criteria level* | **PLANNED** |

> Rules:
> 1. Every task must map to at least one requirement ID
> 2. Every task must map to at least one acceptance criterion
> 3. SLO IDs must be referenced when the task affects availability, latency, or reliability
> 4. ADR references are required for architecture-changing tasks
> 5. If a requirement mapping cannot be established, flag it in the task description

## References

- [Requirement IDs Reference](./product-charter.md) — Product requirements
- [SLO Requirements](./slo-requirements.md) — SLO definitions
- [Risk Register](./risk-register.md) — Risk definitions
- [ADR Directory](../architecture/adr/) — Architecture decisions
