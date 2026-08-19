# TASK 19.1 — RTSP LIVE SESSION ARCHITECTURE AUDIT

**Status: PASS** — Dependencies (Tasks 11–18) are complete and provide a solid foundation.

---

## A. Existing Components

| Component | Location | Status | Notes |
|-----------|----------|--------|-------|
| **FrameSource (abstract contract)** | `backend/app/intelligence/sources/base.py` | ✅ Complete | Enforced lifecycle state machine (CREATED→RUNNING→DRAINING→CLOSED, terminal FAILED), async iteration, monotonic frame indexing, decode-error accounting, idempotent cancellation-safe resource release |
| **FrameData / DecodeStatus** | `backend/app/intelligence/sources/base.py` | ✅ Complete | In-process decoded payload (never serialized), companion to FramePacket |
| **FramePacket contract** | `contracts/video/models.py` | ✅ Complete | Canonical frame envelope: frame_id, session_id, frame_index, event_time (UTC), width/height, source_ref |
| **VideoSession contract** | `contracts/video/models.py` | ✅ Complete | session_id, source_type (LIVE/RECORDED), asset_id, started_at/ended_at, metadata |
| **VideoAsset contract** | `contracts/video/models.py` | ✅ Complete | Immutable reference to source video (live camera or recorded file) |
| **FileFrameSource** | `backend/app/intelligence/sources/file.py` | ✅ Complete | Recorded video ingestion via StoragePort + FrameDecoder |
| **RTSPFrameSource** | `backend/app/intelligence/sources/rtsp.py` | ✅ Complete | Live ingestion via RtspTransport protocol + shared FrameDecoder; bounded exponential backoff reconnect policy using Task 7 `compute_backoff_delay` |
| **RtspTransport protocol** | `backend/app/intelligence/sources/rtsp.py` | ✅ Complete | Provider-isolated boundary (connect→AsyncIterator[bytes], disconnect idempotent) |
| **ReconnectPolicy** | `backend/app/intelligence/sources/rtsp.py` | ✅ Complete | max_attempts, base_delay_seconds, max_delay_seconds, jitter |
| **FrameDecoder protocol** | `backend/app/intelligence/sources/decoder.py` | ✅ Complete | Isolates decoding SDK (PyAV/OpenCV/ffmpeg) |
| **BoundedFrameQueue** | `backend/app/intelligence/sources/queue.py` | ✅ Complete | Explicit capacity, mandatory full policy (BLOCK/DROP_OLDEST), observability stats, shutdown semantics |
| **FramePipeline** | `backend/app/intelligence/pipeline.py` | ✅ Complete | Source-agnostic pump: FrameSource → BoundedFrameQueue → FrameConsumer; guaranteed cleanup |
| **FrameConsumer protocol** | `backend/app/intelligence/pipeline.py` | ✅ Complete | Downstream CV boundary; consumes (FramePacket, FrameData) pairs |

---

## B. Existing Contracts

| Contract | Location | Notes |
|----------|----------|-------|
| `SourceType` enum | `contracts/video/models.py` | LIVE, RECORDED |
| `FramePacket` | `contracts/video/models.py` | Canonical frame envelope (no source discriminator) |
| `VideoSession` | `contracts/video/models.py` | Processing session with source_type |
| `VideoAsset` | `contracts/video/models.py` | Immutable source reference |
| `RtspTransport` protocol | `backend/app/intelligence/sources/rtsp.py` | Provider boundary for RTSP |
| `FrameDecoder` protocol | `backend/app/intelligence/sources/decoder.py` | Provider boundary for decoding |
| `FrameSource` abstract | `backend/app/intelligence/sources/base.py` | Ingestion boundary contract |

---

## C. Existing Database Models

| Model | Table | Key Fields |
|-------|-------|------------|
| `CameraModel` | `cameras` | camera_id, venue_id, tenant_id, name, status, protocol (rtsp/onvif), metadata |
| `VideoStreamModel` | `video_streams` | stream_id, camera_id, venue_id, tenant_id, name, status, source_url (RTSP URL) |
| `VideoAssetModel` | `video_assets` | asset_id, venue_id, tenant_id, name, source_type, camera_id (live), storage_uri (recorded), evidence_ref, capture_time, duration_seconds, media_metadata |
| `VideoSessionModel` | `video_sessions` | session_id, venue_id, tenant_id, source_type, camera_id (live), asset_id (recorded), configuration_version_id (pinned), status (active/ended/failed), started_at, ended_at, metadata |
| `CameraConfigModel` | `camera_configs` | config_id, camera_id, venue_id, tenant_id, status (draft/active/archived), version, analysis_enabled, frame_rate, width, height, detection_sensitivity, parameters (JSONB) |
| `MediaAssetModel` | `media_assets` | Centralized media metadata (recordings, evidence, etc.) with camera_id, session_id provenance |

All models enforce **tenant isolation** via composite FKs (entity_id, tenant_id) and direct tenant_id NOT NULL columns.

---

## D. Existing APIs

| Endpoint | Purpose | Auth |
|----------|---------|------|
| `POST /media/uploads/initiate` | Initiate upload (idempotent) | ActorContext + Idempotency-Key |
| `POST /media/uploads/{media_id}/complete` | Complete upload | ActorContext |
| `POST /media/uploads/{media_id}/multipart` | Multipart session | ActorContext |
| `POST /media/uploads/{media_id}/multipart/presign` | Presign part URLs | ActorContext |
| `POST /media/uploads/{media_id}/verify` | Verify checksum | ActorContext |
| `GET /media/{media_id}/download` | Signed download URL | ActorContext |
| `GET /media/{media_id}` | Media metadata | ActorContext |
| `DELETE /media/{media_id}` | Idempotent deletion | ActorContext |
| `GET /operational/events/{event_id}` | Occupancy event | ActorContext + ANALYTICS_READ |
| `GET /operational/facts/{fact_id}` | Occupancy fact | ActorContext + ANALYTICS_READ |
| `GET /operational/events/{event_id}/evidence` | Evidence availability | ActorContext + ANALYTICS_READ |

Authorization: Server-side ActorContext from JWT → tenant/venue scoping + PostgreSQL RLS (SET LOCAL app.tenant_id).

---

## E. Existing Worker Infrastructure

| Component | Location | Notes |
|-----------|----------|-------|
| `PollingWorker` base | `backend/app/workers/base.py` | Cooperative stop, poll interval, exception resilience |
| Outbox publisher | `backend/app/workers/outbox_publisher.py` | Transactional outbox with bounded backoff |
| Inbox consumer | `backend/app/workers/inbox_consumer.py` | Consumer-group with PEL reclaim |
| Media cleanup worker | `backend/app/workers/media_cleanup.py` | Retention/expiration enforcement |

**Note**: `/workers/live/`, `/workers/recorded/`, `/workers/analytics/` directories exist but are **empty** — no live-session worker exists yet.

---

## F. Existing Reusable Components

| Component | Reuse for Task 19 |
|-----------|-------------------|
| `compute_backoff_delay` | ✅ Used by `RTSPFrameSource._reconnect_or_fail` |
| `FrameSource` lifecycle | ✅ RTSPFrameSource already implements it |
| `BoundedFrameQueue` + `FramePipeline` | ✅ Shared by live/recorded (ADR-005) |
| `VideoSessionModel` (DB) | ✅ Persists live session state (active/ended/failed) |
| `CameraModel` + `VideoStreamModel` | ✅ Camera config + RTSP URL storage |
| `CameraConfigModel` | ✅ Per-camera analysis config (frame_rate, resolution, sensitivity) |
| `ConfigurationService.resolve_session_configuration` | ✅ Pins exact published version for session |
| `IdempotencyService` | ✅ For session start/stop operations |
| `record_pipeline_metric` | ✅ `PIPELINE_METRIC_FRAMES` already recorded at ingestion boundary |
| Health/readiness models | `backend/app/infrastructure/health/models.py` |
| Redis Streams transport | `backend/app/infrastructure/transport/redis_streams.py` |

---

## G. Missing Task 19 Components

| Missing Component | Required By Task 19 | Dependency |
|-------------------|---------------------|------------|
| **Concrete `RtspTransport` implementation** | Live ingestion | `aiortsp` / `PyAV` adapter behind the protocol |
| **Live session worker** (`LiveSessionWorker`) | Start/stop/health monitoring of live sessions | Extends `PollingWorker`; manages `RTSPFrameSource` + `FramePipeline` lifecycle |
| **Session lease/heartbeat mechanism** | Multi-instance coordination, crash detection | Redis key with TTL + background renewal; or DB lease column |
| **Session start/stop API** | `POST /live/sessions/{camera_id}/start`, `POST /live/sessions/{session_id}/stop` | New routes under `/live` prefix |
| **Session status API** | `GET /live/sessions/{session_id}` | Returns health, reconnects, frame rate, dropped frames |
| **Session list API** | `GET /live/sessions?camera_id=...` | Filter by camera, venue, status |
| **Per-session health metrics** | Prometheus: frames_processed, reconnects, latency, queue_depth | Extend `PIPELINE_METRIC_*` or add `LIVE_SESSION_*` metrics |
| **Camera → VideoSession orchestration** | Resolve CameraConfig, create VideoSessionModel, pin config version | Uses `ConfigurationService.resolve_session_configuration` |
| **Graceful degradation on config change** | Session continues on pinned version; new sessions pick up new config | Already handled by session pinning (Task 10.13) |

---

## H. Conflicting Implementations

**None found.** The architecture cleanly separates:
- Ingestion boundary (`FrameSource` + `RTSPFrameSource`) — **complete**
- CV pipeline (`FramePipeline` + `FrameConsumer`) — **complete, source-agnostic**
- Database models — **complete, tenant-isolated**
- Configuration — **complete, versioned, pinned per session**

No duplicate `VideoSession`, `FramePacket`, `FrameSource`, retry framework, worker framework, camera model, or health model exists.

---

## I. Required Modifications

| File | Modification | Reason |
|------|--------------|--------|
| `backend/app/intelligence/sources/rtsp.py` | Add concrete `RtspTransport` implementation (e.g., `PyAVRtspTransport`) | Currently only a protocol; needs SDK adapter |
| `backend/app/api/routes/live.py` | **New file**: Live session CRUD + health endpoints | Task 19 API surface |
| `backend/app/workers/live/session_worker.py` | **New file**: `LiveSessionWorker` extending `PollingWorker` | Manages live session lifecycle |
| `backend/app/workers/live/__init__.py` | **New file**: Exports | Package init |
| `backend/app/infrastructure/health/checks.py` | Add live session health check | `/ready` should verify Redis + DB + (optionally) active session connectivity |
| `backend/app/infrastructure/observability/metrics.py` | Add `LIVE_SESSION_*` metrics (optional) | Per-session observability |

---

## J. Required New Files

| File | Purpose |
|------|---------|
| `backend/app/intelligence/sources/pyav_transport.py` | Concrete `RtspTransport` using PyAV/aiortsp |
| `backend/app/api/routes/live.py` | Live session REST API (start, stop, status, list) |
| `backend/app/workers/live/session_worker.py` | `LiveSessionWorker` — orchestrates `RTSPFrameSource` + `FramePipeline` + consumer |
| `backend/app/workers/live/manager.py` | Session registry: tracks active sessions, handles lease/heartbeat, enforces max concurrent (SLO-011: 16) |
| `backend/app/application/services/live_session.py` | Application service: session create/end, DB persistence, config pinning |
| `tests/unit/test_live_session_worker.py` | Unit tests for worker lifecycle |
| `tests/integration/test_live_session_api.py` | API integration tests |

---

## K. Dependency Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **PyAV/aiortsp not available / broken** | Medium | Blocks live ingestion | Protocol isolates SDK; fake transport works for CI; vendor evaluation needed |
| **RTSP stream authentication variations** | High | Connection failures | `redact_rtsp_url` exists; transport must handle auth methods (Basic, Digest, query params) |
| **Network instability → reconnect storms** | Medium | Resource exhaustion | `ReconnectPolicy` bounds attempts; episode budget resets only on successful frame delivery |
| **16-stream limit (SLO-011) exceeded** | Low | OOM / CPU saturation | `LiveSessionManager` enforces max concurrent per instance |
| **Config version drift during live session** | None | N/A | Session pins exact published version (Task 10.13); immutable for session lifetime |
| **Downstream pipeline (Tasks 12–18) not source-agnostic** | None | N/A | Verified: `FramePipeline` typed against `FrameSource` protocol; `FramePacket` has no source_type; consumer protocol forbids branching |

---

## Verification: Downstream Tasks 12–18 Remain Source-Agnostic

| Task | Component | Source-Agnostic? | Evidence |
|------|-----------|------------------|----------|
| 12 | YOLO detection | ✅ | `detectors/yolo_adapter.py` consumes `FramePacket` + `FrameData` via `FrameConsumer` |
| 13 | ByteTrack tracking | ✅ | `tracking/bytetrack_adapter.py` uses `VideoSessionId` + `FramePacket` only |
| 14 | Spatial intelligence | ✅ | `spatial/engine.py` operates on detections/tracks, no source awareness |
| 15 | Temporal FSM | ✅ | `temporal/` consumes occupancy events, not frames |
| 16 | Deterministic rules | ✅ | `rules/` evaluates facts/events, not ingestion |
| 17 | Evidence | ✅ | `evidence/` links to `session_id`/`camera_id`, not ingestion path |
| 18 | Vertical slice | ✅ | `operational_read.py` reads events/facts; pipeline integration test proves shared boundary |

**Confirmed**: The canonical `FramePacket` carries **no live/recorded discriminator**. The `FramePipeline.run()` signature is `async def run(self, source: FrameSource)`. The `FrameConsumer.consume()` receives only `QueuedFrame(packet: FramePacket, data: FrameData)`. No concrete source class leaks into the CV layer.

---

## Conclusion

**Task 19.1 = PASS**

All foundational components (Tasks 11–18) are implemented and verified. The ingestion boundary (`FrameSource`/`RTSPFrameSource`), shared pipeline (`FramePipeline`), database models (`VideoSessionModel`, `CameraModel`), configuration pinning, observability (`PIPELINE_METRIC_FRAMES`), and authorization patterns are complete and production-ready.

**Task 19 must add only:**
1. Concrete `RtspTransport` implementation (SDK adapter)
2. Live session worker + manager (lifecycle, lease, max-concurrency)
3. Live session REST API (start/stop/status/list)
4. Per-session health metrics

No modifications to existing contracts, models, or downstream pipeline components are required.