"""Canonical identifier types for HotelOps AI contracts."""

from typing import NewType
from uuid import UUID, uuid4

# --- Task 4 IDs ---
EventId = NewType("EventId", UUID)
FrameId = NewType("FrameId", UUID)
DetectionId = NewType("DetectionId", UUID)
TrackId = NewType("TrackId", UUID)
EvidenceId = NewType("EvidenceId", UUID)
MediaId = NewType("MediaId", UUID)
VideoAssetId = NewType("VideoAssetId", UUID)
VideoSessionId = NewType("VideoSessionId", UUID)
AnalysisJobId = NewType("AnalysisJobId", UUID)
OpportunityId = NewType("OpportunityId", UUID)
FindingId = NewType("FindingId", UUID)
RecommendationId = NewType("RecommendationId", UUID)
AlertId = NewType("AlertId", UUID)
ApprovalRequestId = NewType("ApprovalRequestId", UUID)
ActionCommandId = NewType("ActionCommandId", UUID)
IntegrationId = NewType("IntegrationId", UUID)
AuditEventId = NewType("AuditEventId", UUID)
OutboxMessageId = NewType("OutboxMessageId", UUID)
InboxMessageId = NewType("InboxMessageId", UUID)

# --- Task 5.2 Identity IDs ---
TenantId = NewType("TenantId", UUID)
VenueId = NewType("VenueId", UUID)
UserId = NewType("UserId", UUID)
RoleId = NewType("RoleId", UUID)
MembershipId = NewType("MembershipId", UUID)

# --- Configuration domain IDs ---
CameraId = NewType("CameraId", UUID)
ConfigurationId = NewType("ConfigurationId", UUID)
ConfigurationVersionId = NewType("ConfigurationVersionId", UUID)

# --- Task 16 deterministic rule registry IDs ---
# Rule ids are stable natural keys ("queue_candidate"), NOT UUIDs — they are
# the canonical operational-rule identity. Rule versions are explicit
# ("v1", "v2") so queue_candidate:v1 and queue_candidate:v2 are distinct.
RuleId = NewType("RuleId", str)
RuleVersion = NewType("RuleVersion", str)


def new_uuid() -> UUID:
    """Generate a new canonical UUID."""
    return uuid4()
