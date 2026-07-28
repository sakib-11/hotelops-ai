# Ownership

## Team Structure

The HotelOps AI project is developed by a single engineering team. Ownership areas define primary responsibility and decision authority within the team, and identify decisions that require external approval.

---

## Ownership Areas

### Computer Vision & Video Intelligence

| Aspect | Owner | Description |
|--------|-------|-------------|
| Object detection pipeline | **CV Engineer** | YOLO adapter, model selection, confidence configuration, accuracy evaluation |
| Object tracking pipeline | **CV Engineer** | ByteTrack adapter, tracking parameters, occlusion handling |
| Frame source abstraction | **CV Engineer** | FrameSource interface, frame extraction, stream management |
| Spatial intelligence | **CV Engineer** | Zone definitions, area mapping, spatial calculations |
| Temporal intelligence | **CV Engineer** | Duration tracking, sequence detection, temporal logic |
| CV model evaluation | **CV Engineer** | Accuracy testing, performance benchmarks, model selection decisions |

**Decisions requiring approval**:
- Model architecture changes → Tech Lead approval
- CV accuracy targets → Product approval
- Privacy-impacting CV capabilities (e.g., identity tracking) → Security Lead + Client approval

### Backend Engineering

| Aspect | Owner | Description |
|--------|-------|-------------|
| FastAPI application | **Backend Engineer** | API design, endpoint implementation, middleware, authentication |
| Deterministic rule engine | **Backend Engineer** | Rule definition DSL, rule execution, event generation |
| Database schema & queries | **Backend Engineer** | PostgreSQL schema, TimescaleDB schema, query optimization, partitioning |
| Redis Streams integration | **Backend Engineer** | Event transport, consumer groups, stream management |
| Storage adapters | **Backend Engineer** | S3-compatible storage adapter, file management |
| Integration adapters | **Backend Engineer** | Camera adapter, future POS/PMS/staffing adapters |
| API documentation | **Backend Engineer** | OpenAPI specification, endpoint documentation |

**Decisions requiring approval**:
- Database technology changes → Tech Lead + ADR
- API breaking changes → Tech Lead approval + client notification
- Integration adapter priority → Product approval
- Data schema changes affecting existing data → Tech Lead + Product approval

### Desktop Application

| Aspect | Owner | Description |
|--------|-------|-------------|
| Tauri application | **Desktop Engineer** | Application shell, native integrations, window management |
| React UI | **Desktop Engineer** | Component library, page implementation, state management |
| Real-time dashboard | **Desktop Engineer** | Live data visualization, WebSocket integration, chart components |
| Analysis interface | **Desktop Engineer** | Recorded video upload, analysis workflow, results display |
| Configuration interface | **Desktop Engineer** | Camera setup, zone definition, system configuration |
| Evidence review interface | **Desktop Engineer** | Evidence package viewing, export, review workflow |

**Decisions requiring approval**:
- UI framework changes → Tech Lead approval
- Desktop application architecture changes (Tauri vs. alternative) → Tech Lead + ADR
- User-facing feature additions → Product approval

### AI & Reasoning

| Aspect | Owner | Description |
|--------|-------|-------------|
| LLM integration (LangGraph) | **AI Engineer** | LangGraph workflow design, bounded workflow implementation |
| ModelGateway | **AI Engineer** | LLM provider routing, fallback configuration, cost management |
| Evidence → LLM pipeline | **AI Engineer** | Evidence formatting, context window management, prompt design |
| Verification layer | **AI Engineer** | LLM output verification, consistency checking, confidence scoring |
| AI recommendation design | **AI Engineer** | Recommendation structure, evidence linking, explainability |

**Decisions requiring approval**:
- LLM provider changes → Tech Lead + Product approval
- LangGraph workflow changes affecting core pipeline → Tech Lead + ADR
- Adding new LLM use cases → Product + Security approval
- Cost impact of LLM usage → Product approval

### Security & Privacy

| Aspect | Owner | Description |
|--------|-------|-------------|
| Authentication & authorization | **Security Lead** | Auth strategy, RBAC design, token management |
| Data encryption | **Security Lead** | Encryption in transit, at rest, key management |
| Privacy compliance | **Security Lead** | Privacy baseline, data retention, access controls |
| Security review | **Security Lead** | Security review process, vulnerability assessment, penetration testing |
| Audit logging | **Security Lead** | Audit trail design, log retention, log access controls |

**Decisions requiring approval**:
- Any change affecting data handling → Security Lead approval
- Privacy baseline changes → Security Lead + Client approval
- Access control model changes → Security Lead + Tech Lead approval

### Operations & Infrastructure

| Aspect | Owner | Description |
|--------|-------|-------------|
| Deployment pipeline | **DevOps Engineer** | CI/CD pipeline, deployment automation, environment management |
| Infrastructure provisioning | **DevOps Engineer** | Server setup, GPU configuration, network configuration |
| Monitoring & alerting | **DevOps Engineer** | Health metrics, dashboards, alert configuration, on-call procedures |
| Backup & recovery | **DevOps Engineer** | Backup procedures, recovery testing, disaster recovery planning |
| Performance monitoring | **DevOps Engineer** | System metrics, performance baselines, capacity planning |

**Decisions requiring approval**:
- Infrastructure provider changes → Tech Lead + Product approval
- Deployment architecture changes → Tech Lead + ADR
- Monitoring/alerting SLAs → Product approval

### Product & Project

| Aspect | Owner | Description |
|--------|-------|-------------|
| Feature prioritization | **Product Owner** | Backlog management, priority decisions, release planning |
| Client communication | **Product Owner** | Client updates, requirement gathering, feedback management |
| Acceptance criteria | **Product Owner** | Criteria definition, sign-off coordination, change management |
| Release management | **Product Owner** | Release planning, release notes, version management |

**Decisions requiring approval**:
- Scope changes → Client approval
- Timeline changes → Client approval
- Budget changes → Client approval

---

## Escalation Path

For decisions requiring approval beyond the engineering team:

| Decision Type | Approval Required |
|---------------|-------------------|
| Architecture changes | Tech Lead → ADR |
| Scope changes | Product Owner → Client |
| Security/privacy changes | Security Lead → Client |
| Cost/budget changes | Product Owner → Client |
| Timeline changes | Product Owner → Client |

## Decision Authority Matrix

| Decision | CV Engineer | Backend Engineer | Desktop Engineer | AI Engineer | Security Lead | DevOps Engineer | Product Owner | Tech Lead | Client |
|----------|-------------|------------------|-----------------|-------------|---------------|----------------|---------------|-----------|--------|
| Model selection | **Decide** | Consult | - | Consult | - | Consult | - | Approve | - |
| API design | Consult | **Decide** | Consult | - | - | - | - | Approve | - |
| UI implementation | - | - | **Decide** | - | - | - | Consult | Approve | - |
| Security controls | - | - | - | - | **Decide** | - | - | Consult | - |
| Deployment pipeline | - | - | - | - | - | **Decide** | - | Approve | - |
| Feature scope | - | - | - | - | - | - | **Decide** | Consult | **Approve** |
| Architecture decision | Consult | Consult | Consult | Consult | Consult | Consult | - | **Decide** | - |
| Release go/no-go | - | - | - | - | - | - | **Decide** | Approve | Approve |
| Acceptance sign-off | - | - | - | - | - | - | **Decide** | - | **Approve** |

**Decide** = Primary decision maker
**Approve** = Veto power
**Consult** = Must be consulted before decision
**-** = Not involved
