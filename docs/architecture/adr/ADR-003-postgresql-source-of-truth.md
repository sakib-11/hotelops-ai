# ADR-003: PostgreSQL/TimescaleDB as Source of Truth

## Status

Accepted

## Context

HotelOps AI generates and processes various types of data including video session metadata, detection events, tracking observations, analytical metrics, evidence artifacts, and operational actions. A fundamental architectural question is where authoritative business/event state lives.

The team evaluated several storage approaches:

1. **PostgreSQL/TimescaleDB** as the durable source of truth
2. **Redis** as the primary store
3. **Object storage + metadata index**
4. **Event log / event sourcing**

The following forces influenced the decision:

- **Durability**: Operational data must survive restarts and failures
- **Queryability**: Hotel operators and analytics need ad-hoc queries across time ranges, zones, and event types
- **Time-series data**: Video events, occupancy, dwell times are inherently time-series
- **Transactional consistency**: Some operations (e.g., session state changes, approval workflows) require ACID guarantees
- **Relationship integrity**: Detections relate to frames, frames to sessions, sessions to assets, recommendations to findings to evidence
- **Single-team codebase**: Event sourcing would add significant complexity without clear benefit for the current team size

## Decision

Use **PostgreSQL with TimescaleDB** as the durable source of truth for all authoritative business and event state.

PostgreSQL stores:
- Entity state (sessions, assets, jobs, findings, recommendations)
- Relational data linking entities
- Configuration and reference data

TimescaleDB hypertables store:
- Time-series metrics (occupancy, dwell times, detection counts)
- Event streams with time-based queries
- Analytical rollups

Redis may be used **only** for transient event transport and caching. Redis is **not** authoritative business storage.

## Rationale

- **Mature and reliable**: PostgreSQL is battle-tested in production environments
- **TimescaleDB for time-series**: Native hypertables, continuous aggregates, and retention policies for time-series data
- **ACID compliance**: Transactional guarantees for critical operations
- **Relational model**: Natural fit for the entity relationships in the domain
- **JSON support**: PostgreSQL's JSON/JSONB handles semi-structured metadata well
- **Single-team simplicity**: Avoids event sourcing complexity for a single-team codebase
- **Excellent Python ecosystem**: SQLAlchemy, asyncpg provide robust async support

## Alternatives Considered

### Alternative 1: Redis as Primary Store

- **Description**: Use Redis for all operational state with persistence enabled
- **Pros**: Low latency, simple data model, pub/sub built-in
- **Cons**: Limited queryability, no relational model, data loss risk during failover, poor ad-hoc analytics support, separate indexing needed for time-series queries
- **Why not chosen**: Redis excels as a cache and transport layer, not as a queryable source of truth for complex operational data

### Alternative 2: Event Sourcing

- **Description**: Store all state changes as an append-only event log; rebuild current state by replaying events
- **Pros**: Complete audit trail, temporal queryability, event-driven architecture alignment
- **Cons**: Significant complexity, event schema evolution challenges, snapshot management overhead, overkill for single-team codebase
- **Why not chosen**: The operational benefits do not justify the architectural complexity for the current team size and project phase

### Alternative 3: Object Storage + Metadata Index

- **Description**: Store data as files in S3-compatible storage with a metadata index in PostgreSQL
- **Pros**: Good for large binary artifacts (video, images)
- **Cons**: Poor for small transactional data, high latency for frequent writes, no relational query capability
- **Why not chosen**: Object storage is appropriate for video evidence and large artifacts but not for the core transactional data

## Consequences

### Positive
- Robust ACID guarantees for critical operations
- Rich query capabilities for analytics and reporting
- TimescaleDB handles time-series workloads natively
- Mature tooling and ecosystem
- Clear separation: PostgreSQL for truth, Redis for transport

### Negative
- Schema migrations required for all state changes
- PostgreSQL can become a bottleneck if not properly indexed and scaled
- TimescaleDB adds operational knowledge requirements

### Neutral
- Migration to distributed PostgreSQL (e.g., CockroachDB) is possible if scale requires it
- Read replicas can offload analytical queries

## Security Impact

- PostgreSQL supports row-level security for multi-tenant data isolation
- Connection encryption via TLS
- Credentials managed through environment variables, never committed

## Operational Impact

- Regular backup strategy required
- Connection pooling needed for production
- Query performance monitoring required
- TimescaleDB compression policies for data retention

## Rollback / Migration

### Rollback Plan
- PostgreSQL can be dumped and restored; no vendor lock-in
- Migrating from TimescaleDB to plain PostgreSQL loses time-series optimizations but retains all data

### Migration Plan
- Not applicable — this is the initial storage architecture decision

## References

- ADR-001 — Desktop Application Stack
- [Infrastructure Docker Compose](../../../infrastructure/docker/compose.yaml)
- [Product Charter](../../product/product-charter.md) — Architecture Principle #4

---

## Metadata

| Field | Value |
|-------|-------|
| **Date** | 2026-07-29 |
| **Author(s)** | Engineering Team |
| **Last Modified** | 2026-07-29 |
