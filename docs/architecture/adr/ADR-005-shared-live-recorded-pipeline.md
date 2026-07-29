# ADR-005: Shared Live/Recorded Video Processing Pipeline

## Status

Accepted

## Context

HotelOps AI processes video from two distinct sources:

1. **Live CCTV streams** (RTSP) — real-time video from cameras
2. **Recorded video** — uploaded files or archived footage

The team must decide whether these sources use separate processing pipelines or converge into a shared pipeline.

The following forces influenced the decision:

- **Processing logic duplication**: CV, tracking, analytics, and rules logic would be identical for both paths
- **Maintenance burden**: Two pipelines means two codebases to maintain, test, and evolve
- **Feature parity risk**: One pipeline may lag behind the other in capabilities
- **Ingestion difference**: Live and recorded sources have different ingestion mechanisms (RTSP vs file upload)
- **Performance characteristics**: Live processing must keep up with real-time; recorded processing can be batch/accelerated

## Decision

Live CCTV and recorded video use **different ingestion mechanisms** but **converge into a shared canonical processing pipeline**.

```
Live RTSP ──────────┐
                     ├──> FramePacket ──> Shared CV Pipeline
Recorded Video ──────┘
```

- **Ingestion** differs: RTSP stream reader vs. file decoder/upload handler
- **Boundary contract**: Both produce the same `FramePacket` contract
- **Downstream processing**: Identical CV, tracking, spatial/temporal intelligence, rules engine
- **Session context**: The `VideoSession` carries metadata distinguishing live vs. recorded mode for downstream consumers

Do **not** create duplicate analytics architectures for live and recorded modes.

## Rationale

- **Single codebase**: One CV pipeline to maintain, test, and optimize
- **Consistent behavior**: Live and recorded frames receive identical processing
- **Test reuse**: Recorded video can be used to test the same pipeline that processes live frames
- **Performance flexibility**: Frame pacing can differ (real-time for live, batch for recorded) without duplicating pipeline logic
- **Contract clarity**: The FramePacket contract defines a clear architectural boundary

## Alternatives Considered

### Alternative 1: Completely Separate Pipelines

- **Description**: Independent live and recorded processing codebases
- **Pros**: Can optimize each pipeline independently, live pipeline can be simpler
- **Cons**: Massive code duplication, feature drift, increased testing burden, higher maintenance cost
- **Why not chosen**: The duplication cost far exceeds the minor optimization benefits

### Alternative 2: Single Ingestion Abstraction

- **Description**: Unified ingestion interface that abstracts RTSP and file reading
- **Pros**: Fully unified pipeline including ingestion
- **Cons**: Forces live streaming semantics on file processing and vice versa; abstraction leaks
- **Why not chosen**: Ingestion semantics differ significantly enough to justify separate implementations. The convergence point after ingestion is the correct boundary.

## Consequences

### Positive
- Single CV pipeline to maintain, test, and evolve
- Consistent processing behavior regardless of source
- Recorded video can serve as test data for the live pipeline
- Clear contract boundary at FramePacket

### Negative
- Live CV performance constraints apply to the shared pipeline design
- Recorded video processing must support the same contract as live (no skipping FramePacket)

### Neutral
- VideoAsset (recorded) can include additional metadata not available for live streams
- VideoSession carries source_type to allow downstream conditional logic if needed

## Security Impact

- Live ingestion handles RTSP credentials
- Recorded ingestion handles file validation and access control
- Once in the shared pipeline, both sources follow the same security model

## Operational Impact

- Different infrastructure for ingestion (RTSP connections vs. file storage)
- Shared pipeline components are deployed once, regardless of source
- Monitoring must distinguish live vs. recorded processing metrics

## Rollback / Migration

### Rollback Plan
- If shared pipeline becomes a bottleneck, specific stages can be forked for live vs. recorded without changing the FramePacket contract
- Ingestion is already separate — can be reverted independently

### Migration Plan
- Not applicable — this is the initial architecture decision

## References

- ADR-002 — Deterministic Core / LLM-Last
- ADR-003 — PostgreSQL as Source of Truth
- [contracts/video/models.py](../../../contracts/video/models.py) — FramePacket, VideoSession
- [Architecture README](../README.md) — Pipeline diagrams

---

## Metadata

| Field | Value |
|-------|-------|
| **Date** | 2026-07-29 |
| **Author(s)** | Engineering Team |
| **Last Modified** | 2026-07-29 |
