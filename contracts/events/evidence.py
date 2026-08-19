"""Canonical evidence reference contract (Task 17.2).

Represents a reference to evidence that may eventually point to a frame,
image, video clip, object-storage object, or analytical artifact.
Does NOT embed binary evidence.

The contract connects a material event to its source evidence along the
canonical chain:

    EVENT → SOURCE → SESSION → CAMERA → TIME / FRAME RANGE
                                          → PROCESSING PROVENANCE

Field policy (REQUIRED / OPTIONAL / NOT_APPLICABLE):

REQUIRED  — identity and event linkage: ``ref_id``, ``ref_type``,
            ``ref_uri``, ``event_id``, ``event_time``. An evidence ref
            without a material event is a request, not a ref: the
            canonical ``EventEnvelope`` always exists before evidence is
            linked (Task 16 constructs evidence requests from the event
            identity, never the other way around). ``ref_uri`` is the
            storage reference (Task 9 key/URI) — no second
            ``storage_reference`` field exists.
OPTIONAL  — everything else, validated when present. ``created_at`` is
            intentionally left to the fulfillment layer: a rule-emitted
            request must stay wall-clock free so replay is deterministic.
NOT_APPLICABLE — ``start_frame``/``end_frame`` for non-frame evidence;
            ``detector_version``/``tracker_version`` for artifacts that
            never passed through CV; ``camera_id`` for analytical
            artifacts; etc. Optional fields are simply absent.

Validation rejects:

- missing event linkage (``event_id`` / ``event_time``);
- missing source provenance for media-backed types
  (FRAME / IMAGE / VIDEO_CLIP require at least one of
  ``video_asset_id`` / ``video_session_id`` / ``camera_id``);
- ``end_time < start_time`` and inverted frame ranges;
- non-UTC timestamps;
- malformed SHA-256 ``checksum`` values;
- tenant/venue scope violations (a venue is tenant-scoped, so
  ``venue_id`` without ``tenant_id`` is invalid);
- rule provenance without its configuration version (``rule_id``
  requires ``rule_version`` + ``configuration_version_id`` — never a
  silent "latest configuration" lookup);
- unsupported version formats (rule versions follow the project
  ``v1``/``v2`` convention; detector/tracker versions are numeric
  dotted versions such as ``8.1.0``).

Note: ``source_id`` (Task 17.2) maps to ``video_asset_id`` — the
canonical project name for a video source (``VideoAsset``). No duplicate
field is introduced.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    EventId,
    EvidenceId,
    RuleId,
    RuleVersion,
    TenantId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)


class EvidenceType(StrEnum):
    """The kind of artifact an evidence reference points to."""

    FRAME = "frame"
    IMAGE = "image"
    VIDEO_CLIP = "video_clip"
    OBJECT_STORAGE = "object_storage"
    ANALYTICAL_ARTIFACT = "analytical_artifact"


# Media-backed evidence types must carry source provenance (a frame or
# clip is meaningless without knowing which source/session/camera it
# came from). Analytical artifacts and raw object-storage refs may be
# sourced purely by their ref_uri.
_MEDIA_EVIDENCE_TYPES = frozenset({EvidenceType.FRAME, EvidenceType.IMAGE, EvidenceType.VIDEO_CLIP})

# Rule versions follow the project convention: "v1", "v2", "v1.2.3"
# (see contracts/rules validate_rule_version). Detector/tracker versions
# are numeric dotted versions such as "8.1.0" (DETECTION_MODEL_VERSION).
_RULE_VERSION_PATTERN = re.compile(r"^v\d+(\.\d+)*$")
_COMPONENT_VERSION_PATTERN = re.compile(r"^\d+(\.\d+)*([-+][0-9A-Za-z.-]+)?$")


def _validate_sha256(v: str | None) -> str | None:
    """Validate a SHA-256 checksum string (64 lowercase hex chars)."""
    if v is None:
        return v
    normalized = v.lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise ValueError("checksum must be a 64-character lowercase hex SHA-256 digest")
    return normalized


def _validate_rule_version(v: str | None) -> str | None:
    """Validate an explicit project rule version ("v1", "v2", "v1.2")."""
    if v is None:
        return v
    if not _RULE_VERSION_PATTERN.match(v):
        msg = (
            f"rule_version must match the project rule-version convention "
            f"(e.g. 'v1', 'v2'), got {v!r}"
        )
        raise ValueError(msg)
    return v


def _validate_component_version(v: str | None) -> str | None:
    """Validate a numeric dotted component version (e.g. '8.1.0')."""
    if v is None:
        return v
    if not _COMPONENT_VERSION_PATTERN.match(v):
        msg = f"version must be a numeric dotted version (e.g. '8.1.0'), got {v!r}"
        raise ValueError(msg)
    return v


class EvidenceRef(BaseModel, frozen=True):
    """A typed reference linking a material event to its source evidence.

    The ref_uri provides a resolvable location for the artifact
    (e.g., object-storage key, frame ID, or file path).
    """

    model_config = {"extra": "forbid"}

    ref_id: EvidenceId
    schema_version: str = Field(default=SCHEMA_VERSION)
    ref_type: EvidenceType
    ref_uri: str = Field(min_length=1)

    # --- Event linkage (REQUIRED) ---
    event_id: EventId
    event_time: datetime

    # --- Scope (OPTIONAL, validated) ---
    tenant_id: TenantId | None = None
    venue_id: VenueId | None = None

    # --- Source chain (OPTIONAL; media types require at least one) ---
    video_asset_id: VideoAssetId | None = None
    video_session_id: VideoSessionId | None = None
    camera_id: CameraId | None = None

    # --- Time / frame range (OPTIONAL, validated) ---
    start_time: datetime | None = None
    end_time: datetime | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)

    # --- Processing provenance (OPTIONAL, validated) ---
    configuration_version_id: ConfigurationVersionId | None = None
    detector_version: str | None = None
    tracker_version: str | None = None
    rule_id: RuleId | None = None
    rule_version: RuleVersion | None = None
    checksum: str | None = None
    created_at: datetime | None = None

    # --- Free-form provenance metadata (OPTIONAL) ---
    metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_start_time = field_validator("start_time")(validate_utc)
    _validate_end_time = field_validator("end_time")(validate_utc)
    _validate_created_at = field_validator("created_at")(validate_utc)
    _validate_checksum = field_validator("checksum")(_validate_sha256)
    _validate_rule_version = field_validator("rule_version")(_validate_rule_version)
    _validate_detector_version = field_validator("detector_version")(_validate_component_version)
    _validate_tracker_version = field_validator("tracker_version")(_validate_component_version)

    @model_validator(mode="after")
    def _validate_evidence_chain(self) -> EvidenceRef:
        # Tenant/venue scope: a venue is tenant-scoped — a venue reference
        # without its tenant is structurally invalid.
        if self.venue_id is not None and self.tenant_id is None:
            raise ValueError("venue_id requires tenant_id (venues are tenant-scoped)")

        # Time range ordering.
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.end_time < self.start_time
        ):
            raise ValueError("end_time must not precede start_time")

        # Frame range ordering (bounds are validated non-negative by the fields).
        if (
            self.start_frame is not None
            and self.end_frame is not None
            and self.end_frame < self.start_frame
        ):
            raise ValueError("end_frame must be >= start_frame")

        # Media-backed evidence types require source provenance.
        if self.ref_type in _MEDIA_EVIDENCE_TYPES and not (
            self.video_asset_id is not None
            or self.video_session_id is not None
            or self.camera_id is not None
        ):
            msg = (
                f"ref_type={self.ref_type.value} requires source provenance "
                "(video_asset_id, video_session_id, or camera_id)"
            )
            raise ValueError(msg)

        # Rule provenance must preserve its configuration version — never a
        # silent "latest configuration" resolution.
        if self.rule_id is not None and (
            self.configuration_version_id is None or self.rule_version is None
        ):
            msg = (
                "rule_id requires rule_version and configuration_version_id "
                "(configuration provenance must be preserved)"
            )
            raise ValueError(msg)

        return self
