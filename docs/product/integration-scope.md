# Integration Scope

## Integration Maturity Levels

| Level | Definition |
|-------|------------|
| **Planned** | Interface defined, adapter contract specified. Not implemented. |
| **In Progress** | Adapter under development. |
| **Implemented** | Adapter complete and tested. |

---

## Camera Integration

| Aspect | Details |
|--------|---------|
| **Interface** | FrameSource adapter contract: frame extraction, stream health, reconnection |
| **Protocol** | RTSP (Real-Time Streaming Protocol) |
| **Status** | **In Progress** — RTSP FrameSource adapter under development |
| **Constraints** | Up to 16 simultaneous streams per instance; standard CCTV cameras only |
| **Future** | Additional camera protocols may be added via adapter pattern |

---

## POS Integration

| Aspect | Details |
|--------|---------|
| **Interface** | POS adapter contract: transaction events, menu/items data, revenue data |
| **Status** | **Planned** — Interface defined, implementation deferred |
| **Priority** | Post-v1.0 |
| **Notes** | Integration scope depends on specific POS system used by client. Adapter pattern allows system-specific implementations. |

---

## Booking / Reservation Integration (PMS)

| Aspect | Details |
|--------|---------|
| **Interface** | PMS adapter contract: reservations, check-in/check-out events, room status, guest counts |
| **Status** | **Planned** — Interface defined, implementation deferred |
| **Priority** | Post-v1.0 |
| **Notes** | Integration scope depends on specific PMS used by client. Operational event correlation with booking data is a key future outcome. |

---

## Staffing Integration

| Aspect | Details |
|--------|---------|
| **Interface** | Staffing adapter contract: schedules, role assignments, on-duty status |
| **Status** | **Planned** — Interface defined, implementation deferred |
| **Priority** | Post-v1.0 |
| **Notes** | Staffing correlation enables operational efficiency analysis (staffing vs. demand) |

---

## Storage Integration

| Aspect | Details |
|--------|---------|
| **Interface** | Object storage adapter contract: upload, download, delete, list, retention management |
| **Protocol** | S3-compatible API |
| **Status** | **In Progress** — S3 adapter under development |
| **Use** | Recorded video storage, evidence packages, reports, exported data |
| **Constraint** | Not authoritative storage — PostgreSQL/TimescaleDB is the system of record |

---

## External Automation (n8n)

| Aspect | Details |
|--------|---------|
| **Interface** | REST API webhooks for triggered workflows |
| **Status** | **Planned** — Not in v1.0 scope |
| **Constraint** | n8n may trigger approved external actions but must not be treated as authoritative for operational truth |
| **Approval** | Each automation workflow requires documented approval |

---

## Integration Architecture Principles

1. **Adapter Pattern**: Every external system is accessed through a defined adapter interface
2. **Contract-First**: Adapter contracts are defined before implementation
3. **Isolation**: Integration failures in one system do not cascade to other systems
4. **Observability**: All adapter calls are logged, monitored, and measurable
5. **Graceful Degradation**: System continues to function (with reduced capability) when external integrations are unavailable
6. **No Circular Dependencies**: Integration data flows are one-directional where possible

## Integration Testing

- Each adapter has integration tests against a test/stub version of the external system
- Integration health is monitored and alertable
- Breaking changes in external systems are detected through integration monitoring
