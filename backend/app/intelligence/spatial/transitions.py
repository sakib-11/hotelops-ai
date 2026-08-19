"""Deterministic line-crossing and spatial transition engine (Task 14 Step 4).

Detects deterministic spatial transitions from two consecutive tracked
positions of the SAME track, evaluated against a configured spatial
line owned by the camera and the session's IMMUTABLE published
configuration version.

Architecture (Task 14 Step 4):

    TrackObservation(N), TrackObservation(N+1)  — same track
        ↓ point policy (Step 2 ``extract_point``, consistent for both)
    previous_point, current_point
        ↓
    evaluate_line_crossing(configuration + camera_id + line + points)
        ↓
    LineCrossingObservation

The engine is PURE and DETERMINISTIC: no database, Redis, HTTP,
object-storage, or LLM calls, no current time, no randomness, and no
access to "the latest configuration". Repository/service layers resolve
the EXACT version a session pins to BEFORE calling the engine; the
engine only re-asserts the invariants it can check from its inputs.

Line representation (section 2): the configured line is the canonical
Task 10 ``EntranceModel`` — the only line-capable entity in the
configuration (LINESTRING threshold). It is version-owned (deterministic
``profile_id``) and bound to cameras via ``camera_profiles`` (empty =
all cameras) or, for camera-scoped geometry, via
``reference_camera_profile_id``. There are no global lines.

Crossing rule (sections 4-5): the movement segment P(previous) →
P(current) CROSSES the line iff it PROPERLY intersects at least one
edge of the line. A proper intersection requires BOTH movement
endpoints strictly on opposite sides of the edge's supporting line AND
BOTH edge endpoints strictly on opposite sides of the movement segment
— the signed-side test is the deterministic cross product
(``geometry.segments``). Closeness to the line is never a crossing.

Boundary policy (section 7, tolerance = ``GEOMETRY_TOLERANCE`` only):
  - previous point ON the line   → NO_CROSSING (defined, never a crossing)
  - current point ON the line    → NO_CROSSING
  - both points ON the line      → NO_CROSSING
  - same side                    → NO_CROSSING
  - opposite sides               → CROSSED (when the intersection lies
    within BOTH segment extents; crossing the line's infinite extension
    beyond its endpoints is NO_CROSSING)
  - collinear overlap            → NO_CROSSING
  - endpoint touch               → NO_CROSSING (touch is not a crossing)

"ON the line" is measured against the line's ACTUAL extent: the shared
``distance_point_to_segment`` (the same extent-aware distance the Step 2
polygon layer uses for BOUNDARY classification). A point near an edge's
supporting-line EXTENSION but beyond the edge's endpoints is NOT on the
line, and an endpoint within tolerance of one edge does not suppress a
genuine proper crossing of a different edge of the same polyline.
Floating-point noise can never flip a result.

Note on multi-edge polylines: if an endpoint genuinely lies ON one edge
(within tolerance of that edge's extent), the movement is NO_CROSSING
per the boundary policy even when the segment would also cross another
edge — the transition into/out of the on-line state reports that
movement, and "endpoint on line" always wins (deterministic).

Direction (section 6): when the line declares directional semantics
(``EntranceDirection`` other than BIDIRECTIONAL), a crossing is
FORWARD (left → right relative to the crossed edge's vertex order) or
REVERSE (right → left). BIDIRECTIONAL lines (direction not configured)
yield UNKNOWN — the engine never invents direction. FORWARD/REVERSE are
geometric labels; the ENTRANCE/EXIT semantic label is business meaning
the engine ignores (section 12).

Provenance and isolation:
  - Configuration version: both observations are evaluated against the
    exact pinned version; non-PUBLISHED versions are rejected and there
    is never a fallback to the latest (sections 10, 14-test 9).
  - Camera isolation: the camera must exist in the pinned version and
    the line must be bound to it (section 11).
  - Same track/scope: previous.track_id == current.track_id and the
    session must match (section 8). A transition is NEVER manufactured
    from two different tracks.
  - Frame order: a duplicate frame id or event-time regression raises
    ``TransitionOrderError``; intermediate-frame gaps are ALLOWED
    (Task 13: skipped indices are legal) (section 9).

Recorded blocker (from Task 14 Step 3): a VENUE_LOCAL configured line
against IMAGE_NORMALIZED track points raises ``VenuePointRequiredError``
— the camera→venue projection (ADR-010 INV-CS-01 CameraCalibration) is
not part of the Task 10 configuration model. Camera-scoped
(IMAGE_NORMALIZED) lines are supported by this engine's space-agnostic
boundary; the current Task 10 entity contract restricts entrance
geometry to VENUE_LOCAL, so such lines await a Task 10 model extension.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.app.intelligence.geometry import (
    GEOMETRY_TOLERANCE,
    GeometryError,
    LineSide,
    Point2D,
    distance_point_to_segment,
    side_of_line,
    validate_coordinate,
    validate_linestring,
)
from backend.app.intelligence.spatial.exceptions import (
    CameraNotInConfigurationError,
    ConfigurationNotPublishedError,
    InvalidLineError,
    InvalidSpatialInputError,
    LineNotApplicableError,
    ReferenceIntegrityError,
    TransitionOrderError,
    TransitionScopeError,
    VenuePointRequiredError,
)
from contracts.common import CameraId
from contracts.configuration import (
    CameraProfileModel,
    ConfigurationStatus,
    ConfigurationVersionModel,
    EntranceDirection,
    EntranceModel,
)
from contracts.geometry import CoordinateSpace, GeometryScope
from contracts.spatial import (
    SPATIAL_ENGINE_VERSION,
    CrossingDirection,
    CrossingState,
    LineCrossingObservation,
    SpatialPointModel,
)
from contracts.vision import TrackObservation

__all__ = ["LineCrossingInput", "evaluate_line_crossing"]


@dataclass(frozen=True, slots=True)
class LineCrossingInput:
    """Pure-engine inputs for one spatial transition evaluation.

    ``configuration`` MUST be the exact immutable PUBLISHED version the
    session pins to — never the latest. ``line`` is the configured
    spatial line (Task 10 ``EntranceModel``) resolved from that version
    by the caller. ``previous_point``/``current_point`` are the canonical
    points extracted from the two observations with the SAME configured
    point policy (Step 2 ``extract_point``); the tracks carry the
    provenance and ordering contract.
    """

    configuration: ConfigurationVersionModel
    previous_track: TrackObservation
    current_track: TrackObservation
    camera_id: CameraId
    previous_point: SpatialPointModel
    current_point: SpatialPointModel
    line: EntranceModel

    def __post_init__(self) -> None:
        # Typed error contract: the same ``InvalidSpatialInputError`` the
        # engine raises for malformed inputs, so callers catch ONE type.
        if self.configuration is None:
            raise InvalidSpatialInputError("configuration (pinned published version) is required")
        if self.previous_track is None:
            raise InvalidSpatialInputError(
                "previous_track (canonical TrackObservation) is required"
            )
        if self.current_track is None:
            raise InvalidSpatialInputError("current_track (canonical TrackObservation) is required")
        if self.camera_id is None:
            raise InvalidSpatialInputError("camera_id (physical CameraId) is required")
        if self.previous_point is None:
            raise InvalidSpatialInputError(
                "previous_point (canonical SpatialPointModel) is required"
            )
        if self.current_point is None:
            raise InvalidSpatialInputError(
                "current_point (canonical SpatialPointModel) is required"
            )
        if self.line is None:
            raise InvalidSpatialInputError("line (configured spatial line) is required")


def _validate_point(point: SpatialPointModel) -> None:
    """Validate one canonical point at the engine boundary (never repairs)."""
    try:
        validate_coordinate(point.x, point.y, coordinate_space=point.coordinate_space)
    except GeometryError as exc:
        raise InvalidSpatialInputError(
            f"spatial point failed validation: {exc.message}", cause=exc
        ) from exc


def _resolve_camera(
    configuration: ConfigurationVersionModel, camera_id: CameraId
) -> CameraProfileModel:
    """Return the camera profile matching the physical ``camera_id``.

    Camera isolation (section 11): the track's camera must be part of
    the pinned configuration version.
    """
    for camera in configuration.cameras:
        if camera.camera_id == camera_id:
            return camera
    raise CameraNotInConfigurationError(
        f"camera {camera_id} is not configured in the pinned configuration "
        f"version {configuration.configuration_version_id}"
    )


def _assert_line_applies_to_camera(line: EntranceModel, camera: CameraProfileModel) -> None:
    """A configured line applies only to its declared cameras (section 11).

    Empty ``camera_profiles`` means the line applies to every camera
    (Task 10 semantics). Camera-scoped geometry must reference the exact
    camera profile.
    """
    if line.geometry.geometry_scope is GeometryScope.CAMERA:
        if line.geometry.reference_camera_profile_id != camera.profile_id:
            raise LineNotApplicableError(
                f"line '{line.profile_id}' is camera-scoped for profile "
                f"'{line.geometry.reference_camera_profile_id}', not camera "
                f"'{camera.profile_id}'"
            )
        return
    if line.camera_profiles and camera.profile_id not in line.camera_profiles:
        raise LineNotApplicableError(
            f"line '{line.profile_id}' is not declared for camera '{camera.profile_id}'"
        )


def _raise_space_mismatch(
    line: EntranceModel, point_space: CoordinateSpace, camera: CameraProfileModel
) -> None:
    """Recorded projection blocker: venue lines need a VENUE_LOCAL point."""
    if (
        point_space is CoordinateSpace.IMAGE_NORMALIZED
        and line.geometry.coordinate_space is CoordinateSpace.VENUE_LOCAL
    ):
        raise VenuePointRequiredError(
            f"points are IMAGE_NORMALIZED but line '{line.profile_id}' is VENUE_LOCAL; "
            f"line crossing requires a VENUE_LOCAL point and the camera→venue projection "
            "(ADR-010 INV-CS-01 CameraCalibration) is not part of the Task 10 configuration "
            "model — recorded blocker, no silent result"
        )
    raise InvalidLineError(
        f"line '{line.profile_id}' uses coordinate space "
        f"{line.geometry.coordinate_space.value} which does not match the point "
        f"coordinate space {point_space.value} — cross-space evaluation is forbidden"
    )


def _edges(vertices: Sequence[Point2D]) -> tuple[tuple[Point2D, Point2D], ...]:
    """Consecutive vertex pairs of the line (polyline edges)."""
    return tuple((vertices[i], vertices[i + 1]) for i in range(len(vertices) - 1))


def _classify_crossing(
    p1: Point2D,
    p2: Point2D,
    vertices: Sequence[Point2D],
    *,
    directional: bool,
) -> tuple[CrossingState, CrossingDirection]:
    """Proper-intersection crossing of the movement segment vs the polyline.

    Boundary policy (documented, deterministic, single tolerance):
    an endpoint within ``GEOMETRY_TOLERANCE`` of the line's ACTUAL
    extent (extent-aware distance-to-segment, shared with the polygon
    layer) yields NO_CROSSING — an on-line endpoint never becomes a
    crossing (the transition into/out of the on-line state reports it).
    A proper crossing of ANY edge yields CROSSED; direction comes from
    the first crossed edge in vertex order.
    """
    for a, b in _edges(vertices):
        if distance_point_to_segment(p1, a, b) <= GEOMETRY_TOLERANCE:
            return CrossingState.NO_CROSSING, CrossingDirection.UNKNOWN
        if distance_point_to_segment(p2, a, b) <= GEOMETRY_TOLERANCE:
            return CrossingState.NO_CROSSING, CrossingDirection.UNKNOWN

    for a, b in _edges(vertices):
        s1 = side_of_line(a, b, p1)
        s2 = side_of_line(a, b, p2)
        if s1 is s2:
            continue  # same side — no crossing
        t1 = side_of_line(p1, p2, a)
        t2 = side_of_line(p1, p2, b)
        if t1 is LineSide.ON_LINE or t2 is LineSide.ON_LINE:
            continue  # the line touches the movement segment — not a crossing
        if t1 is t2:
            continue  # intersection lies beyond the line's extent
        if directional:
            direction = (
                CrossingDirection.FORWARD
                if s1 is LineSide.LEFT and s2 is LineSide.RIGHT
                else CrossingDirection.REVERSE
            )
        else:
            direction = CrossingDirection.UNKNOWN
        return CrossingState.CROSSED, direction

    return CrossingState.NO_CROSSING, CrossingDirection.UNKNOWN


def evaluate_line_crossing(evaluation: LineCrossingInput) -> LineCrossingObservation:
    """Evaluate one spatial transition of a track across a configured line.

    Pure and deterministic: no I/O, no current time, no randomness, no
    fallback to the latest configuration. Raises the typed
    ``SpatialEvaluationError`` taxonomy on any failure — a failure is
    never encoded as CROSSED/NO_CROSSING.
    """
    if not isinstance(evaluation, LineCrossingInput):
        raise InvalidSpatialInputError(
            f"evaluation must be a LineCrossingInput, got {type(evaluation).__name__}"
        )

    configuration = evaluation.configuration
    previous_track = evaluation.previous_track
    current_track = evaluation.current_track
    camera_id = evaluation.camera_id
    previous_point = evaluation.previous_point
    current_point = evaluation.current_point
    line = evaluation.line

    # --- Input boundary (defensive re-assertion; never repaired) ---
    if not isinstance(configuration, ConfigurationVersionModel):
        raise InvalidSpatialInputError("configuration (pinned published version) is required")
    if not isinstance(previous_track, TrackObservation):
        raise InvalidSpatialInputError("previous_track (canonical TrackObservation) is required")
    if not isinstance(current_track, TrackObservation):
        raise InvalidSpatialInputError("current_track (canonical TrackObservation) is required")
    if not isinstance(previous_point, SpatialPointModel):
        raise InvalidSpatialInputError("previous_point (canonical SpatialPointModel) is required")
    if not isinstance(current_point, SpatialPointModel):
        raise InvalidSpatialInputError("current_point (canonical SpatialPointModel) is required")
    if not isinstance(camera_id, UUID):
        raise InvalidSpatialInputError("camera_id (physical CameraId) is required")
    if not isinstance(line, EntranceModel):
        raise InvalidSpatialInputError("line (configured spatial line) is required")

    # --- Point validation BEFORE configuration traversal, so a malformed
    # point is never masked by (or confused with) a config problem ---
    _validate_point(previous_point)
    _validate_point(current_point)
    if previous_point.coordinate_space != current_point.coordinate_space:
        raise InvalidSpatialInputError(
            "previous_point and current_point must share a coordinate space"
        )
    if previous_point.policy != current_point.policy:
        raise InvalidSpatialInputError(
            "previous_point and current_point must use the same spatial point policy "
            "(the configured point policy must be applied consistently)"
        )

    # --- Configuration version provenance (section 10) ---
    if configuration.status is not ConfigurationStatus.PUBLISHED:
        raise ConfigurationNotPublishedError(
            f"configuration version {configuration.configuration_version_id} is "
            f"'{configuration.status.value}'; spatial transitions require the immutable "
            "PUBLISHED version pinned by the session (never the latest)"
        )

    camera = _resolve_camera(configuration, camera_id)

    # --- Same-track + session scope (section 8) ---
    if previous_track.track_id != current_track.track_id:
        raise TransitionScopeError(
            f"previous track {previous_track.track_id} and current track "
            f"{current_track.track_id} differ; a transition requires the SAME track"
        )
    if previous_track.session_id != current_track.session_id:
        raise TransitionScopeError(
            f"previous observation session {previous_track.session_id} and current "
            f"session {current_track.session_id} differ; a transition never crosses "
            "sessions"
        )

    # --- Frame order (section 9, Task 11/13 policy) ---
    if current_track.frame_id == previous_track.frame_id:
        raise TransitionOrderError(
            f"duplicate/out-of-order frame {current_track.frame_id}: the current "
            "observation does not advance the frame sequence"
        )
    if current_track.event_time < previous_track.event_time:
        raise TransitionOrderError(
            f"event_time regression: current {current_track.event_time.isoformat()} "
            f"precedes previous {previous_track.event_time.isoformat()}"
        )
    # Intermediate-frame gaps are ALLOWED (Task 13: skipped indices are legal).

    # --- Line membership + camera binding (sections 2, 10, 11) ---
    config_line = next(
        (
            entrance
            for entrance in configuration.entrances
            if entrance.profile_id == line.profile_id
        ),
        None,
    )
    if config_line is None:
        raise ReferenceIntegrityError(
            f"line '{line.profile_id}' is not part of configuration version "
            f"{configuration.configuration_version_id}"
        )
    if config_line != line:
        # Reference integrity is profile_id-AND-geometry: the caller
        # must resolve the line FROM the pinned version, so a line with
        # a matching profile_id but foreign geometry is rejected rather
        # than silently evaluated against the caller's geometry.
        raise ReferenceIntegrityError(
            f"line '{line.profile_id}' does not match the entrance pinned in "
            f"configuration version {configuration.configuration_version_id} "
            "(resolve the line from the pinned version, never from elsewhere)"
        )
    _assert_line_applies_to_camera(line, camera)

    # --- Line geometry + coordinate space (sections 2-3) ---
    try:
        vertices = validate_linestring(line.geometry)
    except GeometryError as exc:
        raise InvalidLineError(
            f"configured line '{line.profile_id}' failed validation: {exc.message}", cause=exc
        ) from exc
    if line.geometry.coordinate_space != previous_point.coordinate_space:
        _raise_space_mismatch(line, previous_point.coordinate_space, camera)

    # --- Crossing + direction (sections 4-6) ---
    crossing_state, direction = _classify_crossing(
        (previous_point.x, previous_point.y),
        (current_point.x, current_point.y),
        vertices,
        directional=line.direction is not EntranceDirection.BIDIRECTIONAL,
    )

    return LineCrossingObservation(
        session_id=current_track.session_id,
        track_id=current_track.track_id,
        camera_id=camera_id,
        configuration_version_id=configuration.configuration_version_id,
        line_profile_id=line.profile_id,
        previous_frame_id=previous_track.frame_id,
        current_frame_id=current_track.frame_id,
        previous_event_time=previous_track.event_time,
        current_event_time=current_track.event_time,
        previous_point=previous_point,
        current_point=current_point,
        crossing_state=crossing_state,
        direction=direction,
        engine_version=SPATIAL_ENGINE_VERSION,
    )
