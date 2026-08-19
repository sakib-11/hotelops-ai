"""Canonical video/source contract models.

Conceptual pipeline:

    LIVE CCTV ──────┐
                     ├──> VideoSession ──> FramePacket ──> Shared CV Pipeline
    RECORDED VIDEO ──┘

VideoAsset represents an immutable/logical reference to a source video.
VideoSession represents a processing session over a live or recorded source.
FramePacket represents a frame crossing the video-processing boundary.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    EvidenceId,
    FrameId,
    VideoAssetId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)


class SourceType(StrEnum):
    """Whether the video source is live CCTV or recorded video."""

    LIVE = "live"
    RECORDED = "recorded"


class LiveVideoSessionStatus(StrEnum):
    """Operational health state of a live video session.

    These states track the ingestion pipeline health (connection, frames flowing,
    reconnection), NOT the video event semantics. Event-time (capture_time, PTS)
    is carried by FramePacket; this FSM uses system/processing time for
    heartbeat, staleness detection, and operational lifecycle.
    """

    CONNECTING = "connecting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


class VideoAsset(BaseModel, frozen=True):
    """An immutable/logical reference to a source video asset.

    Represents either a live camera stream or a recorded video file.
    Does NOT contain video bytes — only metadata and storage references.
    """

    model_config = {"extra": "forbid"}

    asset_id: VideoAssetId
    schema_version: str = Field(default=SCHEMA_VERSION)
    source_type: SourceType
    evidence_ref: EvidenceId | None = None
    capture_time: datetime | None = None
    duration_seconds: float | None = None
    media_metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_capture = field_validator("capture_time")(validate_utc)

    @field_validator("duration_seconds")
    @classmethod
    def _validate_duration(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("duration_seconds must be non-negative")
        return v


class VideoSession(BaseModel, frozen=True):
    """A processing session over a live or recorded video source.

    Provides downstream processing with session identity, source
    relationship, live-vs-recorded mode, and event-time context.
    """

    model_config = {"extra": "forbid"}

    session_id: VideoSessionId
    schema_version: str = Field(default=SCHEMA_VERSION)
    source_type: SourceType
    asset_id: VideoAssetId | None = None
    started_at: datetime
    ended_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_started = field_validator("started_at")(validate_utc)
    _validate_ended = field_validator("ended_at")(validate_utc)


class FramePacket(BaseModel, frozen=True):
    """A frame crossing the video-processing boundary.

    Carries enough metadata for deterministic downstream processing.
    Does NOT contain raw image payloads — those are handled via
    in-process references or object-storage evidence refs.
    """

    model_config = {"extra": "forbid"}

    frame_id: FrameId
    session_id: VideoSessionId
    schema_version: str = Field(default=SCHEMA_VERSION)
    frame_index: int = Field(ge=0)
    event_time: datetime
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_ref: VideoAssetId | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
