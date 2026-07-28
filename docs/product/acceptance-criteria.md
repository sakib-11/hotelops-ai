# Acceptance Criteria

## General Principles

- All acceptance criteria must be **objective and testable**
- Criteria are either **Pass** or **Fail** — no subjective grading
- Acceptance requires sign-off from designated approvers
- Changes to acceptance criteria after sign-off require a change request

## Acceptance Levels

### Level 1: Technical Acceptance (Internal)

| # | Criterion | Verification Method | Approver |
|---|-----------|-------------------|----------|
| 1.1 | All components have automated unit tests with ≥80% line coverage | CI pipeline coverage report | Lead Developer |
| 1.2 | All API endpoints have integration tests | CI pipeline test results | Lead Developer |
| 1.3 | All security-critical paths have security tests | Security review checklist | Security Lead |
| 1.4 | Zero critical or high-severity vulnerabilities in dependencies | Dependency scan report | Lead Developer |
| 1.5 | All architecture decisions have corresponding ADRs | ADR directory audit | Tech Lead |
| 1.6 | Documentation is complete and consistent | Documentation review | Tech Lead |
| 1.7 | CI pipeline passes all stages | CI pipeline status | Lead Developer |
| 1.8 | Code review completed with zero unresolved comments | Code review tool | Lead Developer |

### Level 2: Functional Acceptance (Internal)

| # | Criterion | Verification Method | Approver |
|---|-----------|-------------------|----------|
| 2.1 | Live RTSP stream is ingested and frames are processed | Integration test with test RTSP source | Tech Lead |
| 2.2 | Recorded video upload is processed correctly | Integration test with test video file | Tech Lead |
| 2.3 | YOLO detection produces expected bounding boxes for test scenarios | Integration test with labeled test data | Tech Lead |
| 2.4 | ByteTrack tracking maintains consistent track IDs across frames | Integration test with known tracking scenario | Tech Lead |
| 2.5 | Spatial intelligence correctly maps detections to defined zones | Integration test with zone definitions | Tech Lead |
| 2.6 | Temporal intelligence produces correct duration measurements | Integration test with timed scenarios | Tech Lead |
| 2.7 | Deterministic rules generate correct operational events | End-to-end test with known scenario | Tech Lead |
| 2.8 | Operational events are stored correctly in TimescaleDB | Data integrity check | Tech Lead |
| 2.9 | Evidence packages contain correct deterministic evidence | End-to-end test | Tech Lead |

### Level 3: Integration Acceptance (Internal)

| # | Criterion | Verification Method | Approver |
|---|-----------|-------------------|----------|
| 3.1 | Desktop application connects to backend via API | Integration test | Tech Lead |
| 3.2 | Desktop dashboard displays live operational data | Integration test with live stream | Tech Lead |
| 3.3 | Desktop analysis view processes and displays recorded video results | Integration test | Tech Lead |
| 3.4 | Storage adapters correctly write and read from configured storage | Integration test | Tech Lead |
| 3.5 | Redis Streams deliver events to authorized subscribers | Integration test | Tech Lead |

### Level 4: Production Readiness Acceptance (Client + Internal)

| # | Criterion | Verification Method | Approver |
|---|-----------|-------------------|----------|
| 4.1 | Deployment documented and reproducible | Deployment runbook verified | Tech Lead |
| 4.2 | Backup and restore procedures documented and tested | Recovery drill | Tech Lead + Client |
| 4.3 | Monitoring and alerting configured for all components | Monitoring dashboard review | Tech Lead + Client |
| 4.4 | On-call procedures documented | Runbook review | Tech Lead + Client |
| 4.5 | Security review completed | Security review report | Security Lead + Client |
| 4.6 | Privacy baseline review completed | Privacy review report | Security Lead + Client |
| 4.7 | Data retention and deletion procedures verified | Procedure walk-through | Tech Lead + Client |
| 4.8 | SLO framework defined (targets established or documented as TBD pending client confirmation) and measurement infrastructure in place | SLO documentation + monitoring review | Tech Lead + Client |

### Level 5: User Acceptance (Client)

| # | Criterion | Verification Method | Approver |
|---|-----------|-------------------|----------|
| 5.1 | Live monitoring dashboard displays correctly for target camera setup | Client demonstration | Client Representative |
| 5.2 | Recorded video upload and analysis produces correct results | Client demonstration | Client Representative |
| 5.3 | Occupancy measurements match manual count within acceptable tolerance | Side-by-side validation | Client Representative |
| 5.4 | Dwell time measurements match manual timing within acceptable tolerance | Side-by-side validation | Client Representative |
| 5.5 | Evidence packages contain expected information in accessible format | Client review | Client Representative |
| 5.6 | Desktop application runs on target hardware without issues | Client testing | Client Representative |
| 5.7 | Documentation is clear and usable for client team | Client review | Client Representative |

## Sign-Off Process

1. **Technical acceptance** is confirmed when Level 1 criteria pass
2. **Functional acceptance** is confirmed when Level 2 and 3 criteria pass
3. **Production readiness** is confirmed when Level 4 criteria pass
4. **User acceptance** is confirmed when Level 5 criteria are signed off by the Client Representative
5. General availability is declared only after all levels are signed off

## Change Management

- Any change to acceptance criteria after sign-off requires a documented change request
- Changes affecting scope, cost, or timeline require client approval
- All changes to acceptance criteria are tracked in the change log
