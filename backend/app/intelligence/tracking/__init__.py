"""Multi-object tracking abstraction (Task 13).

- ``base`` — the stable ``ObjectTracker`` boundary: input/output
  types, typed tracker configuration, deterministic session-scoped
  track-id derivation, and the tracking provenance helper.
- ``exceptions`` — the typed error taxonomy downstream code depends on
  (backend errors never cross the boundary).
- ``bytetrack_adapter`` — the concrete tracking adapter, isolated
  behind ``ObjectTracker`` exactly like the YOLO adapter is isolated
  behind ``ObjectDetector``.

Concrete tracker backends implement ``ObjectTracker`` and are confined
behind this boundary: no tracker SDK type is ever visible here, and
business logic consumes only the canonical Task 4 ``TrackObservation``.
"""

from backend.app.intelligence.tracking.base import (
    DEFAULT_TRACKER_CONFIG,
    ObjectTracker,
    TrackerConfig,
    TrackingInput,
    box_iou,
    track_uuid,
    validate_tracking_provenance,
)
from backend.app.intelligence.tracking.bytetrack_adapter import (
    ByteTrackAdapter,
    TrackerStats,
)
from backend.app.intelligence.tracking.exceptions import (
    TrackClassSwitchError,
    TrackingError,
    TrackingExecutionError,
    TrackOrderError,
    TrackScopeError,
)

__all__ = [
    "DEFAULT_TRACKER_CONFIG",
    "ByteTrackAdapter",
    "ObjectTracker",
    "TrackClassSwitchError",
    "TrackOrderError",
    "TrackScopeError",
    "TrackerConfig",
    "TrackerStats",
    "TrackingError",
    "TrackingExecutionError",
    "TrackingInput",
    "box_iou",
    "track_uuid",
    "validate_tracking_provenance",
]
