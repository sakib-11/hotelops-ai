# SLO Requirements

## Service Level Objectives

The following SLO targets are defined for the HotelOps AI platform. Targets marked **TBD** require client approval and are placeholders until confirmed.

### Availability

| ID | Component | Target | Measurement Period | Notes |
|----|-----------|--------|-------------------|-------|
| SLO-001 | Backend API (FastAPI) | **TBD** | Monthly | Client to confirm uptime requirements |
| SLO-002 | Desktop Application | **N/A** | N/A | Client-managed; no server-side availability SLO |
| SLO-003 | PostgreSQL Database | **TBD** | Monthly | Tied to backend availability |
| SLO-004 | Redis Streams | **TBD** | Monthly | Tied to backend availability |
| SLO-005 | Object Storage (S3-compatible) | **TBD** | Monthly | Dependent on underlying storage provider |

### Latency

| ID | Operation | Target | Measurement Method | Notes |
|----|-----------|--------|-------------------|-------|
| SLO-006 | Live event detection latency | **TBD** seconds | End-to-end from frame capture to event generation | Dependent on camera fps, resolution, and hardware |
| SLO-007 | Recorded video processing | **TBD** x real-time | Wall-clock time / video duration | e.g. 10× real-time = 1 hour video processed in 6 minutes |
| SLO-008 | API response time (p95) | **TBD** ms | Server-side measurement | Excluding video upload endpoints |
| SLO-009 | Dashboard data refresh | **TBD** seconds | From event generation to dashboard update | |
| SLO-010 | WebSocket event delivery | **TBD** ms | From event generation to client receipt | |

### Processing Throughput

| ID | Metric | Target | Notes |
|----|--------|--------|-------|
| SLO-011 | Simultaneous camera streams | Up to 16 per instance | Hardware-dependent; see deployment requirements |
| SLO-012 | Frame processing rate | **TBD** fps per camera | Dependent on resolution, hardware, and model |
| SLO-013 | Concurrent video uploads | **TBD** | |
| SLO-014 | Concurrent desktop clients | **TBD** | |

### Reliability

| ID | Metric | Target | Notes |
|----|--------|--------|-------|
| SLO-015 | Event delivery guarantee | At-least-once | Redis Streams with consumer acknowledgments |
| SLO-016 | Duplicate event tolerance | **TBD** | Client to confirm tolerance for duplicates |
| SLO-017 | Data durability (operational) | **TBD** | PostgreSQL/TimescaleDB backup frequency |
| SLO-018 | Data durability (recordings) | **TBD** | Object storage replication/backup policy |

### Recovery

| ID | Scenario | Target | Notes |
|----|----------|--------|-------|
| SLO-019 | Backend service failure | **TBD** minutes to restore | Recovery time objective |
| SLO-020 | Database failure | **TBD** minutes to restore | RTO |
| SLO-021 | Data loss tolerance | **TBD** | Recovery point objective — maximum acceptable data loss |
| SLO-022 | Camera stream interruption | **TBD** | Automatic reconnection behavior |

## Measurement & Reporting

- SLO compliance will be measured monthly
- SLO breaches will trigger a post-mortem and ADR if remediation requires architectural change
- SLO targets may be adjusted during the first 90 days of production operation as baseline data is collected

## Note

All TBD values require:
1. Client discussion to establish realistic targets based on operational needs
2. Validation against target hardware specifications
3. Documentation in an ADR once confirmed
