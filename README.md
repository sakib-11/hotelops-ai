# HotelOps AI

**Operational intelligence platform for hospitality.**

HotelOps AI analyzes hotel operations through live and recorded CCTV video streams to produce deterministic operational events, measurements, and evidence — enabling data-driven decision-making.

---

## Current Status

**Phase: Infrastructure Foundation Complete (Tasks 1–3)**

- **Task 1:** Product charter, architecture principles, production scope, risks, privacy baseline, governance docs — complete.
- **Task 2:** Monorepo with Tauri + React + TypeScript desktop, Python backend foundation, quality gates, Makefile, pre-commit, documentation — complete.
- **Task 3:** Local FastAPI backend with PostgreSQL/TimescaleDB, Redis, MinIO object storage, typed configuration, /health + /ready endpoints, dependency diagnostics, failure/recovery, Docker Compose — **verified and operational.**

> No business functionality implemented yet. See [docs/product/product-charter.md](docs/product/product-charter.md) for the full product charter.

---

## Planned Architecture

```
Desktop (Tauri + React + TypeScript)
        │
        ▼  REST / WebSocket
    Backend (Python + FastAPI)
        │
        ├── Live CCTV (RTSP)
        └── Recorded Video (Upload)
                │
                ▼
          FrameSource → Detector (YOLO) → Tracker (ByteTrack)
                → Spatial → Temporal → Rules → Events
                │
                ▼
        PostgreSQL + TimescaleDB
                │
                ▼
        Analytics → Evidence → LangGraph → ModelGateway
                → Verification → Recommendations
```

**Infrastructure:** Redis Streams (event transport), S3-compatible storage (recordings/evidence), n8n (external automation).

> **Note:** This is the **planned architecture**. Components are not yet implemented. See each module's documentation for current status.

---

## Repository Structure

```
hotelops-ai/
├── backend/             # Python FastAPI application (planned)
│   └── app/
├── desktop/             # Tauri + React + TypeScript desktop client
├── video-intelligence/  # Deterministic video intelligence core (planned)
├── workers/             # Background workers (planned)
├── contracts/           # Cross-module schemas/contracts (planned)
├── database/            # Database migrations (planned)
├── infrastructure/      # Docker, deployment, observability (planned)
├── tests/               # Cross-system automated testing
├── docs/                # Documentation
│   ├── product/         # Product charter, scope, risks
│   ├── architecture/    # ADRs and architecture docs
│   ├── api/             # API documentation (planned)
│   ├── testing/         # Testing strategy (planned)
│   ├── security/        # Privacy baseline
│   └── operations/      # Runbooks, ownership, release policy
├── n8n/                 # Version-controlled automation workflows (planned)
├── scripts/             # Development and maintenance scripts
└── .github/             # CI workflows and PR templates
```

---

## Technology Stack

| Layer | Technology | Status |
|-------|------------|--------|
| Desktop Shell | **Tauri** v2 | Initialized |
| Desktop UI | **React** 19 + **TypeScript** 5 | Initialized |
| Backend | **Python + FastAPI** | **Running with PostgreSQL, Redis, MinIO** |
| Computer Vision | YOLO + ByteTrack (adapter pattern) | Planned |
| Database | **PostgreSQL** + **TimescaleDB** | **Running (Docker Compose)** |
| Event Transport | **Redis Streams** | Planned (Redis connection verified) |
| Object Storage | **S3-compatible** (MinIO) | **Running (Docker Compose)** |
| AI Orchestration | LangGraph + ModelGateway | Planned |
| Automation | n8n (external, approved only) | Planned |

---

## Prerequisites

| Tool | Version | Required For |
|------|---------|-------------|
| Python | ≥ 3.14 | Backend, tools |
| Node.js | ≥ 20 | Desktop UI |
| npm | ≥ 10 | Desktop dependencies |
| Rust | ≥ 1.80 | Tauri native shell |
| Git | ≥ 2.40 | Version control |

---

## Developer Bootstrap

```bash
# Clone the repository
git clone <repo-url>
cd hotelops-ai

# Copy environment template (never commit .env)
cp .env.example .env

# Bootstrap development environment
make bootstrap

# Run all quality checks
make check
```

**`make bootstrap`** installs:
- Python virtual environment with ruff, mypy, pytest, pre-commit
- Desktop (Node.js) dependencies
- Pre-commit hooks

---

## Quality Commands

| Command | What it does |
|---------|-------------|
| `make format` | Format Python + TypeScript + Rust code |
| `make format-check` | Check formatting without changing files |
| `make lint` | Run Ruff, ESLint, Clippy |
| `make typecheck` | Run mypy, TypeScript compiler |
| `make test` | Run pytest, cargo test, desktop tests |
| `make check` | Run all quality gates (format-check + lint + typecheck + test) |
| `make bootstrap` | Install/setup all development tooling |

---

## Testing Structure

| Directory | Purpose | Status |
|-----------|---------|--------|
| `tests/unit/` | Fast isolated unit tests | Bootstrapped |
| `tests/contract/` | API/event/schema compatibility tests | Planned |
| `tests/integration/` | Database/Redis/storage integration tests | Planned |
| `tests/cv-regression/` | Video/cv regression tests | Planned |
| `tests/e2e/` | Full end-to-end workflow tests | Planned |
| `tests/security/` | Authorization/privacy/security tests | Planned |
| `tests/performance/` | Throughput/load/capacity tests | Planned |

---

## Documentation

| Location | What's documented |
|----------|-------------------|
| [docs/product/product-charter.md](docs/product/product-charter.md) | Product purpose, principles, outcomes |
| [docs/product/production-scope.md](docs/product/production-scope.md) | v1.0 scope and boundaries |
| [docs/product/non-goals.md](docs/product/non-goals.md) | Explicitly excluded features |
| [docs/product/slo-requirements.md](docs/product/slo-requirements.md) | Service level objectives |
| [docs/product/acceptance-criteria.md](docs/product/acceptance-criteria.md) | Acceptance requirements |
| [docs/product/risk-register.md](docs/product/risk-register.md) | Risk register |
| [docs/product/integration-scope.md](docs/product/integration-scope.md) | Integration boundaries |
| [docs/security/privacy-baseline.md](docs/security/privacy-baseline.md) | Privacy principles and controls |
| [docs/architecture/adr/](docs/architecture/adr/) | Architecture Decision Records |
| [docs/operations/](docs/operations/) | Operations and ownership docs |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |

---

## Architecture Decisions

Architecture decisions are recorded as ADRs in [docs/architecture/adr/](docs/architecture/adr/).

- **ADR-000**: Template for new ADRs
- **ADR-001**: Tauri + React + TypeScript desktop stack

---

## Branch Strategy

Trunk-oriented development on `main`. Use short-lived feature/fix/test/docs branches:

```
feature/HOT-012-description
fix/HOT-031-description
test/HOT-044-description
docs/HOT-001-description
```

---

## Commit Convention

```
<type>(<scope>): <description>

Examples:
  feat(video): add frame source contract
  fix(events): prevent duplicate processing
  test(cv): add tracking regression fixture
  docs(architecture): record desktop decision
  chore(repo): configure development tooling
```

Types: `feat`, `fix`, `test`, `docs`, `refactor`, `perf`, `build`, `ci`, `chore`, `security`

---

## License

Proprietary — HotelOps AI
