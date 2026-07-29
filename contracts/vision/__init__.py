"""Canonical computer-vision observation contracts for HotelOps AI."""

from contracts.vision.models import (
    BoundingBox,
    DetectionObservation,
    TrackObservation,
    TrackState,
)

__all__ = [
    "BoundingBox",
    "DetectionObservation",
    "TrackObservation",
    "TrackState",
]
