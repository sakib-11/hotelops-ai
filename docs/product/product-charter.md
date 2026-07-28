# Product Charter: HotelOps AI

## Product Purpose

HotelOps AI is a production-grade operational intelligence platform for the hospitality industry. It analyzes hotel operations through live and recorded video streams to produce deterministic operational events, measurements, and evidence — enabling data-driven decision-making without relying on LLMs as authoritative sources of operational truth.

## Operating Modes

### Mode 1: Live Analysis
- Real-time analysis of live CCTV/IP camera feeds via RTSP streams
- Continuous monitoring of operational areas (lobbies, check-in queues, dining areas, etc.)
- Near-real-time event generation with sub-minute latency targets
- Dashboard and alerting for live operational awareness

### Mode 2: Recorded Analysis
- Upload and analysis of recorded video files
- Multi-day and multi-week retrospective analysis
- Trend identification and pattern recognition over extended periods
- Evidence package generation for reporting and review

### Shared Core Intelligence
Both modes share an identical deterministic video-intelligence pipeline:

```
Video → FrameSource → YOLO detector adapter → ByteTrack tracker adapter
→ Spatial Intelligence → Temporal Intelligence → Deterministic Rules
→ Operational Events
```

## Outcomes

The platform will produce:

1. **Occupancy Measurements** — real-time and historical occupancy per zone
2. **Dwell/Waiting Time Measurements** — time spent in queues, at counters, in waiting areas
3. **Movement Pattern Analysis** — flow patterns, congestion points, path optimization insights
4. **Operational KPIs** — service efficiency metrics, throughput rates, utilization rates
5. **Anomaly Detection** — deviation from expected operational patterns
6. **Opportunity Identification** — areas for operational improvement
7. **Evidence Packages** — deterministic, auditable evidence for any claim or recommendation

## Architecture Principles

| # | Principle | Description |
|---|-----------|-------------|
| 1 | **Deterministic Core, LLM Last** | All operational facts derive from deterministic computer vision and rules. LLMs are used only for reasoning over evidence, never as the source of truth. |
| 2 | **Evidence Before Recommendation** | Every recommendation must be traceable to deterministic evidence. No recommendation is generated without supporting evidence. |
| 3 | **Shared Core Intelligence** | Live and recorded modes use the same video-intelligence pipeline. Mode-specific differences exist only at the ingestion layer. |
| 4 | **External Systems Behind Adapters** | Every external system (cameras, POS, PMS, etc.) is accessed through a defined adapter interface with clear contracts. |
| 5 | **Desktop-Backend Separation** | The Tauri desktop application communicates exclusively through backend APIs. The desktop never directly accesses PostgreSQL, TimescaleDB, or Redis. |
| 6 | **Security & Privacy by Design** | CCTV data is sensitive by nature. All systems are designed with privacy and security as foundational requirements. |
| 7 | **Observable Failures** | All system components expose health metrics, error states, and operational telemetry. Failures are detected, logged, and alertable. |
| 8 | **Explicit Ownership** | Every component and concern has a named owner. No undocumented production behavior. |
| 9 | **Automated Testing** | All components require automated tests at appropriate levels (unit, integration, end-to-end). |
| 10 | **Recorded Decisions** | All architecture decisions are recorded through Architecture Decision Records (ADRs). |

## AI Reasoning Architecture

LLMs are integrated through a controlled, bounded architecture:

```
Evidence → LangGraph bounded workflow → ModelGateway → specialist reasoning
→ verification → recommendation
```

- **Deterministic Core** produces **Evidence**
- **Evidence** feeds into **LangGraph bounded workflows**
- **ModelGateway** routes to appropriate LLM providers
- **Specialist reasoning** is applied only where LLM reasoning adds value
- **Verification** ensures outputs are consistent with evidence
- **Recommendation** is the final output, always linked to source evidence

## Engineering Standard

This is a production client project. All code and documentation must meet the following standards:

- **Production quality** — not a prototype or tutorial
- **Tested** — automated tests at unit, integration, and E2E levels
- **Documented** — ADRs for architecture decisions, README for each component
- **Observable** — metrics, logs, traces for all components
- **Secure** — security review for all changes affecting data handling
- **Reviewable** — all changes require code review before production deployment
