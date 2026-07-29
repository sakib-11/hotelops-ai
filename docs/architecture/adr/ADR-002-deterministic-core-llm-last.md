# ADR-002: Deterministic Core / LLM-Last Architecture

## Status

Accepted

## Context

HotelOps AI processes video data to produce operational insights for hotel management. A key architectural question is where AI/LLM reasoning fits in the processing pipeline relative to deterministic computer vision, tracking, temporal calculations, rules, and KPI computations.

The team considered two fundamental approaches:

1. **LLM-first**: Pass raw or lightly processed video data to an LLM for interpretation
2. **Deterministic-core / LLM-last**: Process video through deterministic CV, tracking, analytics, and evidence generation *before* any LLM reasoning

The following forces influenced the decision:

- **Operational reliability**: Hotels cannot have unreliable operational data. Occupancy, dwell times, and zone events must be deterministic and auditable.
- **Evidence-based AI**: LLM reasoning should be grounded in concrete evidence, not raw video.
- **Cost**: LLM API calls are expensive; minimizing context size by sending bounded evidence reduces costs significantly.
- **Traceability**: Every recommendation must be traceable back to specific evidence.
- **Deterministic guarantees**: Core operational metrics must be reproducible and verifiable independent of LLM output.

## Decision

Use a **Deterministic Core / LLM-Last** architecture.

Computer vision, tracking, temporal calculations, deterministic rules, KPI calculations, and evidence generation all occur *before* any LLM reasoning.

```
Raw Video
    │
    ▼
Deterministic CV Pipeline (YOLO, ByteTrack)
    │
    ▼
Spatial & Temporal Intelligence (zones, dwell, occupancy)
    │
    ▼
Rules Engine (deterministic operational rules)
    │
    ▼
Evidence Package (bounded, structured evidence)
    │
    ▼
LLM Reasoning (consumes bounded evidence)
    │
    ▼
Recommendation (evidence-grounded, traceable)
```

LLMs consume bounded evidence packages. LLMs do **not** replace deterministic operational truth. The deterministic pipeline operates independently and produces authoritative metrics regardless of LLM availability or output quality.

## Rationale

- **Deterministic correctness**: Occupancy counts, dwell times, and zone entries are computed by deterministic algorithms that can be verified, tested, and audited independently.
- **Evidence grounding**: LLMs receive structured evidence packages, not raw video or ambiguous context, reducing hallucination risk.
- **Cost efficiency**: Evidence packages are small compared to raw video frames or streams, reducing LLM token usage by orders of magnitude.
- **Graceful degradation**: If the LLM is unavailable, the deterministic pipeline continues producing operational metrics and events.
- **Auditability**: Every recommendation traces back to specific findings and evidence, enabling human review.
- **Testability**: Deterministic components can be unit-tested without LLM dependencies.

## Alternatives Considered

### Alternative 1: LLM-First Architecture

- **Description**: Pass raw or lightly processed video frames to an LLM for interpretation
- **Pros**: Simpler initial pipeline, potentially faster to prototype
- **Cons**: High token costs, non-deterministic results, hallucinations in operational metrics, poor traceability, expensive at scale
- **Why not chosen**: Hotel operations require deterministic, auditable metrics. LLM-first would introduce unacceptable uncertainty and cost.

### Alternative 2: Hybrid with LLM Validation

- **Description**: Deterministic pipeline produces results, LLM validates them
- **Pros**: Some determinism preserved, LLM catches edge cases
- **Cons**: LLM validation introduces cost without clear benefit; deterministic results are already verifiable through testing; LLM might override correct deterministic results
- **Why not chosen**: Adds complexity and cost without meaningful improvement over pure deterministic pipeline with bounded evidence LLM reasoning.

## Consequences

### Positive
- Deterministic operational metrics (occupancy, dwell, zone events) are reliable and testable
- LLM costs bounded by evidence package size
- Graceful degradation when LLM is unavailable
- Clear audit trail from raw data to recommendations
- Components independently testable

### Negative
- More code to maintain (deterministic pipeline + LLM integration)
- Evidence package design is critical to LLM reasoning quality
- Requires careful contract design at the deterministic-to-AI boundary

### Neutral
- Future LLM improvements can be adopted without changing the deterministic pipeline
- New deterministic algorithms can be added without affecting LLM integration

## Security Impact

- Evidence packages contain only structured, bounded data — no raw PII from video frames
- LLM API calls can be logged and audited independently
- Deterministic pipeline continues operating even if LLM provider has an incident

## Operational Impact

- Monitoring must track both deterministic pipeline health and LLM availability
- Evidence package generation is a critical path operation
- LLM cost tracking per evidence package

## Rollback / Migration

### Rollback Plan
- If LLM-last proves insufficient, the deterministic pipeline remains independently valuable
- The deterministic pipeline can be operated without the LLM component
- Migration to a different LLM provider does not affect deterministic contracts

### Migration Plan
- Not applicable — this is the initial architectural decision

## References

- [Architecture README](../README.md) — Module boundaries
- ADR-004 — Shared Live/Recorded Pipeline
- [Product Charter](../../product/product-charter.md) — Architecture principles

---

## Metadata

| Field | Value |
|-------|-------|
| **Date** | 2026-07-29 |
| **Author(s)** | Engineering Team |
| **Last Modified** | 2026-07-29 |
