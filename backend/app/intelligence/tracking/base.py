"""Multi-object tracking abstraction (Task 13).

The stable application boundary between canonical detections (Task 4
``DetectionObservation``) and any concrete tracking backend.  Business
logic depends ONLY on this module and the canonical Task 4
``TrackObservation`` contract — never on a tracker SDK's types.

Explicit contract behavior:

- Input
    ``update()`` consumes a canonical ``FramePacket`` (frame/session
    context) plus the canonical ``DetectionObservation`` values for
    that frame.  The tracker never consumes detector-SDK output.
- Output
    ``update()`` returns canonical ``TrackObservation`` values.  Each
    observation links the track to the detection that matched it this
    frame (``detection_id``), carries the frame/session identity, and
    declares a lifecycle state (``TrackState``: ACTIVE / LOST /
    TERMINATED).  An empty frame with no tracks returns ``[]`` — a
    valid "no tracks" outcome, never an error.
- Track identity
    ``track_uuid()`` derives a deterministic, session-scoped canonical
    ``TrackId`` (uuid5) from ``(session_id, tracker-local id)`` — the
    same tracker-local id in two sessions never collides, and replay
    over the same input reproduces identical track ids.
- Lifecycle
    The tracker distinguishes ACTIVE (continuing), LOST (temporarily
    missing, retained by the configurable lost-track buffer) and
    TERMINATED (permanently gone, emitted exactly once).  Missing
    detections never silently create a new track.
- Frame order
    Frames must be ordered: duplicate frames, frame-index regression
    and timestamp regression are structural errors
    (``TrackOrderError``); skipped indices are allowed.
- Scope
    A tracker instance is bound to ONE ``session_id`` at construction;
    frames from another session/source raise ``TrackScopeError``.
- Failure
    Backend failures raise ``TrackingExecutionError`` (never a raw
    SDK exception) and NEVER produce fabricated ``TrackObservation``
    values.  ``NO TRACKS`` and ``TRACKER FAILURE`` are distinct.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from backend.app.intelligence.tracking.exceptions import TrackingError
from contracts.common import TrackId, VideoSessionId
from contracts.video import FramePacket
from contracts.vision import BoundingBox, DetectionObservation, TrackObservation

__all__ = [
    "DEFAULT_TRACKER_CONFIG",
    "ObjectTracker",
    "TrackerConfig",
    "TrackingInput",
    "box_iou",
    "track_uuid",
    "validate_tracking_provenance",
]

#: Namespace for deterministic session-scoped canonical track ids (uuid5).
_TRACK_ID_NAMESPACE = uuid.NAMESPACE_URL


def track_uuid(session_id: VideoSessionId, local_track_id: int) -> TrackId:
    """Derive the canonical ``TrackId`` for ``(session, tracker-local id)``.

    Deterministic (uuid5) so replay/regression over the same input
    reproduces identical ids, and session-scoped so the same
    tracker-local id under two sessions never collides.  ``local_track_id``
    is the tracker backend's own numeric id — never assumed globally
    unique.
    """
    return TrackId(uuid.uuid5(_TRACK_ID_NAMESPACE, f"{session_id}:{local_track_id}"))


def box_iou(a: BoundingBox, b: BoundingBox) -> float:
    """Intersection-over-union of two normalized boxes (deterministic).

    Returns ``0.0`` for disjoint or zero-area boxes; both boxes are
    already contract-validated to [0, 1] with ordered corners.
    """
    inter_w = max(0.0, min(a.x_max, b.x_max) - max(a.x_min, b.x_min))
    inter_h = max(0.0, min(a.y_max, b.y_max) - max(a.y_min, b.y_min))
    intersection = inter_w * inter_h
    area_a = (a.x_max - a.x_min) * (a.y_max - a.y_min)
    area_b = (b.x_max - b.x_min) * (b.y_max - b.y_min)
    union = area_a + area_b - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Typed, bounded tracking configuration (Task 13, Step 12).

    Nothing is hardcoded in the adapter: the lost-track retention
    (``track_buffer``), matching thresholds, detection-confidence gate
    and the frame-rate assumption are all explicit configuration.
    """

    track_thresh: float = 0.5
    match_thresh: float = 0.8
    track_buffer: int = 30
    frame_rate: int = 30
    min_hits: int = 1
    # IoU floor used ONLY to link a tracker-emitted target to the
    # canonical detection of this frame (provenance association).
    detection_match_iou: float = 0.5
    # Class-consistency policy: a track must not silently switch object
    # class; an explicit policy may allow it.
    allow_class_switch: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.track_thresh <= 1.0:
            msg = f"track_thresh must be in [0, 1], got {self.track_thresh}"
            raise ValueError(msg)
        if not 0.0 <= self.match_thresh <= 1.0:
            msg = f"match_thresh must be in [0, 1], got {self.match_thresh}"
            raise ValueError(msg)
        if not 0.0 <= self.detection_match_iou <= 1.0:
            msg = f"detection_match_iou must be in [0, 1], got {self.detection_match_iou}"
            raise ValueError(msg)
        if self.track_buffer < 0:
            msg = f"track_buffer must be >= 0, got {self.track_buffer}"
            raise ValueError(msg)
        if self.frame_rate < 1:
            msg = f"frame_rate must be >= 1, got {self.frame_rate}"
            raise ValueError(msg)
        if self.min_hits < 0:
            msg = f"min_hits must be >= 0, got {self.min_hits}"
            raise ValueError(msg)


#: Defaults used when the caller supplies no tracker configuration.
DEFAULT_TRACKER_CONFIG = TrackerConfig()


@dataclass(frozen=True, slots=True)
class TrackingInput:
    """One frame presented to a tracker.

    Carries the canonical ``FramePacket`` (session identity + frame
    ordering + event time) and the canonical ``DetectionObservation``
    values for that frame — never detector-SDK output.
    """

    frame: FramePacket
    detections: Sequence[DetectionObservation]

    def __post_init__(self) -> None:
        if self.frame is None:
            msg = "frame is required"
            raise ValueError(msg)


@runtime_checkable
class ObjectTracker(Protocol):
    """The stable tracker boundary.

    Implementations (a concrete backend, a mock, a test double) must:

    - return only canonical ``TrackObservation`` values built from the
      input frame and its detections;
    - preserve session-scoped track identity
      (``track_uuid(session, local_id)``);
    - raise ``TrackOrderError``/``TrackScopeError`` for structural
      input violations and ``TrackingExecutionError`` (never a provider
      exception) for backend failures;
    - propagate ``asyncio.CancelledError`` and stay reusable after
      cancellation;
    - expose ``tracker_id`` so tracking provenance is observable.
    """

    @property
    def tracker_id(self) -> str:
        """Identity of the tracking backend (recorded in metadata)."""
        ...

    async def update(self, inp: TrackingInput) -> list[TrackObservation]:
        """Associate detections for one frame and emit track observations.

        Returns ``[]`` for a valid "no tracks" outcome.  Raises the
        typed ``TrackingError`` taxonomy on failure.
        """
        ...

    async def close(self) -> None:
        """Release backend resources and reset track state (idempotent)."""
        ...


def validate_tracking_provenance(
    frame: FramePacket, detections: Sequence[DetectionObservation]
) -> None:
    """Enforce the tracking provenance invariant on the input detections.

    Every detection must belong to the exact ``FramePacket`` being
    tracked (frame identity + session when present).  A violation is a
    programming error upstream and raises ``TrackingError`` before the
    backend is touched — no fabricated track can be produced from
    another frame's detections.
    """
    for det in detections:
        if det.frame_id != frame.frame_id:
            msg = (
                f"detection {det.detection_id} belongs to frame {det.frame_id}, "
                f"not input frame {frame.frame_id}"
            )
            raise TrackingError(msg)
        if det.session_id is not None and det.session_id != frame.session_id:
            msg = (
                f"detection {det.detection_id} session {det.session_id} does not "
                f"match input frame session {frame.session_id}"
            )
            raise TrackingError(msg)
