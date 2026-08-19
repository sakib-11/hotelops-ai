"""Unit tests for the deterministic configuration validation engine (Task 10.9)."""

from __future__ import annotations

import uuid

from backend.app.domain.configuration.validation import (
    ConfigurationValidationEngine,
    RuleCode,
)
from backend.app.domain.configuration.validation.validators import CameraStatusResolver
from contracts.common import CameraId
from contracts.configuration import (
    CameraMountType,
    CameraProfileModel,
    ConfigurationVersionModel,
    EntranceDirection,
    EntranceModel,
    ExclusionROIModel,
    GeometryModel,
    GeometryScope,
    GeometryType,
    PrivacyROIModel,
    QueueAreaModel,
    TableModel,
    ZoneModel,
    ZoneType,
)
from contracts.geometry import CoordinateSpace

TENANT = uuid.uuid4()
VENUE = uuid.uuid4()
CONFIG = uuid.uuid4()


def _polygon(coords, space=CoordinateSpace.VENUE_LOCAL, scope=GeometryScope.VENUE):
    return GeometryModel(
        geometry_id=f"g-{uuid.uuid4()}",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=space,
        geometry_scope=scope,
        coordinates=[*coords, coords[0]],
    )


def _camera_roi(coords, cam_ref="cam-1"):
    return GeometryModel(
        geometry_id=f"g-{uuid.uuid4()}",
        geometry_type=GeometryType.POLYGON,
        coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
        geometry_scope=GeometryScope.CAMERA,
        reference_camera_profile_id=cam_ref,
        coordinates=[*coords, coords[0]],
    )


def _version(**overrides) -> ConfigurationVersionModel:
    base = {
        "configuration_version_id": uuid.uuid4(),
        "configuration_id": CONFIG,
        "venue_id": VENUE,
        "tenant_id": TENANT,
        "version": 1,
    }
    base.update(overrides)
    return ConfigurationVersionModel(**base)


def _zone(profile_id="z1", coords=None, zone_type=ZoneType.LOBBY) -> ZoneModel:
    return ZoneModel(
        profile_id=profile_id,
        name=profile_id,
        zone_type=zone_type,
        geometry=_polygon(coords or [[0, 0], [10, 0], [10, 10], [0, 10]]),
    )


def _table(profile_id="t1", coords=None) -> TableModel:
    return TableModel(
        profile_id=profile_id,
        name=profile_id,
        geometry=_polygon(coords or [[1, 1], [2, 1], [2, 2], [1, 2]]),
    )


def _camera(
    profile_id="cam-1", camera_id=None, detection_zones=None, privacy_rois=None, exclusion_rois=None
) -> CameraProfileModel:
    return CameraProfileModel(
        profile_id=profile_id,
        camera_id=CameraId(camera_id or uuid.uuid4()),
        camera_reference=profile_id,
        resolution_width=1920,
        resolution_height=1080,
        mount_type=CameraMountType.CEILING,
        detection_zones=detection_zones or [],
        privacy_rois=privacy_rois or [],
        exclusion_rois=exclusion_rois or [],
    )


class TestStructural:
    async def test_duplicate_profile_ids_across_categories(self) -> None:
        # The contract already rejects duplicates at construction
        # (defense in depth); the engine re-checks for data that bypassed
        # contract validation (e.g. DB-loaded snapshots), so build via
        # model_construct to exercise the engine path.
        zone = _zone("shared")
        table = _table("shared")
        v = _version().model_construct(**{
            **{
                "configuration_version_id": uuid.uuid4(),
                "configuration_id": CONFIG,
                "venue_id": VENUE,
                "tenant_id": TENANT,
                "version": 1,
            },
            "zones": [zone],
            "tables": [table],
        })
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        codes = [f.code for f in out.errors]
        assert RuleCode.DUPLICATE_IDENTIFIER in codes

    async def test_empty_profile_id(self) -> None:
        zone = _zone(" ")
        v = _version().model_construct(**{
            **{
                "configuration_version_id": uuid.uuid4(),
                "configuration_id": CONFIG,
                "venue_id": VENUE,
                "tenant_id": TENANT,
                "version": 1,
            },
            "zones": [zone],
        })
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.MISSING_REFERENCE for f in out.errors)


class TestReference:
    async def test_missing_zone_reference(self) -> None:
        cam = _camera(detection_zones=["nope"])
        v = _version(cameras=[cam])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.MISSING_REFERENCE for f in out.errors)

    async def test_cross_version_camera_roi_reference(self) -> None:
        roi = PrivacyROIModel(
            profile_id="p1",
            name="Privacy",
            geometry=_camera_roi([[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]], cam_ref="ghost"),
        )
        v = _version(privacy_rois=[roi])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.CAMERA_REFERENCE_INVALID for f in out.errors)


class TestGeometry:
    async def test_self_intersecting_polygon(self) -> None:
        v = _version(zones=[_zone("z1", [[0, 0], [4, 4], [0, 4], [4, 0]])])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.GEOMETRY_SELF_INTERSECTION for f in out.errors)

    async def test_zero_area_polygon(self) -> None:
        v = _version(zones=[_zone("z1", [[0, 0], [4, 0], [8, 0]])])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.GEOMETRY_ZERO_AREA for f in out.errors)

    async def test_out_of_range_image_normalized(self) -> None:
        # Contract construction rejects out-of-range now; the engine
        # re-checks via model_construct (bypassed contract) to prove the
        # rule is enforced in both layers.
        roi = PrivacyROIModel.model_construct(
            profile_id="p1",
            name="P",
            geometry=GeometryModel.model_construct(
                geometry_id="g",
                geometry_type=GeometryType.POLYGON,
                coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
                geometry_scope=GeometryScope.CAMERA,
                reference_camera_profile_id="cam-1",
                coordinates=[[0, 0], [1.2, 0], [1.2, 1], [0, 1], [0, 0]],
            ),
        )
        v = _version().model_construct(**{
            **{
                "configuration_version_id": uuid.uuid4(),
                "configuration_id": CONFIG,
                "venue_id": VENUE,
                "tenant_id": TENANT,
                "version": 1,
            },
            "privacy_rois": [roi],
        })
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.GEOMETRY_OUT_OF_RANGE for f in out.errors)

    async def test_entrance_geometry_contract_linestring_allowed(self) -> None:
        entrance = EntranceModel(
            profile_id="e1",
            name="Main",
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.LINESTRING,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[0, 0], [5, 5]],
            ),
            direction=EntranceDirection.ENTRANCE,
        )
        v = _version(entrances=[entrance])
        out = await ConfigurationValidationEngine().validate(v)
        assert not any(f.code == RuleCode.ENTITY_GEOMETRY_CONTRACT_VIOLATION for f in out.errors)

    async def test_zone_rectangle_invalid_contract(self) -> None:
        # RECTANGLE no longer exists; a point zone violates the contract.
        zone = ZoneModel(
            profile_id="z1",
            name="Z",
            zone_type=ZoneType.CUSTOM,
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.POINT,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[1, 1]],
            ),
        )
        v = _version(zones=[zone])
        out = await ConfigurationValidationEngine().validate(v)
        assert any(f.code == RuleCode.ENTITY_GEOMETRY_CONTRACT_VIOLATION for f in out.errors)


class TestSpatialPolicy:
    async def test_table_table_overlap_blocking(self) -> None:
        v = _version(
            tables=[
                _table("t1", [[0, 0], [2, 0], [2, 2], [0, 2]]),
                _table("t2", [[1, 1], [3, 1], [3, 3], [1, 3]]),
            ]
        )
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.TABLE_OVERLAP for f in out.errors)

    async def test_table_boundary_touch_not_overlap(self) -> None:
        v = _version(
            tables=[
                _table("t1", [[0, 0], [2, 0], [2, 2], [0, 2]]),
                _table("t2", [[2, 0], [4, 0], [4, 2], [2, 2]]),
            ]
        )
        out = await ConfigurationValidationEngine().validate(v)
        assert out.valid
        assert not any(f.code == RuleCode.TABLE_OVERLAP for f in out.errors)

    async def test_table_not_contained_by_declared_zone(self) -> None:
        zone = _zone("z1", [[0, 0], [10, 0], [10, 10], [0, 10]])
        # Table is inside the zone geometry but NOT declared — a table
        # with NO parent is fine. Add a parent and a table outside.
        v = _version(
            zones=[zone],
            tables=[_table("t1", [[50, 50], [52, 50], [52, 52], [50, 52]])],
        )
        out = await ConfigurationValidationEngine().validate(v)
        assert out.valid  # no declared containment, no error

    async def test_declared_parent_zone_must_contain_queue(self) -> None:
        zone = _zone("z1", [[0, 0], [10, 0], [10, 10], [0, 10]])
        outside_queue = QueueAreaModel(
            profile_id="q1",
            name="Q",
            geometry=_polygon([[50, 50], [52, 50], [52, 52], [50, 52]]),
            zone_profile_id="z1",
        )
        v = _version(zones=[zone], queue_areas=[outside_queue])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.INVALID_CONTAINMENT for f in out.errors)

    async def test_entrance_may_touch_zone_boundary(self) -> None:
        zone = _zone("z1", [[0, 0], [10, 0], [10, 10], [0, 10]])
        entrance = EntranceModel(
            profile_id="e1",
            name="E",
            geometry=GeometryModel(
                geometry_id="g",
                geometry_type=GeometryType.LINESTRING,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                geometry_scope=GeometryScope.VENUE,
                coordinates=[[5, 0], [5, -2]],
            ),
            zone_profile_id="z1",
        )
        v = _version(zones=[zone], entrances=[entrance])
        out = await ConfigurationValidationEngine().validate(v)
        assert out.valid


class TestCameraLifecycle:
    class _Resolver(CameraStatusResolver):
        def __init__(self, statuses: dict[str, str] | None = None) -> None:
            self.statuses = statuses or {}

        def camera_status(self, camera_id: object) -> str | None:
            return self.statuses.get(str(camera_id))

    async def test_retired_camera_blocks_publishable(self) -> None:
        cam = _camera(camera_id=uuid.uuid4())
        resolver = self._Resolver({str(cam.camera_id): "retired"})
        v = _version(cameras=[cam])
        out = await ConfigurationValidationEngine(camera_resolver=resolver).validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.CAMERA_RETIRED for f in out.errors)

    async def test_unknown_camera_reference(self) -> None:
        cam = _camera(camera_id=uuid.uuid4())
        resolver = self._Resolver({})
        v = _version(cameras=[cam])
        out = await ConfigurationValidationEngine(camera_resolver=resolver).validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.CAMERA_REFERENCE_INVALID for f in out.errors)

    async def test_unavailable_camera(self) -> None:
        cam = _camera(camera_id=uuid.uuid4())
        resolver = self._Resolver({str(cam.camera_id): "inactive"})
        v = _version(cameras=[cam])
        out = await ConfigurationValidationEngine(camera_resolver=resolver).validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.CAMERA_UNAVAILABLE for f in out.errors)

    async def test_active_camera_with_coverage_no_errors(self) -> None:
        cam = _camera(camera_id=uuid.uuid4(), detection_zones=["z1"])
        resolver = self._Resolver({str(cam.camera_id): "active"})
        v = _version(cameras=[cam], zones=[_zone("z1")])
        out = await ConfigurationValidationEngine(camera_resolver=resolver).validate(v)
        assert out.valid


class TestPrivacyPolicy:
    async def test_privacy_conflicting_actions_rejected(self) -> None:
        roi_a = PrivacyROIModel(
            profile_id="p1",
            name="P1",
            geometry=_camera_roi([[0, 0], [0.4, 0], [0.4, 0.4], [0, 0.4]]),
            privacy_action="blur",
            camera_profiles=["cam-1"],
        )
        roi_b = PrivacyROIModel(
            profile_id="p2",
            name="P2",
            geometry=_camera_roi([[0, 0], [0.4, 0], [0.4, 0.4], [0, 0.4]]),
            privacy_action="exclude",
            camera_profiles=["cam-1"],
        )
        cam = _camera(privacy_rois=["p1", "p2"])
        v = _version(cameras=[cam], privacy_rois=[roi_a, roi_b])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.PRIVACY_POLICY_CONFLICT for f in out.errors)

    async def test_exclusion_does_not_nullify_privacy(self) -> None:
        priv = PrivacyROIModel(
            profile_id="p1",
            name="P1",
            geometry=_camera_roi([[0, 0], [0.4, 0], [0.4, 0.4], [0, 0.4]]),
            privacy_action="blur",
            camera_profiles=["cam-1"],
        )
        excl = ExclusionROIModel(
            profile_id="x1",
            name="X1",
            geometry=_camera_roi([[0, 0], [0.4, 0], [0.4, 0.4], [0, 0.4]]),
            excluded_tasks=["detection"],
            camera_profiles=["cam-1"],
        )
        cam = _camera(privacy_rois=["p1"], exclusion_rois=["x1"])
        v = _version(cameras=[cam], privacy_rois=[priv], exclusion_rois=[excl])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        assert any(f.code == RuleCode.EXCLUSION_POLICY_CONFLICT for f in out.errors)


class TestCoverageWarnings:
    async def test_uncovered_zone_is_warning_not_error(self) -> None:
        v = _version(zones=[_zone("z1")])
        out = await ConfigurationValidationEngine().validate(v)
        assert out.valid  # warnings never block
        assert any(f.code == RuleCode.ZONE_UNCOVERED for f in out.warnings)

    async def test_camera_without_zone_is_warning(self) -> None:
        v = _version(cameras=[_camera()])
        out = await ConfigurationValidationEngine().validate(v)
        assert out.valid
        assert any(f.code == RuleCode.CAMERA_NO_CONFIGURED_COVERAGE for f in out.warnings)


class TestDeterminism:
    async def test_identical_input_identical_result(self) -> None:
        cam = _camera(detection_zones=["z1"])
        v1 = _version(cameras=[cam], zones=[_zone("z1")])
        v2 = v1.model_copy(deep=True)
        engine = ConfigurationValidationEngine()
        o1 = await engine.validate(v1)
        o2 = await engine.validate(v2)
        assert o1.valid == o2.valid
        assert [f.code.value for f in o1.errors] == [f.code.value for f in o2.errors]
        assert [f.code.value for f in o1.warnings] == [f.code.value for f in o2.warnings]
        assert o1.checks_performed == o2.checks_performed

    async def test_stable_validator_version(self) -> None:
        engine = ConfigurationValidationEngine()
        assert engine.validator_version == "10.1.0"


class TestCascadingSuppression:
    async def test_bad_geometry_suppresses_spatial_noise(self) -> None:
        # One self-intersecting table + one normal overlapping table:
        # the self-intersection must NOT produce a cascade of overlap
        # findings on top of the geometry error.
        bowtie = _table("t1", [[0, 0], [4, 4], [0, 4], [4, 0]])
        other = _table("t2", [[1, 1], [3, 1], [3, 3], [1, 3]])
        v = _version(tables=[bowtie, other])
        out = await ConfigurationValidationEngine().validate(v)
        assert not out.valid
        codes = [f.code for f in out.errors]
        assert RuleCode.GEOMETRY_SELF_INTERSECTION in codes
        # Spatial overlap checks were suppressed — no TABLE_OVERLAP noise.
        assert RuleCode.TABLE_OVERLAP not in codes
