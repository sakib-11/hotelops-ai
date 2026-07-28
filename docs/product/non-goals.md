# Non-Goals — Initial Release (v1.0)

The following are explicitly **outside the scope** of the first production release. These items will be reconsidered for future releases based on client priorities.

## Functional Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| 1 | **Multi-property/multi-tenant management** | v1.0 targets single-property deployment. Multi-property support adds significant architectural complexity. |
| 2 | **POS system integration** | POS integration planning is in scope; implementation is deferred. See integration-scope.md. |
| 3 | **PMS/booking system integration** | Booking integration planning is in scope; implementation is deferred. See integration-scope.md. |
| 4 | **Staff scheduling integration** | Staffing integration planning is in scope; implementation is deferred. See integration-scope.md. |
| 5 | **n8n or external automation** | n8n may be approved for automation in future releases. Not included in v1.0. |
| 6 | **Mobile application** | Desktop-only for v1.0. Mobile is not planned for the initial release. |
| 7 | **Public API / third-party developer platform** | API is internal to the platform. No public developer API. |
| 8 | **Real-time alerting outside the desktop app** | Email, SMS, or push notifications are deferred. Alerts are in-app only. |
| 9 | **Custom report builder** | Predefined reports only. Custom report builder is deferred. |
| 10 | **License plate recognition (LPR)** | Not in scope. Vehicle detection is limited to general object detection. |
| 11 | **Facial recognition** | Explicitly excluded for privacy reasons. See privacy-baseline.md. |
| 12 | **Audio analysis** | Video-only. Audio streams are not processed. |
| 13 | **Thermal camera support** | Standard CCTV cameras only. |
| 14 | **PTZ camera control** | Read-only camera integration. No pan/tilt/zoom control. |
| 15 | **Video analytics export to third-party systems** | Evidence packages are exportable; raw analytics stream export is deferred. |
| 16 | **Machine learning model training or customization** | Pre-trained models only. No model training pipeline. |
| 17 | **White-label / re-branding** | Single-brand deployment. |

## Architectural Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| 18 | **Desktop directly accessing databases** | Desktop communicates only through backend APIs. See architecture principles. |
| 19 | **LLM as authoritative operational truth** | All operational facts come from the deterministic core. LLMs reason over evidence only. |
| 20 | **Real-time video transcoding** | Video is processed as ingested. No real-time transcoding pipeline. |
| 21 | **Edge deployment on camera hardware** | Backend runs on server infrastructure, not on edge devices. |
| 22 | **High-availability / active-active deployment** | Single-instance deployment with defined recovery targets. |
