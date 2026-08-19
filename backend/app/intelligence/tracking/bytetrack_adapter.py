"""ByteTrack adapter — the concrete tracker behind the ObjectTracker port.

THIS module is the ONLY place in the application that references the
tracking SDK.  Everything below is confined here:

- the SDK is imported lazily via :func:`importlib.import_module`
  (never a module-level import), so the rest of the application imports
  and runs without the ``cv`` extras installed;
- ByteTrack's ``STrack``/``BYTETracker`` objects exist only inside this
  module's functions and never cross the boundary;
- the public surface exposes only the ``ObjectTracker`` protocol types
  (``TrackingInput``, ``TrackObservation``) and typed ``TrackingError``
  exceptions.

Behavior contract (Task 13):

1.  The tracker is bound to ONE ``session_id`` at construction —
    tracker state never leaks between sessions or sources.  Frames
    from another session/source are rejected with ``TrackScopeError``.
2.  Frames must be ordered: duplicate frames, frame-index regression
    and timestamp regression are ``TrackOrderError``; skipped indices
    are allowed.
3.  Detections must belong to the tracked frame and carry a numeric
    ``class_id`` (tracking requires class-aware association) —
    violations are typed ``TrackingError`` values, never silently
    absorbed.
4.  Canonical detections are converted to the backend's row format
    (normalized x1/y1/x2/y2/conf/class) with ``img_info=(1.0, 1.0)``
    so coordinates stay in the canonical [0, 1] space end to end.
5.  Track identity is session-scoped and deterministic: the backend's
    numeric id maps to ``track_uuid(session, local_id)`` (uuid5).
6.  Lifecycle: ACTIVE (matched), LOST (backend-reported lost, retained
    by the configurable buffer), TERMINATED (emitted exactly once when
    the backend drops a track).  A missing detection never silently
    fabricates a new track.
7.  Class consistency: a track's class is fixed at first sighting; a
    class switch is a ``TrackClassSwitchError`` unless
    ``allow_class_switch`` is explicitly configured.
8.  Backend failures are typed ``TrackingExecutionError`` with the
    original cause attached — never leaked raw, and never converted
    into "zero tracks".
9.  ``close()`` releases the backend and resets track state; a later
    ``update()`` re-acquires a FRESH tracker — old track ids are never
    pretended to still be active after a restart.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_TRACKS,
    record_pipeline_metric,
)
from backend.app.intelligence.tracking.base import (
    DEFAULT_TRACKER_CONFIG,
    ObjectTracker,
    TrackerConfig,
    TrackingInput,
    box_iou,
    track_uuid,
    validate_tracking_provenance,
)
from backend.app.intelligence.tracking.exceptions import (
    TrackClassSwitchError,
    TrackingError,
    TrackingExecutionError,
    TrackOrderError,
    TrackScopeError,
)
from contracts.common import VideoSessionId
from contracts.vision import BoundingBox, DetectionObservation, TrackObservation, TrackState

__all__ = ["ByteTrackAdapter", "TrackerStats"]


def _import_bytetrack() -> Any:
    """Import the tracking SDK lazily, raising a typed error when absent."""
    try:
        return importlib.import_module("bytetrack")
    except ImportError as exc:
        msg = "the tracking SDK is not installed; install the 'cv' extras to use this adapter"
        raise TrackingExecutionError(msg, cause=exc) from exc


def _to_dets_array(rows: list[list[float]]) -> Any:
    """Build the backend row array (normalized x1 y1 x2 y2 conf class).

    Numpy is a documented ``cv``-extras dependency and imported lazily;
    the backend's ``update()`` requires a real ndarray for its internal
    slicing.
    """
    try:
        numpy = importlib.import_module("numpy")
    except ImportError as exc:
        msg = "tracking requires numpy (install the 'cv' extras)"
        raise TrackingExecutionError(msg, cause=exc) from exc
    return numpy.asarray(rows, dtype=numpy.float64)


@dataclass(frozen=True, slots=True)
class TrackerStats:
    """Atomic snapshot of the tracker's observability counters.

    Recorded per tracker-instance lifetime (``close()`` resets them).
    """

    tracker_id: str
    session_id: VideoSessionId
    total_updates: int
    total_frames: int
    total_tracks_created: int
    total_tracks_ended: int
    active_tracks: int
    total_detections: int
    total_failed_updates: int
    last_update_seconds: float | None
    total_update_seconds: float


class ByteTrackAdapter(ObjectTracker):
    """ByteTrack-backed ``ObjectTracker`` implementation.

    All SDK interaction is lazy and confined to this class; the backend
    tracker is acquired on first ``update()`` and released by
    ``close()``.  The backend is configured from the typed
    ``TrackerConfig`` (nothing hardcoded), and every emitted observation
    is a canonical ``TrackObservation`` with session-scoped identity.
    """

    def __init__(
        self,
        *,
        session_id: VideoSessionId,
        config: TrackerConfig | None = None,
    ) -> None:
        self._session_id = session_id
        self._config = config or DEFAULT_TRACKER_CONFIG
        self._tracker: Any | None = None
        self._sdk_version: str = "unknown"
        # Track state, scoped to this tracker instance (and session).
        self._seen_tracks: set[int] = set()
        self._track_classes: dict[int, str] = {}
        self._last_detection: dict[int, DetectionObservation] = {}
        self._last_frame: tuple[Any, int, Any] | None = None
        self._first_source_ref: Any | None = None
        # Observability counters.
        self._total_updates = 0
        self._total_frames = 0
        self._tracks_created = 0
        self._tracks_ended = 0
        self._total_detections = 0
        self._total_failed_updates = 0
        self._last_duration: float | None = None
        self._total_duration = 0.0

    # ------------------------------------------------------------------
    # Identity / observability
    # ------------------------------------------------------------------

    @property
    def tracker_id(self) -> str:
        """Identity of the tracking backend."""
        return "bytetrack"

    @property
    def session_id(self) -> VideoSessionId:
        """The session this tracker is bound to (immutable)."""
        return self._session_id

    @property
    def config(self) -> TrackerConfig:
        """The typed tracking configuration (explicit, immutable)."""
        return self._config

    @property
    def active_tracks(self) -> int:
        """Number of tracks currently retained (ACTIVE or LOST)."""
        return len(self._seen_tracks)

    def stats(self) -> TrackerStats:
        """Snapshot of the tracker's observability counters."""
        return TrackerStats(
            tracker_id=self.tracker_id,
            session_id=self._session_id,
            total_updates=self._total_updates,
            total_frames=self._total_frames,
            total_tracks_created=self._tracks_created,
            total_tracks_ended=self._tracks_ended,
            active_tracks=self.active_tracks,
            total_detections=self._total_detections,
            total_failed_updates=self._total_failed_updates,
            last_update_seconds=self._last_duration,
            total_update_seconds=self._total_duration,
        )

    # ------------------------------------------------------------------
    # ObjectTracker protocol
    # ------------------------------------------------------------------

    async def update(self, inp: TrackingInput) -> list[TrackObservation]:
        """Associate this frame's detections and emit track observations.

        Returns ``[]`` for a valid "no tracks" frame.  Structural input
        violations raise ``TrackOrderError``/``TrackScopeError``;
        backend failures raise ``TrackingExecutionError`` (never a raw
        SDK exception, never a fabricated empty result).
        """
        frame = inp.frame
        # NOTE: ordering/scope validation advances ``_last_frame`` BEFORE
        # the counted backend try below, so a backend failure on frame N
        # followed by a caller retry of frame N is a ``TrackOrderError``
        # (duplicate frame) — the caller must not re-send consumed frames.
        self._validate_frame(frame)
        detections = list(inp.detections)
        validate_tracking_provenance(frame, detections)
        start = time.perf_counter()
        failed = False
        try:
            rows = [
                [
                    det.bounding_box.x_min,
                    det.bounding_box.y_min,
                    det.bounding_box.x_max,
                    det.bounding_box.y_max,
                    det.confidence,
                    self._required_class_id(det),
                ]
                for det in detections
            ]
            dets = _to_dets_array(rows)
            targets = self._acquire_tracker().update(dets, (1.0, 1.0), (1, 1))
            observations = self._translate(targets, frame=frame, detections=detections)
        except TrackingError:
            failed = True
            raise
        except Exception as exc:
            failed = True
            msg = f"tracking failed for frame {frame.frame_index} of session {frame.session_id}"
            raise TrackingExecutionError(msg, cause=exc) from exc
        finally:
            self._record(start, len(detections), failed=failed)
        # Task 18.18 — track observations emitted at the tracking boundary.
        record_pipeline_metric(PIPELINE_METRIC_TRACKS, len(observations))
        return observations

    async def close(self) -> None:
        """Release the backend and reset track state (idempotent).

        A later ``update()`` re-acquires a FRESH backend tracker: after
        a restart, old track ids are never pretended to be active.
        """
        self._tracker = None
        self._sdk_version = "unknown"
        self._seen_tracks.clear()
        self._track_classes.clear()
        self._last_detection.clear()
        self._last_frame = None
        self._first_source_ref = None
        self._total_updates = 0
        self._total_frames = 0
        self._tracks_created = 0
        self._tracks_ended = 0
        self._total_detections = 0
        self._total_failed_updates = 0
        self._last_duration = None
        self._total_duration = 0.0

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def _validate_frame(self, frame: Any) -> None:
        """Enforce session/source scope and frame ordering (Steps 9-10)."""
        if frame.session_id != self._session_id:
            raise TrackScopeError(
                f"frame session {frame.session_id} does not match tracker "
                f"session {self._session_id}"
            )
        if self._first_source_ref is None:
            self._first_source_ref = frame.source_ref
        elif frame.source_ref != self._first_source_ref:
            raise TrackScopeError(
                f"frame source {frame.source_ref} differs from the tracker's "
                f"established source {self._first_source_ref}"
            )
        last = self._last_frame
        if last is not None:
            last_id, last_index, last_time = last
            if frame.frame_id == last_id:
                msg = f"duplicate frame {frame.frame_id}"
                raise TrackOrderError(msg)
            if frame.frame_index <= last_index:
                msg = f"frame index {frame.frame_index} does not advance after {last_index}"
                raise TrackOrderError(msg)
            if frame.event_time < last_time:
                msg = f"event_time regression at frame {frame.frame_index}"
                raise TrackOrderError(msg)
        self._last_frame = (frame.frame_id, frame.frame_index, frame.event_time)

    @staticmethod
    def _required_class_id(det: DetectionObservation) -> int:
        """Numeric class identity is required for class-aware association."""
        if det.class_id is None:
            msg = (
                f"detection {det.detection_id} has no class_id; tracking requires "
                "numeric class identity"
            )
            raise TrackingError(msg)
        return det.class_id

    # ------------------------------------------------------------------
    # Backend lifecycle (lazy)
    # ------------------------------------------------------------------

    def _acquire_tracker(self) -> Any:
        """Acquire the backend tracker exactly once (fresh after close)."""
        if self._tracker is not None:
            return self._tracker
        module = _import_bytetrack()
        tracker_cls = getattr(module, "BYTETracker", None)
        if tracker_cls is None:
            msg = "the installed tracking SDK exposes no BYTETracker"
            raise TrackingExecutionError(msg)
        config = self._config
        args = SimpleNamespace(
            track_thresh=config.track_thresh,
            track_buffer=config.track_buffer,
            match_thresh=config.match_thresh,
            frame_rate=config.frame_rate,
            min_hits=config.min_hits,
        )
        try:
            self._tracker = tracker_cls(args)
        except Exception as exc:
            msg = "failed to initialize the tracker"
            raise TrackingExecutionError(msg, cause=exc) from exc
        self._sdk_version = str(getattr(module, "__version__", "unknown"))
        return self._tracker

    # ------------------------------------------------------------------
    # Target translation (the SDK -> canonical boundary)
    # ------------------------------------------------------------------

    def _translate(
        self,
        targets: Any,
        *,
        frame: Any,
        detections: Sequence[DetectionObservation],
    ) -> list[TrackObservation]:
        """Convert backend targets into canonical TrackObservation values.

        Each target is linked to ITS matched canonical detection
        (greedy IoU association over this frame's detections) so
        ``detection_id`` provenance is exact; predicted/lost targets
        reuse the track's most recent detection.  No fabricated
        observations are ever produced.
        """
        if not targets:
            observations: list[TrackObservation] = []
            observations.extend(self._emit_terminated(frame, present_ids=set()))
            return observations
        target_list = list(targets)
        matched = self._associate(target_list, detections)
        observations = []
        present_ids: set[int] = set()
        for target in target_list:
            local_id = int(target.track_id)
            present_ids.add(local_id)
            observations.append(self._emit_target(target, local_id, frame, matched))
        observations.extend(self._emit_terminated(frame, present_ids=present_ids))
        return observations

    def _associate(
        self,
        targets: Sequence[Any],
        detections: Sequence[DetectionObservation],
    ) -> dict[int, DetectionObservation]:
        """Greedy IoU association: backend target -> canonical detection.

        Targets are matched in score-descending order so the most
        confident tracks claim their detections first, limiting
        provenance mis-attribution when targets overlap.  The floor is
        ``config.detection_match_iou`` — deterministic and explicit,
        used only for provenance linking.
        """
        matched: dict[int, DetectionObservation] = {}
        unmatched = list(detections)
        for target in sorted(targets, key=lambda t: float(getattr(t, "score", 0.0)), reverse=True):
            tbox = _target_box(target)
            best_index = -1
            best_iou = self._config.detection_match_iou
            for index, det in enumerate(unmatched):
                iou = box_iou(tbox, det.bounding_box)
                if iou > best_iou:
                    best_iou = iou
                    best_index = index
            if best_index >= 0:
                matched[int(target.track_id)] = unmatched.pop(best_index)
        return matched

    def _emit_target(
        self,
        target: Any,
        local_id: int,
        frame: Any,
        matched: dict[int, DetectionObservation],
    ) -> TrackObservation:
        """Build one canonical observation for a backend target."""
        matched_det = matched.get(local_id)
        detection = matched_det if matched_det is not None else self._last_detection.get(local_id)
        if detection is None:
            # A backend-emitted track with neither a current match nor a
            # past detection cannot be linked — never fabricate one.
            msg = f"tracker emitted new track {local_id} with no matching detection"
            raise TrackingExecutionError(msg)
        # Class consistency: fixed at first sighting unless the explicit
        # policy allows a switch.
        established = self._track_classes.get(local_id)
        if (
            established is not None
            and detection.class_name != established
            and not self._config.allow_class_switch
        ):
            raise TrackClassSwitchError(
                f"track {local_id} switched class from {established!r} to {detection.class_name!r}"
            )
        if local_id not in self._seen_tracks:
            self._seen_tracks.add(local_id)
            self._tracks_created += 1
        self._track_classes[local_id] = detection.class_name
        if matched_det is not None:
            self._last_detection[local_id] = matched_det
        state = TrackState.LOST if bool(getattr(target, "lost", False)) else TrackState.ACTIVE
        return TrackObservation(
            track_id=track_uuid(self._session_id, local_id),
            detection_id=detection.detection_id,
            frame_id=frame.frame_id,
            session_id=frame.session_id,
            event_time=frame.event_time,
            track_state=state,
            tracking_metadata={
                "tracker": self.tracker_id,
                "tracker_version": self._sdk_version,
                "local_track_id": local_id,
                "matched": matched_det is not None,
            },
        )

    def _emit_terminated(self, frame: Any, *, present_ids: set[int]) -> list[TrackObservation]:
        """Emit a final TERMINATED observation for tracks the backend dropped.

        A track absent from the backend's current output has been
        evicted after the lost-track buffer — it is permanently gone.
        The final observation is emitted exactly once, linked to the
        track's most recent detection.
        """
        terminated: list[TrackObservation] = []
        for local_id in list(self._seen_tracks):
            if local_id in present_ids:
                continue
            self._seen_tracks.remove(local_id)
            self._track_classes.pop(local_id, None)
            last = self._last_detection.pop(local_id, None)
            if last is None:
                continue
            self._tracks_ended += 1
            terminated.append(
                TrackObservation(
                    track_id=track_uuid(self._session_id, local_id),
                    detection_id=last.detection_id,
                    frame_id=frame.frame_id,
                    session_id=frame.session_id,
                    event_time=frame.event_time,
                    track_state=TrackState.TERMINATED,
                    tracking_metadata={
                        "tracker": self.tracker_id,
                        "tracker_version": self._sdk_version,
                        "local_track_id": local_id,
                        "matched": False,
                        "terminated": True,
                    },
                )
            )
        return terminated

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def _record(self, duration: float, detections: int, *, failed: bool) -> None:
        """Accumulate tracker observability counters."""
        self._total_updates += 1
        self._total_frames += 1
        self._total_duration += duration
        self._last_duration = duration
        self._total_detections += detections
        if failed:
            self._total_failed_updates += 1


def _target_box(target: Any) -> BoundingBox:
    """The normalized bounding box of a backend target (tlwh -> xyxy)."""
    tlwh = target.tlwh
    x, y, w, h = (float(tlwh[0]), float(tlwh[1]), float(tlwh[2]), float(tlwh[3]))
    return BoundingBox(x_min=x, y_min=y, x_max=x + w, y_max=y + h)
