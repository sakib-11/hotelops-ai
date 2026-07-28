# Contributing to HotelOps AI

## Development Setup

1. **Clone the repository**
   ```bash
   git clone <repo-url>
   cd hotelops-ai
   ```

2. **Copy environment template**
   ```bash
   cp .env.example .env
   ```

3. **Bootstrap development environment**
   ```bash
   make bootstrap
   ```

4. **Verify everything works**
   ```bash
   make check
   ```

---

## Branch Naming

Use trunk-oriented development on `main`. Branch names follow:

```
<type>/HOT-<ticket-number>-<short-description>
```

Examples:
- `feature/HOT-012-frame-source`
- `fix/HOT-031-outbox-replay`
- `test/HOT-044-recorded-analysis`
- `docs/HOT-001-architecture`
- `refactor/HOT-056-tracker-adapter`
- `chore/HOT-078-ci-pipeline`

Do **not** create long-running branches (e.g., `frontend`, `backend`, `cv`, `ai`).

---

## Commit Convention

Use conventional commits:

```
<type>(<scope>): <description>
```

### Types

| Type | Usage |
|------|-------|
| `feat` | New feature |
| `fix` | Bug fix |
| `test` | Adding or updating tests |
| `docs` | Documentation changes |
| `refactor` | Code refactoring without feature change |
| `perf` | Performance improvement |
| `build` | Build system or dependency changes |
| `ci` | CI/CD changes |
| `chore` | Maintenance, tooling, configuration |
| `security` | Security-related changes |

### Examples

```
feat(video): add FrameSource contract and RTSP adapter
fix(events): deduplicate event processing
test(cv): add tracking regression fixture for occlusion scenarios
docs(architecture): record desktop application stack decision
refactor(tracking): isolate ByteTrack behind adapter interface
chore(repo): configure pre-commit hooks
security(auth): enforce rate limiting on login endpoint
```

---

## Pull Request Process

1. Create a short-lived branch from `main`
2. Make your changes following the engineering standards
3. Run `make check` locally — all checks must pass
4. Create a pull request against `main`
5. PR must be reviewed by at least one team member
6. PR must pass CI checks
7. Squash-merge to `main`

### PR Template

A PR template is available at `.github/pull_request_template.md`.

---

## Local Quality Gates

Before pushing, run:

```bash
make check
```

This executes:
1. Python formatting check (ruff format --check)
2. Python linting (ruff check)
3. TypeScript formatting check (prettier)
4. TypeScript linting (ESLint)
5. Python type checking (mypy)
6. TypeScript type checking (tsc)
7. Python tests (pytest)
8. Rust tests (cargo test) — if applicable

All checks must pass. No warnings in strict mode.

---

## Testing Policy

| Test Type | Location | Must Pass Before Merge? |
|-----------|----------|------------------------|
| Unit tests | `tests/unit/` | ✅ Yes |
| Contract tests | `tests/contract/` | ✅ Yes (if applicable) |
| Integration tests | `tests/integration/` | ✅ Yes (if applicable) |
| CV regression | `tests/cv-regression/` | When implemented |
| E2E tests | `tests/e2e/` | When implemented |
| Security tests | `tests/security/` | ✅ Yes (if applicable) |
| Performance tests | `tests/performance/` | When implemented |

New code should include tests at the appropriate level.

---

## ADR Process

Architecture decisions requiring an ADR:

- Technology or framework choices
- Architectural pattern changes
- Data model changes affecting multiple modules
- Security or privacy-relevant decisions
- Integration approach decisions

1. Copy `docs/architecture/adr/ADR-000-template.md`
2. Create `ADR-XXX-title.md` with next available number
3. Document context, decision, alternatives, consequences
4. Submit for review as part of the PR

---

## Secrets Policy

**Never commit:**

- `.env` files (real configuration)
- Passwords, API keys, tokens
- Database credentials
- CCTV/IP camera credentials
- Private keys or certificates
- Real client CCTV recordings or photographs
- Unapproved production or client data
- Any credential that grants system access

The `.env.example` file is the only approved environment template.

If a secret is accidentally committed:
1. Immediately revoke the credential
2. Notify the security lead
3. Rewrite git history to remove the secret

---

## Security Expectations

- All code is reviewed for security implications
- API endpoints require authentication
- Access control follows least privilege
- Security tests are added for security-relevant changes
- Dependencies are scanned for vulnerabilities
- Report security issues to the security lead directly

---

## Definition of Done

A task or PR is done when:

- [ ] Code compiles and quality checks pass (`make check`)
- [ ] Tests are written at appropriate levels
- [ ] New functionality is documented
- [ ] ADR is created if architecture decision was made
- [ ] No secrets or credentials are committed
- [ ] Security implications are reviewed
- [ ] Code is reviewed by at least one team member
- [ ] Branch is up to date with `main`
- [ ] CI pipeline passes

---

## Questions?

Refer to:
- `docs/operations/ownership.md` — Team ownership areas
- `docs/architecture/adr/` — Architecture decisions
- `README.md` — Project overview
