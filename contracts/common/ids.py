"""Canonical identifier types for HotelOps AI contracts."""

from typing import NewType
from uuid import UUID, uuid4

# --- Task 4 IDs ---
EventId = NewType("EventId", UUID)
FrameId = NewType("FrameId", UUID)
DetectionId = NewType("DetectionId", UUID)
TrackId = NewType("TrackId", UUID)
EvidenceId = NewType("EvidenceId", UUID)
VideoAssetId = NewType("VideoAssetId", UUID)
VideoSessionId = NewType("VideoSessionId", UUID)
AnalysisJobId = NewType("AnalysisJobId", UUID)
OpportunityId = NewType("OpportunityId", UUID)
FindingId = NewType("FindingId", UUID)
RecommendationId = NewType("RecommendationId", UUID)
AlertId = NewType("AlertId", UUID)
ApprovalRequestId = NewType("ApprovalRequestId", UUID)
ActionCommandId = NewType("ActionCommandId", UUID)

# --- Task 5.2 Identity IDs ---
TenantId = NewType("TenantId", UUID)
VenueId = NewType("VenueId", UUID)
UserId = NewType("UserId", UUID)
RoleId = NewType("RoleId", UUID)
MembershipId = NewType("MembershipId", UUID)


def new_uuid() -> UUID:
    """Generate a new canonical UUID."""
    return uuid4()
