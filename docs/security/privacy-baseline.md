# Privacy Baseline

## Scope

This document defines privacy principles and requirements for the HotelOps AI platform, which processes video feeds from CCTV/IP cameras in hotel operational areas. This is a **privacy baseline** — it establishes minimum requirements. Additional requirements may apply based on jurisdictional regulations, client policies, or deployment-specific considerations.

**This document does not provide legal advice. Compliance with applicable privacy laws and regulations is the responsibility of the deploying organization.**

---

## Privacy Principles

### Principle 1: Data Minimization
- Only video data necessary for operational analysis is collected
- Frame extraction rate is configurable and set to the minimum required for the use case
- Video resolution is reduced to the minimum required for accurate detection when feasible
- Audio is never captured or processed

### Principle 2: Purpose Limitation
- CCTV video is processed only for defined operational intelligence purposes
- Video data is not used for surveillance, monitoring of specific individuals, or any purpose beyond operational analytics
- Any change in data use requires documented approval and privacy review

### Principle 3: No Facial Recognition
- Facial recognition is explicitly excluded from the platform
- Object detection operates at the person/object level, not the individual identity level
- The system does not identify, track by identity, or profile specific individuals

### Principle 4: Transparency
- Camera locations and coverage areas are disclosed to relevant stakeholders
- Signage indicating video analytics is displayed in monitored areas per applicable regulations
- Data processing activities are documented and available for review

### Principle 5: Security
- All video data is protected in transit and at rest
- Access to video data is authenticated, authorized, and audited
- Security controls are documented and regularly reviewed

---

## Access Control

### Authentication
- All API access requires authentication (JWT or equivalent)
- Desktop application users authenticate through the backend
- No anonymous or unauthenticated access to video data or analysis results

### Authorization
- Role-based access control (RBAC) with the following roles defined:
  - **Viewer**: Read-only access to dashboards and reports (no raw video access)
  - **Analyst**: Access to analysis tools and evidence packages
  - **Administrator**: Full configuration access including camera and zone management
  - **Security Auditor**: Read-only access to audit logs
- Each role has minimum necessary permissions (least privilege)
- Role assignments require documented approval

### Access Reviews
- User access is reviewed quarterly
- Access is revoked within 24 hours of role termination or change
- Access review records are maintained for audit purposes

---

## Data Retention

### Video Data
| Data Type | Default Retention | Notes |
|-----------|------------------|-------|
| Live stream (transient) | Not stored beyond processing buffer | Live processing buffers are cleared immediately after frame processing |
| Recorded video uploads | TBD — client-configurable | Maximum retention period configured at deployment |
| Extracted frames (processing) | Deleted after processing completion | Frames are not retained beyond the processing pipeline |
| Detection metadata (occupancy, events) | TBD — client-configurable | Configurable per deployment; default TBD |

### Evidence Packages
| Data Type | Default Retention | Notes |
|-----------|------------------|-------|
| Evidence package (report) | TBD — client-configurable | Evidence packages containing video frames subject to retention limits |
| Associated video segments | TBD — client-configurable | Deleted when evidence package retention expires |

### System Logs
| Data Type | Default Retention | Notes |
|-----------|------------------|-------|
| Audit logs | TBD — client-configurable | Minimum 90 days recommended |
| Application logs | TBD — client-configurable | |
| Access logs | TBD — client-configurable | Minimum 90 days recommended |

---

## Evidence Handling

### Evidence Package Contents
Evidence packages contain:
1. Operational event metadata (timestamps, zone, measurement values)
2. Relevant detection data (bounding box coordinates, track IDs, confidence scores)
3. Relevant video segments or frames supporting the evidence
4. Deterministic rule trace showing how evidence was derived

### Evidence Privacy Controls
- Evidence packages containing video frames are subject to same access controls as raw video
- Evidence packages are automatically deleted when associated retention expires
- Evidence package access is logged

---

## Logging & Audit

### Events Logged
- All authentication events (successful and failed)
- All data access events (who accessed what, when)
- All configuration changes (camera, zone, retention settings)
- All evidence package creation, access, and deletion
- All administrative actions
- All errors and system failures

### Audit Log Requirements
- Logs are immutable (append-only)
- Logs are timestamped with synchronized clocks
- Logs are retained for the defined retention period
- Log access is restricted to Security Auditor role and administrators
- Logs are included in regular backup procedures

---

## Least Privilege

### Data Access
- Users have access only to data required for their role
- Zone-level access control: analysts may be restricted to specific camera zones
- Time-limited access grants are used for temporary access needs

### System Access
- Service accounts have minimum required permissions
- Database access is restricted to backend services only (no desktop/client database access)
- Redis access is restricted to backend services only
- Storage access is role-restricted

---

## Client Data Handling

### Data Ownership
- All operational data belongs to the client
- The development team and hosting provider do not have access to production client data without explicit authorization
- Data extraction and transfer procedures require client approval

### Data Separation
- Each deployment (tenant) uses separate database instances or schemas
- Storage isolation between deployments
- No cross-tenant data access

### Data Deletion
- Client may request data deletion at any time
- Deletion procedures are documented and tested
- Deletion confirmation is provided to the client
- Backup data is deleted according to the same policy (with notification of backup retention limitations)

---

## Deployment-Specific Considerations

The following items require deployment-specific assessment:
1. Jurisdictional privacy regulations (e.g., GDPR, CCPA, local CCTV laws)
2. Hotel-specific privacy policies
3. Employee consent/notification requirements
4. Union/works council requirements (if applicable)
5. Data residency requirements
6. Specific retention requirements from regulatory bodies

These items are outside the scope of this baseline and must be addressed during deployment planning.
