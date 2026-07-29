# ADR-004: Redis as Transport, Not Source of Truth

## Status

Accepted

## Context

HotelOps AI requires real-time event transport for frame data, detections, and operational events between processing components (video ingestion, CV pipeline, analytics, workers). The team evaluated Redis Streams and alternative messaging approaches.

The key architectural constraint is that **Redis is not authoritative business storage**. All durable state lives in PostgreSQL/TimescaleDB per ADR-003.

The following forces influenced the decision:

- **Real-time transport**: CV pipeline produces frame-level events at high frequency
- **Consumer groups**: Multiple workers need to process events reliably
- **At-least-once delivery**: Event loss is unacceptable for operational data
- **Durability boundary**: Redis may lose data on restart — that is acceptable for in-flight events but not for committed business state
- **Simplicity**: Minimal infrastructure overhead for a single-team codebase

## Decision

Use **Redis Streams** as the real-time event transport layer. Redis is explicitly **not** authoritative business storage.

- **Event transport only**: Redis carries transient in-flight events between components
- **Consumer groups**: Workers use Redis consumer groups for reliable at-least-once processing
- **Acknowledgment**: Processed events are acknowledged and trimmed
- **Dead-letter**: Unprocessable events are moved to a dead-letter stream for manual inspection
- **No business queries against Redis**: All authoritative queries go to PostgreSQL

The canonical event envelope (EventEnvelope) is used as the wire format. Redis-specific fields (stream name, message ID, consumer group) are NOT part of the canonical event contract.

## Rationale

- **Stream semantics**: Redis Streams provide append-log semantics ideal for event pipelines
- **Consumer groups**: Built-in consumer group support with automatic load balancing
- **Low latency**: Sub-millisecond pub/sub for real-time event distribution
- **Minimal infrastructure**: Redis is already required for caching; using it for transport avoids additional messaging infrastructure
- **Clear boundaries**: The Redis-as-transport contract prevents coupling to Redis internals

## Alternatives Considered

### Alternative 1: Apache Kafka

- **Description**: Distributed event streaming platform
- **Pros**: Higher durability, longer retention, stronger ordering guarantees, larger ecosystem
- **Cons**: Significant operational overhead, JVM dependency, overkill for single-team codebase
- **Why not chosen**: Operational complexity not justified for current scale. Redis Streams provide adequate semantics with zero additional infrastructure.

### Alternative 2: RabbitMQ

- **Description**: AMQP message broker
- **Pros**: Mature routing capabilities, delivery acknowledgments, dead-letter exchanges
- **Cons**: Additional infrastructure, no stream semantics (consumers compete, cannot replay), more complex routing than needed
- **Why not chosen**: Redis Streams provide better stream semantics with the same infrastructure already required for caching

### Alternative 3: NATS

- **Description**: Lightweight messaging system
- **Pros**: Simple, high performance, at-least-once delivery
- **Cons**: Additional infrastructure, smaller ecosystem, less familiar to the team
- **Why not chosen**: Redis Streams provide equivalent semantics without adding infrastructure

## Consequences

### Positive
- No additional messaging infrastructure beyond existing Redis deployment
- Redis Streams provide consumer groups, acknowledgments, and replay
- Clear architectural boundary between transport (Redis) and storage (PostgreSQL)
- Redis continues to serve its caching role

### Negative
- Redis Streams have limited retention compared to Kafka
- Stream data loss is possible if Redis restarts before consumers process messages
- Consumer group management requires careful monitoring

### Neutral
- If scale requires Kafka in the future, the EventEnvelope contract remains unchanged
- Redis cluster mode adds operational complexity if sharding is needed

## Security Impact

- Redis connections use TLS in production
- Redis ACLs restrict stream access to authorized consumers
- No sensitive business data in Redis — events carry references, not PII

## Operational Impact

- Stream length monitoring to prevent unbounded growth
- Consumer group lag monitoring for worker health
- Dead-letter stream review for failed events

## Rollback / Migration

### Rollback Plan
- Components can fall back to direct PostgreSQL writes if Redis is unavailable (graceful degradation)
- The EventEnvelope contract is Redis-agnostic

### Migration Plan
- If migrating to Kafka in the future, only the Redis producer/consumer adapters need replacement
- The EventEnvelope contract and processing logic remain unchanged

## References

- ADR-003 — PostgreSQL as Source of Truth
- [contracts/events/envelope.py](../../../contracts/events/envelope.py)
- [Product Charter](../../product/product-charter.md) — Architecture principles

---

## Metadata

| Field | Value |
|-------|-------|
| **Date** | 2026-07-29 |
| **Author(s)** | Engineering Team |
| **Last Modified** | 2026-07-29 |
