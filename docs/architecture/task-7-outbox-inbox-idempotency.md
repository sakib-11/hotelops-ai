# Task 7 — Transactional Outbox, Inbox & Idempotency

**Status: Implemented & tested (see the acceptance matrix below).**

Task 7 completes the reliability spine of HotelOps AI: durable,
at-least-once event delivery from the transactional database to the
Redis transport (ADR-004), idempotent inbound processing, and
service-level idempotency keys — all reusing the Task 1–6 architecture:

- **Task 4** `EventEnvelope` remains the single event contract.
- **Task 5** `ActorContext` / tenant / venue authorization is enforced at
  every boundary.
- **Task 6** PostgreSQL + Alembic remains the source of truth (ADR-003);
  migration governance is preserved.

---

## 1. What was built

| Component | Files | Purpose |
|-----------|-------|---------|
| Migration `016_outbox_retry_idempotency` | `database/migrations/versions/016_outbox_retry_idempotency.py` | Retry scheduling (`available_at`), error preservation (`last_error`), venue context on outbox/inbox, `dead_letter` terminal state, `idempotency_records` table + grants |
| ORM models | `models/audit_outbox_inbox.py`, `models/idempotency.py` | Extend Task 6.12 models; new `IdempotencyRecordModel` |
| Reliability core | `infrastructure/reliability/backoff.py`, `exceptions.py` | Bounded exponential backoff + jitter; retryable/non-retryable taxonomy; idempotency conflict errors |
| Redis transport | `infrastructure/transport/redis_streams.py` | XADD / consumer-group / XAUTOCLAIM adapter over the existing `RedisClient` (no second Redis connection) |
| Repositories | `repositories/outbox.py`, `inbox.py`, `idempotency.py` | Enqueue/claim (SKIP LOCKED)/publish/retry/dead-letter; dedup receive/process; idempotency claim/complete/reclaim |
| Services | `application/services/outbox.py`, `inbox.py`, `idempotency.py` | Validated enqueue + audit (atomic), dedup ingress, idempotency keys |
| Workers | `workers/outbox_publisher.py`, `inbox_ingress.py`, `inbox_consumer.py` | Poller workers (run standalone: `python -m backend.app.workers.<name>`) |
| Config | `infrastructure/config.py`, `.env.example` | Typed worker/backoff/idempotency/stream settings |

## 2. Transaction boundaries (Phase 13)

One transaction owner per business operation — the caller's
`DatabaseClient.session`:

- **Enqueue (atomic)**: business state + domain event + audit row +
  outbox row commit together. Rollback removes all four. Nothing is
  published to Redis before the commit — the **outbox row is the
  durability boundary**.
- **Publish**: claim is a short transaction (`FOR UPDATE SKIP LOCKED`);
  the external Redis publish happens with NO database transaction open;
  the result transition (published / failed+backoff / dead_letter) is a
  short transaction guarded by `(status='processing' AND claimed_by=worker)`.
- **Consume**: effect + `processed` transition commit in ONE
  transaction (never marked processed before the effect is safely
  committed). A failing effect is rolled back via a savepoint, then the
  failed/dead-letter transition commits.

## 3. Outbox publisher (Phases 6–9)

```
PENDING ──claim──▶ PROCESSING ──publish──▶ PUBLISHED
                          │
                          └─fail─▶ failed (available_at = now + backoff(attempts))
                                   │
                                   └─attempts >= max / non-retryable ──▶ DEAD_LETTER
```

- **Leasing**: `claimed_by`/`claimed_until` + `available_at`. A crashed
  worker's row is re-claimable after lease expiry (reclaim advances
  `attempts`, so the retry budget is real).
- **Retry**: `base · 2^(attempt−1)` capped at `max`, ±jitter, persisted
  as `available_at` — durable across worker restarts.
- **Dead-letter**: terminal, never deleted; payload/tenant/venue/
  attempts/last_error remain inspectable.
- **At-least-once**: a crash between publish and mark re-publishes;
  downstream inbox dedup collapses duplicates to one effect.

## 4. Inbox / consumer dedup (Phase 10)

`(source, source_message_id)` unique key: Consumer A + Event X and
Consumer B + Event X are distinct rows; the same consumer receiving
Event X twice inserts once. The ingress bridge (Redis consumer group +
XAUTOCLAIM PEL recovery) relays stream messages into the inbox and ACKs
only after the insert commits.

## 5. Idempotency keys (Phase 11)

`(tenant_id, operation, idempotency_key)` is the idempotency unit;
`request_hash` is the canonical SHA-256 of the request payload.

- Same key + same payload → stored result replayed, operation NOT
  re-run.
- Same key + different payload → `IdempotencyConflictError` (HTTP 409
  semantics), operation NOT run.
- Simultaneous identical requests → exactly one executes
  (INSERT ON CONFLICT DO NOTHING); losers replay. Stale in-progress
  claims are reclaimable after lease expiry.
- Tenant isolation: lookups are always scoped by the ActorContext
  tenant. Venue isolation: a SPECIFIC_VENUES actor can only use venues
  in scope; a key bound to a different venue is a conflict.

## 6. Migration `016` summary

- `outbox_events` += `venue_id`, `available_at` (NOT NULL, server
  default now()), `last_error`
- `inbox_messages` += `venue_id`, `available_at`, `last_error`
- `outbox_status`/`inbox_status` += `dead_letter`; transition triggers
  extended (dead_letter terminal; all 014 transitions preserved)
- poller partial indexes rebuilt on `(available_at)
  WHERE status IN ('pending','failed')`
- new `idempotency_records` table + `idempotency_status` enum +
  grants (`hotelops_app`: SELECT/INSERT/UPDATE; no DELETE)
- Downgrade fully reverses everything except enum values (PostgreSQL
  cannot `DROP VALUE`; documented, and the restored 014 triggers refuse
  to enter dead_letter)

## 7. Observability (Phase 17)

Structured logs include `event_id`, `event_type`, `inbox_id`,
`attempts`, `tenant_id`/`venue_id` (scoping values, not secrets),
`status`, `retry_in_seconds`, `error_category`. Payloads, tokens and
credentials are never logged.

## 8. Running the workers

```bash
python -m backend.app.workers.outbox_publisher   # poll + publish to Redis stream
python -m backend.app.workers.inbox_ingress      # Redis stream -> inbox
python -m backend.app.workers.inbox_consumer     # inbox -> business effect
```

## 9. Tests

| Suite | Location | Focus |
|-------|----------|-------|
| unit | `tests/unit/test_reliability_backoff.py`, `test_idempotency_service.py`, `test_idempotency_model.py`, updated `test_audit_outbox_inbox_models.py`, `test_alembic_config.py` | Backoff bounds/jitter, canonical hashing, key validation, service decision loop incl. concurrency (fake repo), schema |
| contract | `tests/contract/test_task7_reliability.py` | EventEnvelope serialization/validation before outbox; no tenant smuggling |
| integration | `tests/integration/test_outbox_publisher.py`, `test_inbox_consumer.py`, `test_idempotency_records.py`, `test_reliability_flow_e2e.py` | Real TimescaleDB/Redis: publish, lease, crash recovery, retry, dead-letter, dedup, concurrency, isolation, full E2E flow |
| security | `tests/security/test_task7_isolation.py` | DB-free tenant/venue isolation attacks on the services |

Run: `pytest -m unit`, `pytest -m contract`, `pytest -m integration`
(`INTEGRATION_TESTS=1`), `pytest -m security`.

## 10. Task 7 acceptance matrix

| Requirement | Implemented | Tested | PASS/FAIL |
|-------------|-------------|--------|-----------|
| Transactional atomicity (state+event+audit+outbox) | ✅ | ✅ integration | PASS |
| Outbox durability (survives crash; Redis down is retryable) | ✅ | ✅ integration | PASS |
| Leasing (single active lease; expiry reclaim) | ✅ | ✅ integration | PASS |
| Retry (bounded attempts) | ✅ | ✅ integration | PASS |
| Backoff (exponential) | ✅ | ✅ unit + integration | PASS |
| Jitter (bounded, deterministic) | ✅ | ✅ unit | PASS |
| Dead-letter (terminal, preserved) | ✅ | ✅ integration | PASS |
| Inbox deduplication | ✅ | ✅ integration | PASS |
| Idempotency keys (replay) | ✅ | ✅ unit + integration | PASS |
| Payload conflict (409 semantics, no second op) | ✅ | ✅ unit + integration | PASS |
| Tenant isolation | ✅ | ✅ security + integration | PASS |
| Venue isolation | ✅ | ✅ security + integration | PASS |
| Contract validation (Task 4 EventEnvelope reused) | ✅ | ✅ contract | PASS |
| Migration (Alembic, single head, drift-free) | ✅ | ✅ db-gate + integration | PASS |
| Concurrency (publisher/consumer/idempotency) | ✅ | ✅ unit + integration | PASS |
| Crash recovery (publisher, consumer, ingress, idempotency) | ✅ | ✅ integration | PASS |
| Redis failure recovery (outbox retains, redelivery dedups) | ✅ | ✅ integration | PASS |

**Result: TASK 7 VERIFIED — READY FOR TASK 8.**
