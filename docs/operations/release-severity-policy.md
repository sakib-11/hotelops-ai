# Release Severity Policy

## Purpose

This document defines severity levels for release blockers and production incidents, and the handling expectations for each level. Severity levels guide prioritization and response but do not constitute contractual SLAs or guaranteed response times.

## Severity Levels

### Severity 0: Critical (System Down / Data Loss)

**Definition**: The system is unavailable, core functionality is completely broken for all users, or data loss / data corruption has occurred.

**Examples**:
- Backend API is unresponsive
- Video ingestion fails for all cameras
- Operational events are not being generated or stored
- Data corruption or loss detected
- Security breach or unauthorized data access

**Handling Expectations**:
- Work on all other items stops
- Engineering team mobilizes immediately
- Client is notified of the issue and expected resolution timeline (estimated)
- Investigation begins within 1 hour of detection
- Fix is deployed as soon as validated (no scheduled release window)
- Post-mortem required within 5 business days

### Severity 1: High (Major Feature Broken)

**Definition**: A major feature is broken or degraded, significantly impacting operations. A workaround may exist but is impractical.

**Examples**:
- One or more camera streams are not processing
- Occupancy measurements are incorrect
- Desktop application crashes on startup
- Evidence packages cannot be generated
- API endpoint returning errors for core functionality

**Handling Expectations**:
- Engineering team investigates within 4 hours during business hours
- Fix is prioritized for the next available release window
- Workaround is documented and communicated to client if available
- Client is notified of status and expected fix timeline
- Post-mortem required if root cause indicates systemic issue

### Severity 2: Medium (Feature Degraded / Non-Critical Broken)

**Definition**: A non-critical feature is broken or degraded. A reasonable workaround exists, or the impact is limited to a subset of users or use cases.

**Examples**:
- Dashboard chart rendering issue
- Historical data query is slower than expected
- Minor UI issue in desktop application
- Non-critical API endpoint returning errors
- Export functionality has issues

**Handling Expectations**:
- Issue is triaged within 1 business day
- Fix is scheduled for the next regular release
- Workaround is documented if available
- No client notification required unless impact is visible

### Severity 3: Low (Cosmetic / Enhancement)

**Definition**: Cosmetic issues, minor improvements, or non-urgent enhancements. No operational impact.

**Examples**:
- Typographical error in UI
- Minor styling inconsistency
- Log message improvement
- Feature request for future release
- Documentation improvement

**Handling Expectations**:
- Issue is triaged within 5 business days
- Fix is scheduled according to regular backlog prioritization
- May be deferred to a future release
- No client notification required

---

## Release Blockers

### Definition
A release blocker is any issue that must be resolved before a release can proceed to the next environment (staging → production).

### Blocker Criteria
1. Any Severity 0 or Severity 1 issue in the release scope
2. Any open security vulnerability with identified exploit path in the release scope
3. Any acceptance criterion (Level 1-4) that is not met
4. Any incomplete ADR for architectural changes in the release
5. Outstanding code review comments on release changes

### Blocker Resolution
- Blocker must be resolved, rejected, or deferred by documented decision before release proceeds
- Deferral requires: documented rationale, risk assessment, and approval from Tech Lead and (if client-facing) Client Representative
- Blockers are tracked in the release checklist

---

## Incident Response

### Detection
- Automated monitoring alerts on defined health metrics
- User-reported issues through support channel
- Scheduled health check verification

### Triage
- Initial triage determines severity level
- Severity level may be adjusted as more information becomes available
- Triage includes: impact assessment, affected users, affected functionality, root cause hypothesis

### Communication
- **Internal**: Severity 0/1 incidents are communicated in the engineering team channel immediately
- **Client**: Severity 0 incidents trigger client notification; Severity 1 incidents may trigger client notification based on impact assessment
- **Updates**: Status updates are provided at regular intervals during incident response

### Resolution
- Fix is validated in staging environment before production deployment
- Severity 0 fixes may bypass staging validation if delay would cause greater harm (requiring post-deployment validation)
- Resolution is documented in the incident record

### Post-Mortem
- Required for: Severity 0 incidents, Severity 1 incidents with systemic root cause, security incidents
- Post-mortem includes: timeline, root cause, impact, resolution, preventive measures, action items
- Action items are tracked and assigned

---

## Policy Limitations

1. **This policy defines handling expectations, not contractual SLAs.** Specific response and resolution times are client-negotiated and documented in the service agreement.
2. **Response expectations assume normal business hours unless otherwise specified.** 24/7 coverage requires additional resourcing and contractual agreement.
3. **Severity classifications require human judgment.** Automated classification is a guide, not definitive.
4. **This policy is reviewed quarterly** and updated based on operational experience.
