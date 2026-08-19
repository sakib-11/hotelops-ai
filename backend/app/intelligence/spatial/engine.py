"""Deterministic zone membership, exclusion, and table mapping (Task 14 Steps 3 and 5).

The pure spatial interpretation engine: it converts a canonical spatial
point (produced by the Step 2 geometry layer from a track's bounding
box) plus the session's IMMUTABLE pinned configuration version into a
canonical ``SpatialObservation``.

Architecture (Task 14 Steps 3 and 5):

    TrackObservation
        ↓ point policy (Step 2: ``extract_point``)
    Canonical Spatial Point (``contracts.spatial.SpatialPointModel``)
        ↓
    evaluate_spatial(configuration + camera_id + point)
        ├── privacy evaluation   (camera-scoped privacy ROIs)
        ├── exclusion evaluation (camera-scoped exclusion ROIs)
        ├── zone membership      (camera-scoped zones)
        └── table mapping        (version-scoped tables, Step 5)
        ↓
    SpatialObservation

Step 5 completes the remaining spatial semantics: deterministic table
mapping, the zone/table relationship, and combined zone/table ambiguity
resolution. The engine is PURE and DETERMINISTIC: it performs no
database, Redis, HTTP, object-storage, or LLM calls, reads no current
time, and has no access to "the latest configuration". Repository/
service layers resolve the EXACT version a session pins to
(``ConfigurationService.resolve_session_configuration``) BEFORE calling
the engine; the engine only re-asserts the invariants it can check from
its inputs.

Coordinate spaces (ADR-010, no third format):
  - Zones are always POLYGON in VENUE_LOCAL (entity geometry contract).
  - Tables are always POLYGON in VENUE_LOCAL (entity geometry
    contract). They are VERSION-scoped venue geometry: Task 10 defines
    no camera→table binding, so every table in the pinned version is a
    candidate for any camera in that version (camera isolation is
    preserved through the pinned version and the VENUE_LOCAL point
    requirement).
  - Exclusion/privacy ROIs are POLYGON in IMAGE_NORMALIZED (camera-
    scoped) or VENUE_LOCAL (venue-scoped).
  - A point is evaluated ONLY against geometry in the point's own
    coordinate space; cross-space operations are forbidden (ADR-010
    coordinate space fidelity). Camera-relative ROIs therefore apply to
    IMAGE_NORMALIZED points and venue-relative geometry applies to
    VENUE_LOCAL points.

Camera scoping: a track is evaluated ONLY against the geometry its
camera declares — the physical ``camera_id`` must exist in the pinned
configuration version, and evaluation is bounded to that camera's
``detection_zones`` / ``privacy_rois`` / ``exclusion_rois`` profile
ids. Candidate sets are sorted by profile_id so results never depend on
database or list ordering.

Recorded decisions and blockers (never invent business semantics):

  - Zone overlap precedence: Task 10 defines NO zone priority. A point
    matching multiple zones is reported AMBIGUOUS (and, per the
    ``SpatialObservation`` contract, carries no zone identity) unless
    the caller supplies an explicit ``zone_priority`` list — an
    evaluation-time policy input, not a Task 10 model field. When a
    priority list is given, the first priority-listed zone among the
    matches becomes the primary zone (INSIDE).
  - Table mapping (Step 5): Task 10 defines no table priority either.
    A point matching multiple tables resolves via the explicit
    ``table_priority`` input or is AMBIGUOUS. AMBIGUOUS is the single
    combined ambiguity state: overlapping zones OR overlapping tables
    (never arbitrarily picking the first match).
  - Zone/table relationship (Step 5): the relationship is the
    configuration-declared ``ZoneModel.contained_tables`` — never
    assumed to be "table == zone". A table may be contained in a zone,
    overlap a zone, or have no zone relationship; a point at a table
    with no matching zone is OUTSIDE with the table identity retained.
    ``contained_tables`` references are validated eagerly for every
    evaluation (a dangling reference fails loudly even when the zone
    never matches — configuration integrity is never lazily ignored).
  - Boundary policy: Task 10 defines no boundary membership policy. A
    BOUNDARY classification is NEVER silently converted to INSIDE or
    OUTSIDE — the engine raises ``BoundaryPolicyUndefinedError``. This
    applies to zones AND tables AND ROIs alike.
    BLOCKER: an explicit boundary policy must be defined (Task 10)
    before boundary points can be classified.
  - Camera → venue projection: zone membership, table mapping, and
    venue-scoped ROI evaluation require a VENUE_LOCAL point, which in
    production comes from camera calibration (homography H, ADR-010
    INV-CS-01). The Task 10 configuration model contains no
    CameraCalibration record, so an IMAGE_NORMALIZED point against a
    camera that declares venue geometry (zones, tables, or venue-scoped
    ROIs) raises ``VenuePointRequiredError`` instead of silently
    reporting OUTSIDE. BLOCKER: CameraCalibration must exist before
    live IMAGE_NORMALIZED tracks can be zone/table-classified.
  - Privacy precedence (INV-GEO-07): PRIVACY > EXCLUDED > standard
    zones/tables. A point inside a privacy ROI is reported PRIVACY
    without consulting exclusion ROIs, zones, or tables; a point inside
    an exclusion ROI is reported EXCLUDED — the observation is still
    produced with full provenance, never deleted (section 5/6
    requirement), and never given a zone/table identity.
  - Tenant/venue isolation: the engine evaluates ONLY against the
    configuration object it is given; it cannot reach another tenant's
    or venue's geometry. The tenant/venue authorization boundary lives
    in the repository/service layer (tenant-scoped lookups + RLS) and
    is deliberately NOT duplicated in this pure layer (section 10/11).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.app.intelligence.geometry import (
    GeometryError,
    PointLocation,
    classify_point_in_polygon,
    validate_coordinate,
    validate_polygon,
)
from backend.app.intelligence.spatial.exceptions import (
    BoundaryPolicyUndefinedError,
    CameraNotInConfigurationError,
    ConfigurationNotPublishedError,
    InvalidSpatialInputError,
    ReferenceIntegrityError,
    VenuePointRequiredError,
)
from contracts.common import CameraId
from contracts.configuration import (
    CameraProfileModel,
    ConfigurationStatus,
    ConfigurationVersionModel,
    ExclusionROIModel,
    PrivacyROIModel,
    TableModel,
    ZoneModel,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryType
from contracts.spatial import (
    SPATIAL_ENGINE_VERSION,
    SpatialObservation,
    SpatialPointModel,
    SpatialStatus,
)
from contracts.vision import TrackObservation

__all__ = [
    "SpatialEvaluationInput",
    "SpatialEvaluationResult",
    "TableMembership",
    "ZoneMembership",
    "evaluate_spatial",
]


@dataclass(frozen=True, slots=True)
class ZoneMembership:
    """Classification of one camera-declared zone against the point.

    ``location`` is the Step 2 geometry outcome (INSIDE/OUTSIDE/
    BOUNDARY). The engine never maps BOUNDARY to another value without
    an explicit policy.
    """

    zone_profile_id: str
    location: PointLocation


@dataclass(frozen=True, slots=True)
class TableMembership:
    """Classification of one version-owned table against the point (Step 5).

    Tables are venue-scoped geometry in the pinned configuration
    version (Task 10 defines no camera→table binding), so every table
    in the version is classified. ``location`` is the Step 2 geometry
    outcome; BOUNDARY follows the same recorded blocker policy as
    zones.
    """

    table_profile_id: str
    location: PointLocation


@dataclass(frozen=True, slots=True)
class SpatialEvaluationInput:
    """Pure-engine inputs: pinned configuration + track provenance + point.

    ``configuration`` MUST be the exact immutable PUBLISHED version the
    session pins to — never "the latest" configuration. ``zone_priority``
    and ``table_priority`` are explicit, optional precedence lists of
    profile ids (highest first) used only to resolve overlapping
    zone/table matches; when empty the engine reports overlapping
    matches as AMBIGUOUS (Task 10 defines no zone or table precedence).
    """

    configuration: ConfigurationVersionModel
    track: TrackObservation
    camera_id: CameraId
    point: SpatialPointModel
    zone_priority: tuple[str, ...] = ()
    table_priority: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Typed error contract: the same ``InvalidSpatialInputError`` the
        # engine raises for malformed inputs, so callers catch ONE type.
        if self.configuration is None:
            raise InvalidSpatialInputError("configuration (pinned published version) is required")
        if self.track is None:
            raise InvalidSpatialInputError("track (canonical TrackObservation) is required")
        if self.camera_id is None:
            raise InvalidSpatialInputError("camera_id (physical CameraId) is required")
        if self.point is None:
            raise InvalidSpatialInputError("point (canonical SpatialPointModel) is required")


@dataclass(frozen=True, slots=True)
class SpatialEvaluationResult:
    """Deterministic result of one spatial evaluation.

    ``observation`` is the canonical ``SpatialObservation``. The
    additional members carry audit provenance that the observation
    contract deliberately does not duplicate: the classification of
    every camera-declared zone, every version-owned table, the exact
    ROI profile that matched (when any), and the configuration-declared
    zone/table relationship of the matched zone. ``zone_memberships``
    is empty when no zone membership was evaluated (EXCLUDED/PRIVACY
    outcome, or a camera that declares no zones); ``table_memberships``
    is empty for EXCLUDED/PRIVACY outcomes (policy-intercepted points
    are never table-mapped).
    """

    observation: SpatialObservation
    zone_memberships: tuple[ZoneMembership, ...] = ()
    table_memberships: tuple[TableMembership, ...] = ()
    matched_exclusion_roi_profile_id: str | None = None
    matched_privacy_roi_profile_id: str | None = None
    # Zone/table relationship (Step 5): the contained tables declared by
    # the matched zone (``ZoneModel.contained_tables``), sorted. Empty
    # unless a single zone matched.
    matched_zone_contained_tables: tuple[str, ...] = ()


# =============================================================================
# Reference resolution (deterministic: sorted by profile_id, integrity-checked)
# =============================================================================


def _resolve_camera(
    configuration: ConfigurationVersionModel, camera_id: CameraId
) -> CameraProfileModel:
    """Return the camera profile matching the physical ``camera_id``.

    Camera isolation (section 2): a track is never evaluated against
    another camera's geometry — the named camera must be part of the
    pinned configuration version.
    """
    for camera in configuration.cameras:
        if camera.camera_id == camera_id:
            return camera
    raise CameraNotInConfigurationError(
        f"camera {camera_id} is not configured in the pinned configuration "
        f"version {configuration.configuration_version_id}"
    )


def _camera_declared_zones(
    configuration: ConfigurationVersionModel, camera: CameraProfileModel
) -> tuple[ZoneModel, ...]:
    """Camera-declared zones in deterministic (profile_id) order."""
    by_id = {zone.profile_id: zone for zone in configuration.zones}
    zones: list[ZoneModel] = []
    for profile_id in sorted(camera.detection_zones):
        zone = by_id.get(profile_id)
        if zone is None:
            raise ReferenceIntegrityError(
                f"camera '{camera.profile_id}' declares zone '{profile_id}' which is "
                f"not present in configuration version "
                f"{configuration.configuration_version_id}"
            )
        _assert_zone_contract(zone, camera)
        zones.append(zone)
    return tuple(zones)


def _camera_declared_privacy_rois(
    configuration: ConfigurationVersionModel, camera: CameraProfileModel
) -> tuple[PrivacyROIModel, ...]:
    """Camera-declared privacy ROIs in deterministic (profile_id) order."""
    by_id = {roi.profile_id: roi for roi in configuration.privacy_rois}
    rois: list[PrivacyROIModel] = []
    for profile_id in sorted(camera.privacy_rois):
        roi = by_id.get(profile_id)
        if roi is None:
            raise ReferenceIntegrityError(
                f"camera '{camera.profile_id}' declares privacy ROI '{profile_id}' "
                f"which is not present in configuration version "
                f"{configuration.configuration_version_id}"
            )
        _assert_roi_contract(roi, camera)
        rois.append(roi)
    return tuple(rois)


def _camera_declared_exclusion_rois(
    configuration: ConfigurationVersionModel, camera: CameraProfileModel
) -> tuple[ExclusionROIModel, ...]:
    """Camera-declared exclusion ROIs in deterministic (profile_id) order."""
    by_id = {roi.profile_id: roi for roi in configuration.exclusion_rois}
    rois: list[ExclusionROIModel] = []
    for profile_id in sorted(camera.exclusion_rois):
        roi = by_id.get(profile_id)
        if roi is None:
            raise ReferenceIntegrityError(
                f"camera '{camera.profile_id}' declares exclusion ROI '{profile_id}' "
                f"which is not present in configuration version "
                f"{configuration.configuration_version_id}"
            )
        _assert_roi_contract(roi, camera)
        rois.append(roi)
    return tuple(rois)


def _assert_zone_contract(zone: ZoneModel, camera: CameraProfileModel) -> None:
    """Re-assert the entity geometry contract for zones (POLYGON, VENUE_LOCAL)."""
    geometry = zone.geometry
    if (
        geometry.geometry_type != GeometryType.POLYGON
        or geometry.coordinate_space != CoordinateSpace.VENUE_LOCAL
    ):
        raise ReferenceIntegrityError(
            f"zone '{zone.profile_id}' declared by camera '{camera.profile_id}' uses "
            f"{geometry.geometry_type.value} in {geometry.coordinate_space.value}; "
            "zones must be POLYGON in VENUE_LOCAL"
        )


def _assert_roi_contract(
    roi: PrivacyROIModel | ExclusionROIModel, camera: CameraProfileModel
) -> None:
    """Re-assert the entity geometry contract for ROIs (POLYGON, either space)."""
    geometry = roi.geometry
    if geometry.geometry_type != GeometryType.POLYGON or geometry.coordinate_space not in (
        CoordinateSpace.IMAGE_NORMALIZED,
        CoordinateSpace.VENUE_LOCAL,
    ):
        raise ReferenceIntegrityError(
            f"{'privacy' if isinstance(roi, PrivacyROIModel) else 'exclusion'} ROI "
            f"'{roi.profile_id}' declared by camera '{camera.profile_id}' uses "
            f"{geometry.geometry_type.value} in {geometry.coordinate_space.value}; "
            "ROIs must be POLYGON in IMAGE_NORMALIZED or VENUE_LOCAL"
        )


def _validate_zone_priority(
    zone_priority: tuple[str, ...],
    camera: CameraProfileModel,
    configuration: ConfigurationVersionModel,
) -> None:
    """Validate the explicit overlap-precedence list (no invented semantics)."""
    if not zone_priority:
        return
    if len(zone_priority) != len(set(zone_priority)):
        raise InvalidSpatialInputError("zone_priority must not contain duplicate profile ids")
    declared = set(camera.detection_zones)
    for profile_id in zone_priority:
        if profile_id not in declared:
            raise ReferenceIntegrityError(
                f"zone_priority references zone '{profile_id}' which camera "
                f"'{camera.profile_id}' does not declare in configuration version "
                f"{configuration.configuration_version_id}"
            )


def _version_tables(configuration: ConfigurationVersionModel) -> tuple[TableModel, ...]:
    """Version-owned tables in deterministic (profile_id) order.

    Task 10 defines no camera→table binding: tables are venue-scoped
    geometry belonging to the configuration version, so every table in
    the pinned version is a candidate (camera isolation is preserved
    through the pinned version and the VENUE_LOCAL point requirement).
    """
    tables = sorted(configuration.tables, key=lambda table: table.profile_id)
    for table in tables:
        _assert_table_contract(table)
    return tuple(tables)


def _assert_table_contract(table: TableModel) -> None:
    """Re-assert the entity geometry contract for tables (POLYGON, VENUE_LOCAL)."""
    geometry = table.geometry
    if (
        geometry.geometry_type != GeometryType.POLYGON
        or geometry.coordinate_space != CoordinateSpace.VENUE_LOCAL
    ):
        raise ReferenceIntegrityError(
            f"table '{table.profile_id}' uses {geometry.geometry_type.value} in "
            f"{geometry.coordinate_space.value}; tables must be POLYGON in VENUE_LOCAL"
        )


def _validate_table_priority(
    table_priority: tuple[str, ...], configuration: ConfigurationVersionModel
) -> None:
    """Validate the explicit table overlap-precedence list (no invented semantics)."""
    if not table_priority:
        return
    if len(table_priority) != len(set(table_priority)):
        raise InvalidSpatialInputError("table_priority must not contain duplicate profile ids")
    declared = {table.profile_id for table in configuration.tables}
    for profile_id in table_priority:
        if profile_id not in declared:
            raise ReferenceIntegrityError(
                f"table_priority references table '{profile_id}' which is not present "
                f"in configuration version {configuration.configuration_version_id}"
            )


def _validate_contained_tables_references(
    configuration: ConfigurationVersionModel,
) -> None:
    """Eagerly validate every zone's ``contained_tables`` references.

    A dangling contained-tables reference is a configuration integrity
    failure even when the zone never matches the evaluated point — it
    fails loudly for every evaluation, matching the engine's defensive
    reference-integrity style (camera-declared zones/ROIs are also
    validated upfront). Bounded: one pass over the version's zones and
    their contained lists.
    """
    table_ids = {table.profile_id for table in configuration.tables}
    for zone in configuration.zones:
        for ref in zone.contained_tables:
            if ref not in table_ids:
                raise ReferenceIntegrityError(
                    f"zone '{zone.profile_id}' declares contained table '{ref}' which is "
                    f"not present in configuration version "
                    f"{configuration.configuration_version_id}"
                )


def _zone_contained_tables(
    configuration: ConfigurationVersionModel, zone_profile_id: str | None
) -> tuple[str, ...]:
    """The configuration-declared tables contained in the matched zone (Step 5).

    The relationship is ``ZoneModel.contained_tables`` — config-declared,
    never derived geometrically and never assumed to be "table == zone".
    References are validated eagerly by
    ``_validate_contained_tables_references`` for every evaluation, so
    this lookup only resolves and sorts deterministically.
    """
    if zone_profile_id is None:
        return ()
    zone = next((z for z in configuration.zones if z.profile_id == zone_profile_id), None)
    if zone is None:
        raise ReferenceIntegrityError(
            f"zone '{zone_profile_id}' is not present in configuration version "
            f"{configuration.configuration_version_id}"
        )
    return tuple(sorted(zone.contained_tables))


# =============================================================================
# Classification
# =============================================================================


def _classify(point: SpatialPointModel, geometry: GeometryModel, *, owner: str) -> PointLocation:
    """Tri-state classification of ``point`` against one configuration polygon.

    Reuses the Step 2 geometry layer (``validate_polygon`` +
    ``classify_point_in_polygon``) — the point-in-polygon logic exists
    exactly once. Geometry failures are wrapped into the spatial
    engine's typed taxonomy; input is never repaired or reordered.
    """
    try:
        ring = validate_polygon(geometry)
        return classify_point_in_polygon((point.x, point.y), ring)
    except GeometryError as exc:
        raise ReferenceIntegrityError(
            f"geometry of {owner} failed validation: {exc.message}", cause=exc
        ) from exc


def _evaluate_roi_pass(
    point: SpatialPointModel,
    rois: Sequence[PrivacyROIModel | ExclusionROIModel],
    *,
    kind: str,
) -> str | None:
    """Return the first (sorted) ROI profile id containing ``point``.

    Only ROIs in the point's own coordinate space are evaluated
    (cross-space operations are forbidden). A BOUNDARY classification
    aborts with the recorded boundary-policy blocker — it is never
    silently promoted to PRIVACY/EXCLUDED or demoted to OUTSIDE.
    """
    for roi in rois:
        if roi.geometry.coordinate_space != point.coordinate_space:
            continue
        location = _classify(point, roi.geometry, owner=f"{kind} '{roi.profile_id}'")
        if location is PointLocation.BOUNDARY:
            raise BoundaryPolicyUndefinedError(
                f"point {(point.x, point.y)} lies on the boundary of {kind} "
                f"'{roi.profile_id}' and Task 10 defines no boundary policy; "
                "BOUNDARY is never silently converted"
            )
        if location is PointLocation.INSIDE:
            return roi.profile_id
    return None


def _evaluate_zone_membership(
    point: SpatialPointModel,
    zones: tuple[ZoneModel, ...],
    *,
    zone_priority: tuple[str, ...],
) -> tuple[SpatialStatus, str | None, tuple[ZoneMembership, ...]]:
    """Classify the point against every camera-declared zone.

    Deterministic resolution (section 4/7): zero matches -> OUTSIDE,
    one match -> INSIDE, several matches -> the explicit ``zone_priority``
    picks the primary zone (INSIDE) or the result is AMBIGUOUS. A
    BOUNDARY classification aborts with the recorded blocker.
    """
    memberships: list[ZoneMembership] = []
    for zone in zones:
        location = _classify(point, zone.geometry, owner=f"zone '{zone.profile_id}'")
        if location is PointLocation.BOUNDARY:
            raise BoundaryPolicyUndefinedError(
                f"point {(point.x, point.y)} lies on the boundary of zone "
                f"'{zone.profile_id}' and Task 10 defines no boundary policy; "
                "BOUNDARY is never silently converted to INSIDE or OUTSIDE"
            )
        memberships.append(ZoneMembership(zone_profile_id=zone.profile_id, location=location))

    matched = [m for m in memberships if m.location is PointLocation.INSIDE]
    all_memberships = tuple(memberships)
    if len(matched) == 1:
        return SpatialStatus.INSIDE, matched[0].zone_profile_id, all_memberships
    if len(matched) > 1:
        if zone_priority:
            primary = next(
                (pid for pid in zone_priority if pid in {m.zone_profile_id for m in matched}),
                None,
            )
            if primary is not None:
                return SpatialStatus.INSIDE, primary, all_memberships
        # No valid precedence: report the ambiguity instead of inventing
        # business semantics (section 4/7).
        return SpatialStatus.AMBIGUOUS, None, all_memberships
    return SpatialStatus.OUTSIDE, None, all_memberships


def _evaluate_table_membership(
    point: SpatialPointModel,
    tables: tuple[TableModel, ...],
    *,
    table_priority: tuple[str, ...],
) -> tuple[str | None, tuple[TableMembership, ...], bool]:
    """Classify the point against every version-owned table (Step 5).

    Deterministic resolution (section 4/7): zero matches -> no table,
    one match -> that table, several matches -> the explicit
    ``table_priority`` picks the primary table or the result is
    ambiguous. A BOUNDARY classification aborts with the recorded
    blocker (same policy as zones — never silently converted).

    Returns ``(matched_table_profile_id, all_memberships, ambiguous)``.
    """
    memberships: list[TableMembership] = []
    for table in tables:
        location = _classify(point, table.geometry, owner=f"table '{table.profile_id}'")
        if location is PointLocation.BOUNDARY:
            raise BoundaryPolicyUndefinedError(
                f"point {(point.x, point.y)} lies on the boundary of table "
                f"'{table.profile_id}' and Task 10 defines no boundary policy; "
                "BOUNDARY is never silently converted to INSIDE or OUTSIDE"
            )
        memberships.append(TableMembership(table_profile_id=table.profile_id, location=location))

    matched = [m for m in memberships if m.location is PointLocation.INSIDE]
    all_memberships = tuple(memberships)
    if len(matched) > 1:
        if table_priority:
            primary = next(
                (pid for pid in table_priority if pid in {m.table_profile_id for m in matched}),
                None,
            )
            if primary is not None:
                return primary, all_memberships, False
        # No valid precedence: report the ambiguity instead of inventing
        # business semantics (section 4/7).
        return None, all_memberships, True
    if len(matched) == 1:
        return matched[0].table_profile_id, all_memberships, False
    return None, all_memberships, False


def _resolve_combined(
    zone_status: SpatialStatus,
    zone_profile_id: str | None,
    table_profile_id: str | None,
    table_ambiguous: bool,
    *,
    configuration: ConfigurationVersionModel,
) -> tuple[SpatialStatus, str | None, str | None, tuple[str, ...]]:
    """Combine zone membership and table mapping into one status (Step 5).

    AMBIGUOUS is the single combined ambiguity state: overlapping zones
    OR overlapping tables (never silently picking the first match). An
    AMBIGUOUS observation carries no zone/table identity, per the
    ``SpatialObservation`` contract. A point at a table with no
    matching zone is OUTSIDE with the table identity retained (a table
    may have no zone relationship — configuration semantics preserved).
    """
    if zone_status is SpatialStatus.AMBIGUOUS or table_ambiguous:
        return SpatialStatus.AMBIGUOUS, None, None, ()
    if zone_status is SpatialStatus.INSIDE:
        contained = _zone_contained_tables(configuration, zone_profile_id)
        return SpatialStatus.INSIDE, zone_profile_id, table_profile_id, contained
    return SpatialStatus.OUTSIDE, None, table_profile_id, ()


def _declares_venue_geometry(
    zones: tuple[ZoneModel, ...],
    privacy_rois: tuple[PrivacyROIModel, ...],
    exclusion_rois: tuple[ExclusionROIModel, ...],
    tables: tuple[TableModel, ...] = (),
) -> bool:
    """True when the camera declares geometry that needs a VENUE_LOCAL point.

    Tables count as venue geometry (Step 5): they are VENUE_LOCAL
    polygons in the pinned version, so an IMAGE_NORMALIZED point cannot
    be table-mapped without the camera→venue projection.
    """
    if zones or tables:
        return True
    return any(
        roi.geometry.coordinate_space is CoordinateSpace.VENUE_LOCAL for roi in privacy_rois
    ) or any(roi.geometry.coordinate_space is CoordinateSpace.VENUE_LOCAL for roi in exclusion_rois)


def _build_observation(
    *,
    configuration: ConfigurationVersionModel,
    track: TrackObservation,
    camera_id: CameraId,
    point: SpatialPointModel,
    status: SpatialStatus,
    zone_profile_id: str | None,
    table_profile_id: str | None = None,
) -> SpatialObservation:
    """Map the deterministic outcome into the canonical observation (section 8/15)."""
    return SpatialObservation(
        session_id=track.session_id,
        track_id=track.track_id,
        frame_id=track.frame_id,
        event_time=track.event_time,
        camera_id=camera_id,
        configuration_version_id=configuration.configuration_version_id,
        spatial_point=point,
        status=status,
        zone_profile_id=zone_profile_id,
        table_profile_id=table_profile_id,
        engine_version=SPATIAL_ENGINE_VERSION,
    )


# =============================================================================
# Public API
# =============================================================================


def evaluate_spatial(evaluation: SpatialEvaluationInput) -> SpatialEvaluationResult:
    """Evaluate one canonical spatial point against the pinned configuration.

    Pure and deterministic: no I/O, no current time, no randomness, no
    fallback to the latest configuration. Raises the typed
    ``SpatialEvaluationError`` taxonomy on any failure — a failure is
    never encoded as OUTSIDE/EXCLUDED.
    """
    if not isinstance(evaluation, SpatialEvaluationInput):
        raise InvalidSpatialInputError(
            f"evaluation must be a SpatialEvaluationInput, got {type(evaluation).__name__}"
        )

    configuration = evaluation.configuration
    track = evaluation.track
    camera_id = evaluation.camera_id
    point = evaluation.point

    # --- Input boundary (defensive re-assertion; never repaired) ---
    if not isinstance(configuration, ConfigurationVersionModel):
        raise InvalidSpatialInputError("configuration (pinned published version) is required")
    if not isinstance(track, TrackObservation):
        raise InvalidSpatialInputError("track (canonical TrackObservation) is required")
    if not isinstance(point, SpatialPointModel):
        raise InvalidSpatialInputError("point (canonical SpatialPointModel) is required")
    if not isinstance(camera_id, UUID):
        raise InvalidSpatialInputError("camera_id (physical CameraId) is required")

    # --- Point validation BEFORE configuration traversal, so a malformed
    # point is never masked by (or confused with) a config problem ---
    try:
        validate_coordinate(point.x, point.y, coordinate_space=point.coordinate_space)
    except GeometryError as exc:
        raise InvalidSpatialInputError(
            f"spatial point failed validation: {exc.message}", cause=exc
        ) from exc

    # --- Configuration version provenance (section 9) ---
    if configuration.status is not ConfigurationStatus.PUBLISHED:
        raise ConfigurationNotPublishedError(
            f"configuration version {configuration.configuration_version_id} is "
            f"'{configuration.status.value}'; spatial evaluation requires the immutable "
            "PUBLISHED version pinned by the session (never the latest)"
        )

    camera = _resolve_camera(configuration, camera_id)

    # --- Camera-scoped candidate sets (section 2) ---
    _validate_zone_priority(evaluation.zone_priority, camera, configuration)
    _validate_table_priority(evaluation.table_priority, configuration)
    _validate_contained_tables_references(configuration)
    zones = _camera_declared_zones(configuration, camera)
    tables = _version_tables(configuration)
    privacy_rois = _camera_declared_privacy_rois(configuration, camera)
    exclusion_rois = _camera_declared_exclusion_rois(configuration, camera)

    # --- Privacy (supreme precedence, INV-GEO-07) ---
    matched_privacy = _evaluate_roi_pass(point, privacy_rois, kind="privacy ROI")
    if matched_privacy is not None:
        observation = _build_observation(
            configuration=configuration,
            track=track,
            camera_id=camera_id,
            point=point,
            status=SpatialStatus.PRIVACY,
            zone_profile_id=None,
        )
        return SpatialEvaluationResult(
            observation=observation,
            matched_privacy_roi_profile_id=matched_privacy,
        )

    # --- Exclusion (section 5: evaluated separately from zones) ---
    matched_exclusion = _evaluate_roi_pass(point, exclusion_rois, kind="exclusion ROI")
    if matched_exclusion is not None:
        observation = _build_observation(
            configuration=configuration,
            track=track,
            camera_id=camera_id,
            point=point,
            status=SpatialStatus.EXCLUDED,
            zone_profile_id=None,
        )
        return SpatialEvaluationResult(
            observation=observation,
            matched_exclusion_roi_profile_id=matched_exclusion,
        )

    # --- Projection blocker: venue geometry is undeclarable for an
    # IMAGE_NORMALIZED point (no CameraCalibration in Task 10 config) ---
    if point.coordinate_space is CoordinateSpace.IMAGE_NORMALIZED and _declares_venue_geometry(
        zones, privacy_rois, exclusion_rois, tables
    ):
        raise VenuePointRequiredError(
            f"point {(point.x, point.y)} is IMAGE_NORMALIZED but camera "
            f"'{camera.profile_id}' declares venue geometry (zones, tables, or "
            "venue-scoped ROIs); zone/table membership requires a VENUE_LOCAL point "
            "and the camera→venue projection (ADR-010 INV-CS-01 CameraCalibration) "
            "is not part of the Task 10 configuration model — recorded blocker, "
            "no silent OUTSIDE"
        )

    # --- Zone membership (section 3/4/7) ---
    zone_status, zone_profile_id, zone_memberships = _evaluate_zone_membership(
        point, zones, zone_priority=evaluation.zone_priority
    )

    # --- Table mapping (Step 5, section 2/3/4) ---
    table_profile_id, table_memberships, table_ambiguous = _evaluate_table_membership(
        point, tables, table_priority=evaluation.table_priority
    )

    # --- Combined ambiguity resolution (Step 5, section 7/8) ---
    status, primary_zone_id, primary_table_id, contained_tables = _resolve_combined(
        zone_status,
        zone_profile_id,
        table_profile_id,
        table_ambiguous,
        configuration=configuration,
    )
    observation = _build_observation(
        configuration=configuration,
        track=track,
        camera_id=camera_id,
        point=point,
        status=status,
        zone_profile_id=primary_zone_id,
        table_profile_id=primary_table_id,
    )
    return SpatialEvaluationResult(
        observation=observation,
        zone_memberships=zone_memberships,
        table_memberships=table_memberships,
        matched_zone_contained_tables=contained_tables,
    )
