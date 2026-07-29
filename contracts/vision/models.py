"""Canonical CV observation contract models.

DetectionObservation — deterministic detector output.
TrackObservation    — tracking output associated with detections over time.

These are observations, not business conclusions.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    DetectionId,
    FrameId,
    TrackId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)


class BoundingBox(BaseModel, frozen=True):
    """A normalized bounding box with explicit coordinate semantics.

    All values are normalized to [0.0, 1.0] relative to image dimensions.
    x_min, y_min = top-left corner. x_max, y_max = bottom-right corner.
    """

    model_config = {"extra": "forbid"}

    x_min: float = Field(ge=0.0, le=1.0)
    y_min: float = Field(ge=0.0, le=1.0)
    x_max: float = Field(ge=0.0, le=1.0)
    y_max: float = Field(ge=0.0, le=1.0)

    @field_validator("x_max")
    @classmethod
    def _x_max_gte_x_min(cls, v: float, info: Any) -> float:
        if "x_min" in info.data and v < info.data["x_min"]:
            raise ValueError("x_max must be >= x_min")
        return v

    @field_validator("y_max")
    @classmethod
    def _y_max_gte_y_min(cls, v: float, info: Any) -> float:
        if "y_min" in info.data and v < info.data["y_min"]:
            raise ValueError("y_max must be >= y_min")
        return v


class DetectionObservation(BaseModel, frozen=True):
    """Deterministic detector output from a single frame.

    Produced by detectors (e.g., YOLO). Represents what was detected,
    not what it means.
    """

    model_config = {"extra": "forbid"}

    detection_id: DetectionId
    frame_id: FrameId
    schema_version: str = Field(default=SCHEMA_VERSION)
    class_name: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    bounding_box: BoundingBox
    event_time: datetime
    detector_metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)


class TrackState(StrEnum):
    """The lifecycle state of a tracked object."""

    ACTIVE = "active"
    LOST = "lost"
    TERMINATED = "terminated"


class TrackObservation(BaseModel, frozen=True):
    """Tracker output linking detections across frames.

    Produced by trackers (e.g., ByteTrack). Represents persistent object
    identity across a video session.
    """

    model_config = {"extra": "forbid"}

    track_id: TrackId
    detection_id: DetectionId
    frame_id: FrameId
    session_id: VideoSessionId
    schema_version: str = Field(default=SCHEMA_VERSION)
    event_time: datetime
    track_state: TrackState = TrackState.ACTIVE
    tracking_metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
