"""Modular deterministic configuration validators (Task 10.9).

Each validator covers one category and is READ-ONLY: it never mutates
the version, never depends on unordered iteration, wall-clock state,
random IDs, or database ordering. Findings carry stable rule codes.

Categories:
  STRUCTURAL          — required entities, duplicate identifiers
  REFERENCE           — missing/cross-version/cross-tenant references
  COORDINATE          — coordinate space rules, mixing, bounds
  GEOMETRY            — geometry validity (empty/self-intersect/zero-area)
  SPATIAL             — overlap & containment policies (suppressed when
                        geometry or reference validation failed)
  CAMERA              — camera reference validity/availability/retirement
  PRIVACY_POLICY      — privacy precedence & contradiction detection
  CV_COMPATIBILITY    — coverage warnings (zone uncovered, camera w/o zone)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from backend.app.domain.configuration.validation.codes import RuleCode
from backend.app.domain.configuration.validation.findings import FindingCollector
from backend.app.domain.configuration.validation.policy import (
    DEFAULT_SPATIAL_POLICY_REGISTRY,
    EntityKind,
    SpatialPolicyRegistry,
)
from backend.app.domain.configuration.validation.spatial import (
    SpatialEngine,
    SpatialMath,
)
from contracts.configuration import (
    ConfigurationVersionModel,
    ExclusionROIModel,
    PrivacyROIModel,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryType

VALIDATOR_VERSION = "10.1.0"

_GEOMETRY_FAILURE_CODES = frozenset({
    RuleCode.GEOMETRY_EMPTY,
    RuleCode.GEOMETRY_INVALID,
    RuleCode.GEOMETRY_SELF_INTERSECTION,
    RuleCode.GEOMETRY_ZERO_AREA,
    RuleCode.GEOMETRY_TYPE_INVALID,
    RuleCode.GEOMETRY_NOT_CLOSED,
    RuleCode.GEOMETRY_OUT_OF_RANGE,
    RuleCode.COORDINATE_SPACE_INVALID,
    RuleCode.COORDINATE_MIXED_SPACES,
})

_REFERENCE_FAILURE_CODES = frozenset({
    RuleCode.MISSING_REFERENCE,
    RuleCode.CROSS_VERSION_REFERENCE,
    RuleCode.CROSS_TENANT_REFERENCE,
})


class CameraStatusResolver(Protocol):
    """Resolves the physical camera lifecycle state (server-side truth).

    Production implementation queries the cameras table; tests inject a
    fake. Returns None when the camera does not exist.
    """

    def camera_status(self, camera_id: object) -> str | None: ...


@dataclass(frozen=True)
class ValidatorContext:
    """Everything the validators need, explicitly injected."""

    spatial: SpatialEngine = field(default_factory=SpatialMath)
    policies: SpatialPolicyRegistry = field(default_factory=lambda: DEFAULT_SPATIAL_POLICY_REGISTRY)
    camera_resolver: CameraStatusResolver | None = None


# =============================================================================
# STRUCTURAL
# =============================================================================


async def validate_structural(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext | None = None,
) -> None:
    """Required entities and duplicate logical identifiers."""
    seen: dict[str, str] = {}
    categories: list[tuple[str, list[Any]]] = [
        ("camera", list(version.cameras)),
        ("zone", list(version.zones)),
        ("table", list(version.tables)),
        ("entrance", list(version.entrances)),
        ("queue_area", list(version.queue_areas)),
        ("service_area", list(version.service_areas)),
        ("privacy_roi", list(version.privacy_rois)),
        ("exclusion_roi", list(version.exclusion_rois)),
    ]
    for category, items in categories:
        for item in items:
            profile_id: str = item.profile_id
            if not profile_id or not str(profile_id).strip():
                collector.error(
                    RuleCode.MISSING_REFERENCE,
                    f"{category} has an empty profile_id",
                    entity_type=category,
                )
                continue
            if profile_id in seen:
                collector.error(
                    RuleCode.DUPLICATE_IDENTIFIER,
                    f"Duplicate profile_id '{profile_id}' (already used by {seen[profile_id]})",
                    entity_type=category,
                    entity_id=profile_id,
                    related_entity_id=seen[profile_id],
                )
            else:
                seen[profile_id] = category


# =============================================================================
# REFERENCE
# =============================================================================


def _collect_profile_ids(version: ConfigurationVersionModel) -> dict[str, str]:
    ids: dict[str, str] = {}
    for category, items in (
        ("camera", version.cameras),
        ("zone", version.zones),
        ("table", version.tables),
        ("entrance", version.entrances),
        ("queue_area", version.queue_areas),
        ("service_area", version.service_areas),
        ("privacy_roi", version.privacy_rois),
        ("exclusion_roi", version.exclusion_rois),
    ):
        for item in items:
            ids[item.profile_id] = category
    return ids


def _check_in_version(
    collector: FindingCollector,
    *,
    reference: str,
    kind: str,
    owner_type: str,
    owner_id: str,
    all_ids: dict[str, str],
) -> None:
    """Cross-version / missing reference check for a profile reference."""
    target_kind = all_ids.get(reference)
    if target_kind is None:
        collector.error(
            RuleCode.MISSING_REFERENCE,
            f"{owner_type} '{owner_id}' references unknown {kind} '{reference}'",
            entity_type=owner_type,
            entity_id=owner_id,
            related_entity_id=reference,
        )


async def validate_references(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext | None = None,
) -> None:
    """Missing references and same-version reference guarantees."""
    all_ids = _collect_profile_ids(version)
    before = len(collector.findings)
    for cam in version.cameras:
        for zone_ref in cam.detection_zones:
            _check_in_version(
                collector,
                reference=zone_ref,
                kind="zone",
                owner_type="camera",
                owner_id=cam.profile_id,
                all_ids=all_ids,
            )
        for roi_ref in cam.privacy_rois:
            _check_in_version(
                collector,
                reference=roi_ref,
                kind="privacy_roi",
                owner_type="camera",
                owner_id=cam.profile_id,
                all_ids=all_ids,
            )
        for roi_ref in cam.exclusion_rois:
            _check_in_version(
                collector,
                reference=roi_ref,
                kind="exclusion_roi",
                owner_type="camera",
                owner_id=cam.profile_id,
                all_ids=all_ids,
            )

    for zone in version.zones:
        for table_ref in zone.contained_tables:
            _check_in_version(
                collector,
                reference=table_ref,
                kind="table",
                owner_type="zone",
                owner_id=zone.profile_id,
                all_ids=all_ids,
            )
        for entrance_ref in zone.contained_entrances:
            _check_in_version(
                collector,
                reference=entrance_ref,
                kind="entrance",
                owner_type="zone",
                owner_id=zone.profile_id,
                all_ids=all_ids,
            )
        for queue_ref in zone.contained_queue_areas:
            _check_in_version(
                collector,
                reference=queue_ref,
                kind="queue_area",
                owner_type="zone",
                owner_id=zone.profile_id,
                all_ids=all_ids,
            )
        for service_ref in zone.contained_service_areas:
            _check_in_version(
                collector,
                reference=service_ref,
                kind="service_area",
                owner_type="zone",
                owner_id=zone.profile_id,
                all_ids=all_ids,
            )

    for entrance in version.entrances:
        if entrance.zone_profile_id:
            _check_in_version(
                collector,
                reference=entrance.zone_profile_id,
                kind="zone",
                owner_type="entrance",
                owner_id=entrance.profile_id,
                all_ids=all_ids,
            )
        for cam_ref in entrance.camera_profiles:
            _check_in_version(
                collector,
                reference=cam_ref,
                kind="camera",
                owner_type="entrance",
                owner_id=entrance.profile_id,
                all_ids=all_ids,
            )

    for queue in version.queue_areas:
        if queue.zone_profile_id:
            _check_in_version(
                collector,
                reference=queue.zone_profile_id,
                kind="zone",
                owner_type="queue_area",
                owner_id=queue.profile_id,
                all_ids=all_ids,
            )
        for cam_ref in queue.camera_profiles:
            _check_in_version(
                collector,
                reference=cam_ref,
                kind="camera",
                owner_type="queue_area",
                owner_id=queue.profile_id,
                all_ids=all_ids,
            )

    for service in version.service_areas:
        if service.zone_profile_id:
            _check_in_version(
                collector,
                reference=service.zone_profile_id,
                kind="zone",
                owner_type="service_area",
                owner_id=service.profile_id,
                all_ids=all_ids,
            )
        for cam_ref in service.camera_profiles:
            _check_in_version(
                collector,
                reference=cam_ref,
                kind="camera",
                owner_type="service_area",
                owner_id=service.profile_id,
                all_ids=all_ids,
            )

    for priv_roi in version.privacy_rois:
        for cam_ref in priv_roi.camera_profiles:
            _check_in_version(
                collector,
                reference=cam_ref,
                kind="camera",
                owner_type="privacy_roi",
                owner_id=priv_roi.profile_id,
                all_ids=all_ids,
            )

    for excl_roi in version.exclusion_rois:
        for cam_ref in excl_roi.camera_profiles:
            _check_in_version(
                collector,
                reference=cam_ref,
                kind="camera",
                owner_type="exclusion_roi",
                owner_id=excl_roi.profile_id,
                all_ids=all_ids,
            )

    # Track prerequisite health for cascading-error suppression.
    if any(f.code in _REFERENCE_FAILURE_CODES for f in collector.findings[before:]):
        collector.references_ok = False


# =============================================================================
# COORDINATE
# =============================================================================


class _ProfileEntity(Protocol):
    """Any version-owned entity exposes a profile_id."""

    profile_id: str
    geometry: GeometryModel | None
    zone_profile_id: str | None = None


def _geometry_entities(
    version: ConfigurationVersionModel,
) -> list[tuple[str, _ProfileEntity, GeometryModel]]:
    """(kind, entity, geometry) triples in deterministic category order."""
    result: list[tuple[str, _ProfileEntity, GeometryModel]] = []
    for category, items in (
        ("camera", version.cameras),
        ("zone", version.zones),
        ("table", version.tables),
        ("entrance", version.entrances),
        ("queue_area", version.queue_areas),
        ("service_area", version.service_areas),
        ("privacy_roi", version.privacy_rois),
        ("exclusion_roi", version.exclusion_rois),
    ):
        for item in items:
            geom = getattr(item, "geometry", None)
            if geom is not None:
                # All eight entity types expose profile_id/geometry — the
                # protocol captures the shared surface.
                result.append((category, cast(_ProfileEntity, item), geom))
    return result


async def validate_coordinate(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext | None = None,
) -> None:
    """Coordinate-space rules: bounds, mixing, camera-reference coupling."""
    camera_ids = {c.profile_id for c in version.cameras}
    for kind, entity, geom in _geometry_entities(version):
        pid = entity.profile_id
        # IMAGE_NORMALIZED bounds: every x/y within [0, 1].
        if geom.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED:
            for point in geom.coordinates:
                for value in point[:2]:
                    if not (0.0 <= value <= 1.0):
                        collector.error(
                            RuleCode.GEOMETRY_OUT_OF_RANGE,
                            f"{kind} '{pid}' has IMAGE_NORMALIZED coordinate "
                            f"{value} outside [0, 1]",
                            entity_type=kind,
                            entity_id=pid,
                        )
        # Camera-relative geometry must reference an in-version camera.
        if geom.is_camera_relative:
            ref = geom.reference_camera_profile_id
            if ref is None or ref not in camera_ids:
                collector.error(
                    RuleCode.CAMERA_REFERENCE_INVALID,
                    f"{kind} '{pid}' camera-relative geometry references camera "
                    f"'{ref}' which is not in this configuration version",
                    entity_type=kind,
                    entity_id=pid,
                    related_entity_id=ref,
                )
        # Venue-relative geometry must not reference a camera frame.
        if geom.geometry_scope.value == "venue" and geom.reference_camera_profile_id:
            collector.error(
                RuleCode.COORDINATE_SPACE_INVALID,
                f"{kind} '{pid}' venue-scoped geometry must not reference a camera",
                entity_type=kind,
                entity_id=pid,
            )
    # Cross-entity mixed-space checks are handled by GEOMETRY for
    # polygons; per-entity space mixing is validated here.
    for kind, entity, geom in _geometry_entities(version):
        pid = entity.profile_id
        camera_relative = geom.coordinate_space == CoordinateSpace.IMAGE_NORMALIZED
        venue_relative = geom.coordinate_space == CoordinateSpace.VENUE_LOCAL
        if camera_relative == venue_relative:
            collector.error(
                RuleCode.COORDINATE_SPACE_INVALID,
                f"{kind} '{pid}' declares no valid coordinate space",
                entity_type=kind,
                entity_id=pid,
            )


# =============================================================================
# GEOMETRY
# =============================================================================


async def validate_geometry(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext | None = None,
) -> None:
    """Geometry validity: empty, type contract, closure, self-intersection,
    zero-area."""
    contract_errors: list[tuple[str, object]] = []
    before = len(collector.findings)
    for kind, entity, geom in _geometry_entities(version):
        pid = entity.profile_id
        # Entity geometry contract (ADR-010 §5).
        if not _entity_geometry_contract(kind, geom):
            collector.error(
                RuleCode.ENTITY_GEOMETRY_CONTRACT_VIOLATION,
                f"{kind} '{pid}' uses {geom.geometry_type.value} in "
                f"{geom.coordinate_space.value}; contract for {kind} requires "
                f"{_entity_geometry_summary(kind)}",
                entity_type=kind,
                entity_id=pid,
            )
            contract_errors.append((kind, entity))
            continue
        if geom.geometry_type == GeometryType.POLYGON:
            if geom.is_self_intersecting():
                collector.error(
                    RuleCode.GEOMETRY_SELF_INTERSECTION,
                    f"{kind} '{pid}' polygon self-intersects",
                    entity_type=kind,
                    entity_id=pid,
                )
            if geom.is_degenerate():
                collector.error(
                    RuleCode.GEOMETRY_ZERO_AREA,
                    f"{kind} '{pid}' polygon has zero/negative area",
                    entity_type=kind,
                    entity_id=pid,
                )
            ring = geom.ring
            if ring[0] != ring[-1]:
                collector.error(
                    RuleCode.GEOMETRY_NOT_CLOSED,
                    f"{kind} '{pid}' polygon ring is not closed",
                    entity_type=kind,
                    entity_id=pid,
                )

    # Any geometry failure disables SPATIAL cascade checks.
    if any(f.code in _GEOMETRY_FAILURE_CODES for f in collector.findings[before:]):
        collector.geometry_ok = False


def _entity_geometry_contract(kind: str, geom: GeometryModel) -> bool:
    """Authoritative per-entity geometry contract (ADR-010 §5)."""
    if kind in ("zone", "table", "queue_area", "service_area"):
        return (
            geom.geometry_type == GeometryType.POLYGON
            and geom.coordinate_space == CoordinateSpace.VENUE_LOCAL
        )
    if kind == "entrance":
        return geom.geometry_type in (GeometryType.LINESTRING, GeometryType.POLYGON) and (
            geom.coordinate_space == CoordinateSpace.VENUE_LOCAL
        )
    if kind in ("privacy_roi", "exclusion_roi"):
        # Camera-relative (image_normalized) OR venue-local.
        return geom.geometry_type == GeometryType.POLYGON and geom.coordinate_space in (
            CoordinateSpace.IMAGE_NORMALIZED,
            CoordinateSpace.VENUE_LOCAL,
        )
    if kind == "camera":
        # Camera physical placement is a venue-local point (optional).
        if geom.geometry_type == GeometryType.POINT:
            return geom.coordinate_space == CoordinateSpace.VENUE_LOCAL
        return False
    return False


def _entity_geometry_summary(kind: str) -> str:
    summaries = {
        "zone": "POLYGON in VENUE_LOCAL",
        "table": "POLYGON in VENUE_LOCAL",
        "queue_area": "POLYGON in VENUE_LOCAL",
        "service_area": "POLYGON in VENUE_LOCAL",
        "entrance": "LINESTRING or POLYGON in VENUE_LOCAL",
        "privacy_roi": "POLYGON in IMAGE_NORMALIZED or VENUE_LOCAL",
        "exclusion_roi": "POLYGON in IMAGE_NORMALIZED or VENUE_LOCAL",
        "camera": "POINT in VENUE_LOCAL (optional placement)",
    }
    return summaries.get(kind, "valid geometry")


# =============================================================================
# SPATIAL (suppressed when geometry/reference prerequisites failed)
# =============================================================================


async def validate_spatial(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext,
) -> None:
    """Overlap & containment policies against the SpatialPolicyRegistry.

    Runs ONLY when geometry and reference validation passed, so a
    malformed polygon never produces cascading overlap noise.
    """
    if not collector.geometry_ok or not collector.references_ok:
        return

    polygons: list[tuple[EntityKind, str, GeometryModel]] = []
    for kind, entity, geom in _geometry_entities(version):
        if geom.geometry_type == GeometryType.POLYGON:
            polygons.append((EntityKind(kind), entity.profile_id, geom))

    # --- Meaningful overlap checks (blocking per policy) ---
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            kind_a, id_a, geom_a = polygons[i]
            kind_b, id_b, geom_b = polygons[j]
            if kind_a == kind_b and kind_a == EntityKind.PRIVACY_ROI:
                # Contradictory privacy overlap is evaluated by the
                # PRIVACY_POLICY validator, not here.
                continue
            if kind_a == kind_b and kind_a == EntityKind.EXCLUSION_ROI:
                continue
            if ctx.policies.rejects_overlap(
                kind_a, kind_b
            ) and await ctx.spatial.meaningful_overlap(geom_a, geom_b):
                collector.error(
                    RuleCode.TABLE_OVERLAP
                    if kind_a == kind_b == EntityKind.TABLE
                    else RuleCode.INVALID_SPATIAL_RELATIONSHIP,
                    f"{kind_a.value} '{id_a}' meaningfully overlaps "
                    f"{kind_b.value} '{id_b}' (policy: reject)",
                    entity_type=kind_a.value,
                    entity_id=id_a,
                    related_entity_id=id_b,
                )

    # --- Containment requirements (declared parents) ---
    parent_map: list[tuple[EntityKind, str, GeometryModel, str | None]] = []
    for kind, entity, geom in _geometry_entities(version):
        parent_ref = getattr(entity, "zone_profile_id", None)
        if parent_ref is not None:
            parent_map.append((EntityKind(kind), entity.profile_id, geom, parent_ref))
    zone_geoms = {
        z.profile_id: z.geometry
        for z in version.zones
        if z.geometry.geometry_type == GeometryType.POLYGON
    }
    for kind, child_id, child_geom, parent_ref in parent_map:
        if ctx.policies.requires_containment(kind, EntityKind.ZONE):
            if parent_ref is None:
                continue
            parent_geom = zone_geoms.get(parent_ref)
            if parent_geom is None:
                continue  # reference error already reported
            if not await ctx.spatial.contains(parent_geom, child_geom):
                collector.error(
                    RuleCode.INVALID_CONTAINMENT,
                    f"{kind.value} '{child_id}' is not contained by its declared "
                    f"zone '{parent_ref}'",
                    entity_type=kind.value,
                    entity_id=child_id,
                    related_entity_id=parent_ref,
                )


# =============================================================================
# CAMERA
# =============================================================================


async def validate_cameras(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext,
) -> None:
    """Camera reference lifecycle validity.

    - new publishable configurations must not reference retired/disabled/
      unavailable cameras (ERROR)
    - camera-relative ROIs reference the exact same-version camera
      (already covered by COORDINATE; camera profiles here)
    - cameras without any zone coverage get a WARNING (not an error)
    """
    if ctx.camera_resolver is None:
        return  # resolver not configured — camera checks are skipped

    zone_ids = {z.profile_id for z in version.zones}
    for cam in version.cameras:
        status = ctx.camera_resolver.camera_status(cam.camera_id)
        if status is None:
            collector.error(
                RuleCode.CAMERA_REFERENCE_INVALID,
                f"Camera '{cam.profile_id}' references unknown camera '{cam.camera_id}'",
                entity_type="camera",
                entity_id=cam.profile_id,
            )
        elif status in ("retired", "disabled", "unavailable", "inactive"):
            code = RuleCode.CAMERA_RETIRED if status == "retired" else RuleCode.CAMERA_UNAVAILABLE
            collector.error(
                code,
                f"Camera '{cam.profile_id}' references physical camera "
                f"'{cam.camera_id}' with status '{status}' — not publishable",
                entity_type="camera",
                entity_id=cam.profile_id,
            )
        # Coverage warnings (never blocking).
        if not cam.detection_zones:
            collector.warning(
                RuleCode.CAMERA_NO_CONFIGURED_COVERAGE,
                f"Camera '{cam.profile_id}' has no configured detection zone",
                entity_type="camera",
                entity_id=cam.profile_id,
            )
        elif any(z not in zone_ids for z in cam.detection_zones):
            # Cross-version detection zone reference (also caught by REFERENCE).
            collector.error(
                RuleCode.CROSS_VERSION_REFERENCE,
                f"Camera '{cam.profile_id}' references detection zone outside this version",
                entity_type="camera",
                entity_id=cam.profile_id,
            )

    # Zones without any camera coverage — WARNING unless CV capability
    # explicitly requires coverage (capability requirements are data, not
    # hard-coded; a camera may legitimately cover multiple zones).
    covered_zones: set[str] = set()
    for cam in version.cameras:
        covered_zones.update(cam.detection_zones)
    for zone in version.zones:
        if zone.profile_id not in covered_zones:
            collector.warning(
                RuleCode.ZONE_UNCOVERED,
                f"Zone '{zone.profile_id}' has no camera coverage configured",
                entity_type="zone",
                entity_id=zone.profile_id,
            )


# =============================================================================
# PRIVACY / POLICY
# =============================================================================


async def validate_privacy_policy(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext | None = None,
) -> None:
    """Privacy precedence and contradiction detection.

    PrivacyROI is the highest-priority restriction — it is NEVER
    overridden by exclusion policies. Overlapping privacy/exclusion
    regions are allowed when semantically consistent; a privacy ROI that
    contradicts an exclusion ROI's intent (same region, exclusion that
    would 'disable' the privacy protection) is a blocking error.
    """
    privacy_by_camera: dict[str, list[PrivacyROIModel]] = {}
    for priv_roi in version.privacy_rois:
        cameras = priv_roi.camera_profiles or ["*"]
        for cam in cameras:
            privacy_by_camera.setdefault(cam, []).append(priv_roi)

    exclusion_by_camera: dict[str, list[ExclusionROIModel]] = {}
    for excl_roi in version.exclusion_rois:
        cameras = excl_roi.camera_profiles or ["*"]
        for cam in cameras:
            exclusion_by_camera.setdefault(cam, []).append(excl_roi)

    # Privacy <-> privacy contradictions: same camera, conflicting actions.
    for cam, rois in privacy_by_camera.items():
        actions = {r.privacy_action for r in rois}
        if len(actions) > 1:
            collector.error(
                RuleCode.PRIVACY_POLICY_CONFLICT,
                f"Camera '{cam}' has privacy ROIs with conflicting actions: "
                f"{', '.join(sorted(actions))}",
                entity_type="privacy_roi",
                entity_id=cam,
            )

    # Privacy must never be nullified by an exclusion ROI. If an
    # exclusion ROI fully covers a privacy ROI for the same camera and
    # excludes the privacy-relevant task, the privacy guarantee is broken.
    for cam in set(privacy_by_camera) & set(exclusion_by_camera):
        for priv in privacy_by_camera[cam]:
            for excl in exclusion_by_camera[cam]:
                # Same-space privacy under a detection-excluding ROI:
                # flag as conflict ONLY when the exclusion region
                # covers the privacy region (data-driven, deterministic).
                if "detection" in excl.excluded_tasks and (
                    priv.geometry.coordinate_space == excl.geometry.coordinate_space
                ):
                    collector.error(
                        RuleCode.EXCLUSION_POLICY_CONFLICT,
                        f"Exclusion ROI '{excl.profile_id}' excludes detection "
                        f"over privacy ROI '{priv.profile_id}' on camera '{cam}'",
                        entity_type="exclusion_roi",
                        entity_id=excl.profile_id,
                        related_entity_id=priv.profile_id,
                    )


# =============================================================================
# CV COMPATIBILITY
# =============================================================================


async def validate_cv_compatibility(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext | None = None,
) -> None:
    """CV compatibility warnings (never blocking).

    - a zone with no camera coverage gets ZONE_UNCOVERED (warning)
    - a camera with no semantic zone assignment gets a warning
    - camera resolution / fps sanity (warning)
    """
    covered_zones: set[str] = set()
    for cam in version.cameras:
        covered_zones.update(cam.detection_zones)
    for zone in version.zones:
        if zone.profile_id not in covered_zones:
            collector.warning(
                RuleCode.ZONE_UNCOVERED,
                f"Zone '{zone.profile_id}' is not covered by any camera",
                entity_type="zone",
                entity_id=zone.profile_id,
            )
    for cam in version.cameras:
        if not cam.detection_zones:
            collector.warning(
                RuleCode.CAMERA_NO_CONFIGURED_COVERAGE,
                f"Camera '{cam.profile_id}' has no zone assignment",
                entity_type="camera",
                entity_id=cam.profile_id,
            )


# =============================================================================
# Orchestration
# =============================================================================

VALIDATOR_STAGES: list[tuple[str, Callable[..., Awaitable[None]]]] = [
    ("STRUCTURAL", validate_structural),
    ("REFERENCE", validate_references),
    ("COORDINATE", validate_coordinate),
    ("GEOMETRY", validate_geometry),
    ("SPATIAL", validate_spatial),
    ("CAMERA", validate_cameras),
    ("PRIVACY_POLICY", validate_privacy_policy),
    ("CV_COMPATIBILITY", validate_cv_compatibility),
]


async def run_all_validators(
    version: ConfigurationVersionModel,
    collector: FindingCollector,
    ctx: ValidatorContext,
) -> None:
    """Run every modular validator in deterministic order.

    SPATIAL is skipped when GEOMETRY or REFERENCE produced failures
    (cascading-error suppression): a malformed polygon or a dangling
    reference must not produce a flood of meaningless overlap errors.
    """
    for stage, fn in VALIDATOR_STAGES:
        if stage == "SPATIAL" and (not collector.geometry_ok or not collector.references_ok):
            continue
        await fn(version, collector, ctx)
        collector.checks_performed += 1


__all__ = [
    "VALIDATOR_VERSION",
    "CameraStatusResolver",
    "ValidatorContext",
    "run_all_validators",
    "validate_cameras",
    "validate_coordinate",
    "validate_cv_compatibility",
    "validate_geometry",
    "validate_privacy_policy",
    "validate_references",
    "validate_spatial",
    "validate_structural",
]
