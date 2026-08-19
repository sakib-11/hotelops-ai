"""Canonical spatial intelligence contracts (Task 14).

SpatialObservation — the deterministic result of interpreting a
canonical TrackObservation against an IMMUTABLE published venue
configuration (the configuration version pinned by the video session).

This is an observation, not a business conclusion. It answers:

  "Where, in the venue, was this tracked object at this instant,
   according to the configuration version in force for the session?"

Architecture (Task 14 Step 1 audit):
  VideoSession
      ↓ pins configuration_version_id (immutable published version)
  ConfigurationVersionModel
      ↓ cameras → CameraProfileModel (physical camera_id)
  TrackObservation (session_id)
      ↓
  SpatialObservation (camera_id + configuration_version_id + track)

Coordinate spaces (ADR-010, no new format):
  - Track/detection points are anchored to the camera frame in
    IMAGE_NORMALIZED space [0, 1] x [0, 1] — identical to the canonical
    BoundingBox convention. The spatial point is carried verbatim from
    that space; a VENUE_LOCAL point is produced only after the spatial
    engine applies its camera→venue transformation.
  - Venue zones/tables/entrances/queues are POLYGON in VENUE_LOCAL
    (entity geometry contract, Task 10.5). The engine owns the
    transformation between the two canonical spaces; this contract
    never invents a third format.

Point policy (Task 14 Step 5): every observation declares how its
spatial point was derived from the track bounding box — CENTROID or
FOOTPOINT. The producer must choose explicitly; this contract does not
default one silently.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    FrameId,
    TrackId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)
from contracts.geometry import CoordinateSpace

# Version of the spatial interpretation engine that produced the
# observation. Bumped when interpretation semantics change; observations
# carry the engine version that actually interpreted them.
#
# 0.1.0 — Step 3 zone membership + exclusion evaluation.
# 0.2.0 — Step 5 table mapping + zone/table ambiguity resolution (an
#         observation may now carry a table_profile_id and the AMBIGUOUS
#         state covers overlapping tables as well as zones).
SPATIAL_ENGINE_VERSION = "0.2.0"


class SpatialPointPolicy(StrEnum):
    """How the spatial point was derived from the track bounding box.

    CENTROID  — box center: ((x_min + x_max) / 2, (y_min + y_max) / 2).
    FOOTPOINT — bottom-center: ((x_min + x_max) / 2, y_max), the point
                where a standing person/object contacts the floor.

    Hotel spatial semantics (zone membership, table mapping, queue
    formation) usually depend on floor contact, so FOOTPOINT is the
    likely production default for person tracks — but the choice is
    declared per observation, never silently assumed.
    """

    CENTROID = "centroid"
    FOOTPOINT = "footpoint"


class SpatialPointModel(BaseModel, frozen=True):
    """The canonical spatial point evaluated against venue geometry.

    ``coordinate_space`` mirrors the ADR-010 spaces. IMAGE_NORMALIZED
    points are bounded to the unit square (same rule as the canonical
    geometry contract); VENUE_LOCAL points are unbounded metric
    positions produced by the camera→venue transformation.
    """

    model_config = {"extra": "forbid"}

    x: float
    y: float
    coordinate_space: CoordinateSpace = CoordinateSpace.IMAGE_NORMALIZED
    policy: SpatialPointPolicy

    @model_validator(mode="after")
    def _validate_normalized_bounds(self) -> SpatialPointModel:
        if self.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED and not (
            0.0 <= self.x <= 1.0 and 0.0 <= self.y <= 1.0
        ):
            raise ValueError("IMAGE_NORMALIZED spatial points must lie in [0, 1] x [0, 1]")
        return self


class SpatialStatus(StrEnum):
    """Deterministic spatial interpretation outcome.

    The distinction between these states is the same one the canonical
    pipeline enforces between "no detections" and "inference failure":

      OUTSIDE  — evaluated and inside NO zone (legitimate result).
      EXCLUDED / PRIVACY — evaluated and policy-intercepted.

    An engine failure must never be encoded as OUTSIDE/EXCLUDED — it
    raises a typed application error instead (Task 14 Step 9+).
    """

    INSIDE = "inside"  # inside exactly one operational zone
    OUTSIDE = "outside"  # inside no zone, not excluded, not privacy
    AMBIGUOUS = "ambiguous"  # multiple overlapping zones or tables (tie-break undefined)
    EXCLUDED = "excluded"  # inside an exclusion ROI (processing optimization)
    PRIVACY = "privacy"  # inside a privacy ROI (data protection, supreme)

    # Precedence (ADR-010 INV-GEO-07): PRIVACY > EXCLUSION > standard
    # zones/tables. ``status`` is a single value, so when a point is
    # inside a zone AND a policy ROI the engine reports PRIVACY/EXCLUDED,
    # never INSIDE/AMBIGUOUS. Ambiguity resolution (Task 14 Steps 3 and
    # 5): a point matching multiple zones OR multiple tables, with no
    # explicit priority resolving it, is AMBIGUOUS — an AMBIGUOUS
    # observation does not carry the overlapping zone/table identities
    # (``zone_profile_id``/``table_profile_id`` are None).


class CrossingState(StrEnum):
    """Deterministic line-crossing outcome for one movement segment.

    CROSSED      — the segment P(previous) → P(current) properly
                   intersects the configured line.
    NO_CROSSING  — evaluated; the segment does not properly intersect
                   the line, including the documented boundary cases
                   (an endpoint on the line, both endpoints on the same
                   side, collinear overlap, and endpoint touch).

    An engine failure must never be encoded as either value — it raises
    a typed application error instead (same rule as ``SpatialStatus``).
    """

    CROSSED = "crossed"
    NO_CROSSING = "no_crossing"


class CrossingDirection(StrEnum):
    """Movement direction across a crossed line (Task 14 Step 4 §6).

    FORWARD — the movement crosses the line from its LEFT side (positive
              signed side) to its RIGHT side, relative to the crossed
              edge's vertex order. REVERSE is right → left.
    UNKNOWN — direction is not determinable: the line declares no
              directional semantics (BIDIRECTIONAL), or no crossing
              occurred.

    FORWARD/REVERSE are geometric labels relative to the configured
    line's own orientation — never business labels (guest entry/exit,
    etc.). The engine never invents direction when the configuration
    does not declare it.
    """

    FORWARD = "forward"
    REVERSE = "reverse"
    UNKNOWN = "unknown"


class LineCrossingObservation(BaseModel, frozen=True):
    """Deterministic spatial transition of one track across a configured line.

    Produced by the Task 14 Step 4 transition engine for two consecutive
    observations of the SAME track (previous → current), evaluated
    against the line pinned by the session's immutable configuration
    version. This is a spatial transition observation, never a business
    conclusion — guest entry/exit, queue arrival, occupancy change, etc.
    belong to later tasks.

    Architecture (Task 14 Step 4):
      VideoSession → pins configuration_version_id
      ConfigurationVersionModel → entrances (line-profile entities)
      TrackObservation(N), TrackObservation(N+1) — same track
        ↓ point policy (Step 2 extract_point, consistent for both)
      LineCrossingObservation (camera + config version + line + points)

    Provenance, minimal by design: every field is either canonical
    provenance (session/track/camera/config version/frame ids/times) or
    a declared transition result (points, crossing state, direction,
    engine version). The line is referenced by its version-owned
    profile_id — the immutable configuration version scopes it, so no
    line geometry is duplicated here.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    session_id: VideoSessionId
    track_id: TrackId
    camera_id: CameraId
    configuration_version_id: ConfigurationVersionId
    line_profile_id: str = Field(..., min_length=1)
    previous_frame_id: FrameId
    current_frame_id: FrameId
    previous_event_time: datetime
    current_event_time: datetime
    previous_point: SpatialPointModel
    current_point: SpatialPointModel
    crossing_state: CrossingState
    direction: CrossingDirection = CrossingDirection.UNKNOWN
    engine_version: str = Field(default=SPATIAL_ENGINE_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_previous_time = field_validator("previous_event_time")(validate_utc)
    _validate_current_time = field_validator("current_event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_time_order(self) -> LineCrossingObservation:
        """The transition is previous → current: time must not regress.

        Mirrors the engine's ``TransitionOrderError`` at the contract
        boundary so a deserialized observation can never present an
        impossible ordering (same invariant ``SpatialPointModel``
        enforces for the unit-square bounds).
        """
        if self.previous_event_time > self.current_event_time:
            raise ValueError(
                "previous_event_time must not follow current_event_time "
                "(a transition is previous -> current)"
            )
        return self


class SpatialObservation(BaseModel, frozen=True):
    """Spatial interpretation of one track observation.

    Minimal by design (Task 14 Step 1): every field is either canonical
    provenance (session/track/frame/time/camera/config version) or a
    declared interpretation result (point, status, zone/table mapping,
    engine version). No speculative business fields.

    Table mapping (Task 14 Step 5): ``table_profile_id`` is the single
    version-owned table that deterministically matched the spatial
    point (``None`` when no table matches, when the point is
    EXCLUDED/PRIVACY, or when the result is AMBIGUOUS). The zone/table
    relationship is configuration-declared (``ZoneModel.contained_tables``)
    and preserved by the engine result, never assumed to be "table ==
    zone".
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    session_id: VideoSessionId
    track_id: TrackId
    frame_id: FrameId
    event_time: datetime
    camera_id: CameraId
    configuration_version_id: ConfigurationVersionId
    spatial_point: SpatialPointModel
    status: SpatialStatus
    # Version-owned profile ids within the pinned configuration version
    # (profile_id is unique across categories within a version).
    # zone_profile_id is None when no single zone applies (OUTSIDE/
    # AMBIGUOUS/EXCLUDED/PRIVACY). table_profile_id is None when no
    # single table applies (AMBIGUOUS/EXCLUDED/PRIVACY, or no table
    # matched) — an OUTSIDE observation may still carry a table identity
    # when the point is at a table with no zone relationship (Step 5).
    zone_profile_id: str | None = Field(default=None, min_length=1)
    table_profile_id: str | None = Field(default=None, min_length=1)
    engine_version: str = Field(default=SPATIAL_ENGINE_VERSION)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)


__all__ = [
    "SPATIAL_ENGINE_VERSION",
    "CrossingDirection",
    "CrossingState",
    "LineCrossingObservation",
    "SpatialObservation",
    "SpatialPointModel",
    "SpatialPointPolicy",
    "SpatialStatus",
]
