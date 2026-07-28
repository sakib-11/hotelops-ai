# Open Requirements Register

> This register tracks unresolved decisions and pending client inputs that
> must be resolved before downstream tasks can be completed or before
> production deployment.

## Unresolved Items

| ID | Question / Requirement | Why Required | Owner | Blocks | Status | Decision Due |
|----|----------------------|-------------|-------|--------|--------|-------------|
| OR-001 | Which hotel venue(s) will host the pilot? | Determines deployment environment, camera infrastructure, network requirements, and integration targets | Product Owner | Task 4+ deployment planning, pilot execution | **PENDING CLIENT INPUT** | TBD |
| OR-002 | What camera makes/models are in use at the pilot venue? | Determines RTSP compatibility, stream configuration, and adapter validation scope | CV Engineer | Camera adapter implementation, validation | **PENDING CLIENT INPUT** | TBD |
| OR-003 | What video formats/codecs are used for recorded footage? | Determines recording upload pipeline requirements and format support scope | Backend Engineer | Recording upload implementation | **PENDING CLIENT INPUT** | TBD |
| OR-004 | What is the acceptable live event detection latency? | Drives SLO-001 target, processing pipeline design, and hardware requirements | Product Owner | SLO-001 finalisation, performance testing | **PENDING CLIENT APPROVAL** | TBD |
| OR-005 | What is the acceptable recorded video processing speed? | Drives SLO-002 target batch processing architecture | Product Owner | SLO-002 finalisation | **PENDING CLIENT APPROVAL** | TBD |
| OR-006 | What is the required system availability (uptime)? | Drives SLO-003 target, deployment architecture, and HA requirements | Product Owner | SLO-003 finalisation, infrastructure planning | **PENDING CLIENT APPROVAL** | TBD |
| OR-007 | What is the acceptable data retention period for operational data? | Drives privacy-baseline retention configuration, storage sizing, and deletion policies | Security Lead + Product Owner | Privacy baseline finalisation, storage planning | **PENDING CLIENT APPROVAL** | TBD |
| OR-008 | Are POS integration credentials/access available for testing? | Required for POS adapter development and integration testing | Backend Engineer | POS integration (post-v1.0) | **PENDING CLIENT INPUT** | TBD |
| OR-009 | Are PMS/booking system credentials/access available for testing? | Required for PMS adapter development and integration testing | Backend Engineer | Booking integration (post-v1.0) | **PENDING CLIENT INPUT** | TBD |
| OR-010 | Who is the client-side acceptance owner for pilot sign-off? | Required for acceptance-criteria Level 5 sign-off and UAT planning | Product Owner | Acceptance sign-off | **PENDING CLIENT INPUT** | TBD |
| OR-011 | What are the target hardware specifications for deployment? | Required for infrastructure provisioning, GPU sizing, and performance baselines | DevOps Engineer | Deployment planning, infrastructure setup | **PENDING CLIENT INPUT** | TBD |
| OR-012 | What network bandwidth is available at the deployment site? | Required for camera streaming configuration and infrastructure sizing | DevOps Engineer | Network planning, camera count limits | **PENDING CLIENT INPUT** | TBD |

## Process

1. Items are added to this register as unanswered questions arise during development
2. Items are assigned an owner who is responsible for obtaining the answer
3. Items blocking active development tasks are prioritised for resolution
4. Resolved items are moved to the **CLOSED** section below with decision recorded

## Resolved Items

*(None to date)*

## References

- [SLO Requirements](./slo-requirements.md) — SLO targets awaiting client approval
- [Privacy Baseline](../security/privacy-baseline.md) — Retention periods awaiting client input
- [Integration Scope](./integration-scope.md) — Integration access pending client input
- [Risk Register](./risk-register.md) — Related operational and integration risks
