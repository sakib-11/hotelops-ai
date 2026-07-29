"""Canonical evidence reference contract.

Represents a reference to evidence that may eventually point to a frame,
image, video clip, object-storage object, or analytical artifact.
Does NOT embed binary evidence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    EvidenceId,
    validate_schema_version,
)


class EvidenceType(StrEnum):
    """The kind of artifact an evidence reference points to."""

    FRAME = "frame"
    IMAGE = "image"
    VIDEO_CLIP = "video_clip"
    OBJECT_STORAGE = "object_storage"
    ANALYTICAL_ARTIFACT = "analytical_artifact"


class EvidenceRef(BaseModel, frozen=True):
    """A typed reference to an evidence artifact.

    The ref_uri provides a resolvable location for the artifact
    (e.g., object-storage key, frame ID, or file path).
    """

    model_config = {"extra": "forbid"}

    ref_id: EvidenceId
    schema_version: str = Field(default=SCHEMA_VERSION)
    ref_type: EvidenceType
    ref_uri: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
