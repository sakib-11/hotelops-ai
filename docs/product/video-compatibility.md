# Video Compatibility Baseline

> **Status**: PLANNED — No video input compatibility has been validated.
> All entries reflect planned/expected capabilities only.

## A. Live Camera / Input Compatibility

| Input Type | Protocol | Codec(s) | Max Resolution | Max FPS | Authentication | Transport | Status |
|------------|----------|----------|---------------|---------|----------------|-----------|--------|
| IP Camera (RTSP) | RTSP | H.264, H.265 (planned) | TBD | TBD | Digest, Basic (planned) | TCP, UDP (planned) | **PLANNED** |
| IP Camera (ONVIF) | ONVIF Profile S/G/T | H.264, H.265 (planned) | TBD | TBD | WS-UsernameToken (planned) | TCP (planned) | **PLANNED** — implementation dependent on adapter priority |

### Notes
- RTSP is the primary planned protocol for live ingestion (see integration-scope.md)
- Actual compatibility depends on camera make, model, and firmware version
- ONVIF support may provide device discovery and stream negotiation
- No camera models have been validated yet
- Maximum concurrent streams: up to 16 per instance (see production-scope.md)

## B. Recorded Video Compatibility

| Container | Codec(s) | Max Resolution | Max FPS | Max File Size | Max Duration | Multi-file/week | Status |
|-----------|----------|---------------|---------|---------------|-------------|-----------------|--------|
| MP4 (planned) | H.264 | TBD | TBD | TBD (upload) | 24 hours (per upload) | Planned | **PLANNED** |
| AVI (planned) | TBD | TBD | TBD | TBD | TBD | TBD | **PLANNED** |
| MKV (planned) | TBD | TBD | TBD | TBD | TBD | TBD | **PLANNED** |

### Notes
- Recorded video support requires validation with representative files from the target environment
- Maximum file size and duration limits will be determined during implementation
- Multi-day/week analysis support is planned (see product-charter.md — Mode 2)
- Frame extraction pipeline must handle variable frame rates and container formats

## Validation Status

| Item | Status | Target |
|------|--------|--------|
| Camera model compatibility matrix | **PENDING** | To be created when pilot venue is confirmed |
| H.264 decoding validation | **PENDING** | Task 5+ implementation phase |
| H.265 decoding validation | **PENDING** | Post-v1.0 |
| RTSP stream negotiation | **PENDING** | Camera adapter implementation |
| Recorded file format validation | **PENDING** | Recording upload implementation |
| Maximum camera concurrency test | **PENDING** | Performance testing phase |

## References

- [Integration Scope](./integration-scope.md) — Camera integration details
- [Production Scope](./production-scope.md) — Release boundaries and camera limits
- [Risk Register](./risk-register.md) — R16 (Camera compatibility)
