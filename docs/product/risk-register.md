# Risk Register

## Risk Scoring

| Score | Probability | Impact |
|-------|-------------|--------|
| 1 | Very Low (<10%) | Negligible |
| 2 | Low (10-25%) | Minor |
| 3 | Medium (25-50%) | Moderate |
| 4 | High (50-75%) | Significant |
| 5 | Very High (>75%) | Critical |

**Risk Score = Probability × Impact** (1-25)

| Risk Level | Score Range | Response |
|------------|-------------|----------|
| Low | 1-4 | Accept / Monitor |
| Medium | 5-10 | Mitigate |
| High | 11-16 | Mitigate aggressively |
| Critical | 17-25 | Must mitigate before release |

---

## Technical Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R01 | YOLO detection accuracy insufficient for production use cases | 3 | 4 | 12 | Systematic accuracy testing with labeled hotel operations data; fallback to configurable confidence thresholds; adapter pattern allows model replacement without core changes | CV Engineer | Open |
| R02 | ByteTrack tracking fails under crowded or occlusion scenarios | 3 | 4 | 12 | Testing with representative hotel video; configurable tracking parameters; adapter pattern supports tracker replacement | CV Engineer | Open |
| R03 | RTSP stream reliability issues cause frequent disconnections | 3 | 3 | 9 | Automatic reconnection with exponential backoff; stream health monitoring; alerting on stream loss | Backend Engineer | Open |
| R04 | Real-time processing cannot keep up with target camera count | 3 | 3 | 9 | Hardware sizing guidelines; configurable frame-rate and resolution; processing metrics to detect overload before failure | Backend Engineer | Open |
| R05 | Object detection accuracy degrades in low-light or night-time conditions | 3 | 3 | 9 | Test with night-time footage; camera-appropriate model selection guidance | CV Engineer | Open |

---

## CV Accuracy Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R06 | False positives cause incorrect occupancy measurements | 3 | 3 | 9 | Spatial-temporal filtering; configurable confidence thresholds; human review of evidence packages before action | CV Engineer | Open |
| R07 | False negatives miss operational events | 3 | 3 | 9 | Redundant detection paths where justified; sensitivity configuration; alerting on unexpected low detection rates | CV Engineer | Open |
| R08 | Object misclassification in edge cases | 2 | 3 | 6 | N-verification across frames; configurable per-class thresholds | CV Engineer | Open |
| R09 | Tracking ID switches under occlusion produce incorrect dwell times | 3 | 3 | 9 | Temporal consistency checks; dwell time as statistical distribution rather than single measurement | CV Engineer | Open |

---

## Privacy & Security Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R10 | Unauthorized access to live or recorded CCTV feeds | 2 | 5 | 10 | Authentication and authorization on all API endpoints; TLS encryption in transit; encryption at rest for recordings; desktop-backend separation preventing direct database access | Security Lead | Open |
| R11 | CCTV data retention exceeds policy or regulatory requirements | 2 | 4 | 8 | Automated data retention enforcement; audit logging of data deletion; retention configuration reviewed during setup | Security Lead | Open |
| R12 | Evidence packages contain privacy-sensitive information | 3 | 4 | 12 | Privacy review process for evidence package design; configurable redaction capabilities; clear privacy guidelines for evidence use | Security Lead | Open |
| R13 | Insider threat — authorized user misusing CCTV access | 2 | 4 | 8 | Access logging; audit trails for all data access; least-privilege access control; separation of duties where feasible | Security Lead | Open |
| R14 | Data breach of recorded video storage | 2 | 5 | 10 | Encryption at rest; access controls on storage; secure key management; regular security audits | Security Lead | Open |

---

## Integration Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R15 | POS/PMS integration requires unexpected data model changes | 3 | 3 | 9 | Adapter pattern isolates integration; early API discovery with target systems; clear integration contracts | Backend Engineer | Open |
| R16 | Camera compatibility issues with RTSP implementation | 3 | 3 | 9 | Camera compatibility testing matrix; adapter pattern supports protocol variations | Backend Engineer | Open |
| R17 | Third-party API rate limits or breaking changes affect integrations | 3 | 3 | 9 | Caching where appropriate; adapter pattern isolates external dependencies; monitoring for integration health | Backend Engineer | Open |

---

## Infrastructure Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R18 | GPU resource contention affecting real-time processing | 3 | 4 | 12 | Resource allocation planning; processing prioritization; monitoring for GPU saturation | DevOps Engineer | Open |
| R19 | Storage growth exceeds projections | 3 | 3 | 9 | Storage growth monitoring; configurable retention policies; alerting on storage thresholds | DevOps Engineer | Open |
| R20 | Network bandwidth insufficient for multiple camera streams | 2 | 4 | 8 | Bandwidth assessment during deployment; configurable stream quality; local processing where bandwidth is constrained | DevOps Engineer | Open |
| R21 | PostgreSQL/TimescaleDB performance degradation with time-series data volume | 3 | 3 | 9 | Database performance monitoring; partitioning strategy for time-series data; query optimization; archival strategy for old data | Backend Engineer | Open |

---

## AI Hallucination & Reasoning Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R22 | LLM produces recommendations not supported by deterministic evidence | 3 | 4 | 12 | Evidence-first architecture; LLM reasoning is bounded by available evidence; verification step before recommendation output | AI Engineer | Open |
| R23 | LangGraph workflow produces unexpected behavior in edge cases | 2 | 3 | 6 | Comprehensive workflow testing; deterministic fallback paths; human-in-the-loop for high-impact recommendations | AI Engineer | Open |
| R24 | ModelGateway routing errors cause incorrect model selection | 2 | 3 | 6 | Gateway health monitoring; fallback model configuration; clear error reporting | AI Engineer | Open |

---

## Data Quality Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R25 | Poor camera positioning or quality affects analysis quality | 3 | 3 | 9 | Camera placement guidelines; quality assessment during setup; documentation of limitations | CV Engineer | Open |
| R26 | Labeled training/evaluation data does not represent real operational conditions | 3 | 3 | 9 | Data collection from target environment; iterative model evaluation with production data | CV Engineer | Open |
| R27 | Historical data inconsistencies when integrating with existing systems | 3 | 2 | 6 | Data validation on integration; clear data quality documentation | Backend Engineer | Open |

---

## Operational Risks

| # | Risk | Probability | Impact | Score | Mitigation | Owner | Status |
|---|------|-------------|--------|-------|------------|-------|--------|
| R28 | On-call team lacks sufficient context to diagnose production issues | 3 | 3 | 9 | Comprehensive runbook; observable system design; post-incident reviews; regular on-call training | DevOps Engineer | Open |
| R29 | Deployment process is error-prone or undocumented | 2 | 4 | 8 | Automated deployment pipeline; documented deployment procedure; staging environment for validation | DevOps Engineer | Open |
| R30 | Dependency vulnerability discovered after deployment | 3 | 3 | 9 | Automated dependency scanning; vulnerability alerting; documented patching procedure | DevOps Engineer | Open |

---

## Risk Response Plan

- **High and Critical risks** (score ≥ 11): Must have mitigation plan before v1.0 release. Tracked in regular engineering reviews.
- **Medium risks** (score 5-10): Mitigation planned. Monitored for changes in probability or impact.
- **Low risks** (score 1-4): Accepted. Reviewed periodically.

## Risk Review Cadence

- Risk register is reviewed and updated monthly during the development phase
- New risks are added as they are identified
- Risk status updates are tracked in the register
- Post-incident risks are reviewed and added if missing
