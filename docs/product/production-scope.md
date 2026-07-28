# Production Scope

## First Production Release (v1.0)

### What is Included

#### Ingestion
- RTSP stream ingestion for live CCTV cameras
- File upload for recorded video
- Frame extraction and frame-source abstraction layer
- Configurable frame-rate and resolution handling

#### Video Intelligence Core
- YOLO-based object detection (through adapter pattern)
- ByteTrack-based object tracking (through adapter pattern)
- Spatial intelligence (zone definitions, area calculations)
- Temporal intelligence (duration tracking, sequence detection)
- Deterministic rule engine for operational event generation

#### Operational Events (Initial Set)
- Zone entry/exit events
- Occupancy count per zone
- Dwell time measurements
- Queue length measurements
- Wait time estimates

#### Storage
- PostgreSQL for relational operational data
- TimescaleDB for time-series operational measurements
- S3-compatible object storage for recordings, evidence, and reports
- Redis Streams for event transport (not authoritative storage)

#### API Layer
- FastAPI backend exposing REST endpoints
- Authentication and authorization for API access
- Event subscription via WebSocket for real-time updates

#### Desktop Application
- Tauri + React + TypeScript desktop application
- Dashboard view for live monitoring
- Analysis view for recorded video
- Configuration view for zones, cameras, and settings
- Evidence review interface

#### Integration Adapters (Planned — not all implemented in v1.0)
- Camera adapter interface (defined, RTSP implementation in progress)
- Storage adapter interface (S3-compatible implementation in progress)
- Other adapters defined but not implemented (see integration-scope.md)

#### Analytics (Initial Set)
- Real-time occupancy dashboard
- Historical occupancy trends
- Dwell time reporting
- Basic anomaly flagging
- Evidence package generation

### Release Boundaries

| Boundary | Scope |
|----------|-------|
| **Cameras** | Up to 16 simultaneous RTSP streams per instance |
| **Recording** | Video files up to 24 hours duration per upload |
| **Retention** | TBD — client-configurable (see privacy-baseline.md for retention framework) |
| **Users** | Single-tenant deployment for one hotel property |
| **Deployment** | On-premise or private cloud deployment |
| **Languages** | English UI and documentation (initial release) |

### What Requires Explicit Approval for Inclusion

Any feature outside the above scope requires:
1. Written product approval from the client
2. ADR documenting the scope change
3. Updated acceptance criteria
4. Security and privacy review
