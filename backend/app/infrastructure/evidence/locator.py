"""Recording locator port (Task 17.6).

Maps the RESOLVED source identity — tenant, venue, asset, session — to the
exact recording object (object key + container facts) that backs it. This
is the seam between the deterministic ``ResolvedSourceSegment`` (Task
17.4) and the object-storage recording: the extractor never queries the
database and never searches for "the latest recording"; the caller-scoped
locator resolves the exact object.

The DB-backed implementation (querying ``video_assets``/``media_assets``)
lives behind this port; callers MUST scope the lookup by the segment's
tenant/venue — the extractor passes the segment scope through verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from contracts.common import TenantId, VenueId, VideoAssetId, VideoSessionId

__all__ = ["RecordingLocator", "RecordingReference"]


@dataclass(frozen=True)
class RecordingReference:
    """The exact recording object backing a resolved source segment."""

    object_key: str
    media_format: str = "mp4"
    # Recording capture start — maps decoded PTS to event time
    # (capture_time + pts), mirroring the Task 11 recorded-source policy.
    capture_time: datetime | None = None
    # Byte-level coverage of the recording (may be narrower than the
    # requested interval → truncated/PARTIAL).
    byte_start: datetime | None = None
    byte_end: datetime | None = None


@runtime_checkable
class RecordingLocator(Protocol):
    """Resolve the recording object for a resolved source identity.

    Returns None when no recording exists (the caller's scope is
    authoritative — a missing recording is ``SOURCE_NOT_FOUND``, never a
    substitution).
    """

    async def locate(
        self,
        *,
        tenant_id: TenantId,
        venue_id: VenueId,
        asset_id: VideoAssetId,
        session_id: VideoSessionId | None = None,
    ) -> RecordingReference | None: ...
