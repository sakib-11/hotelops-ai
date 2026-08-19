"""Tests for the Task 15.5.1 movement state foundation.

Classifies a tracked entity as UNKNOWN / STATIONARY / MOVING from
consecutive canonical spatial observations (SpatialObservation +
SpatialPointModel, the Task 14 canonical point policy). Each measurement
is the deterministic Euclidean displacement + event-time delta of a
consecutive pair; the classification is the single configuration-driven
``TemporalPolicy.movement_threshold``.

Covered:

- the Euclidean distance and event-time delta math (3-4-5 triangle,
  axis-aligned, zero displacement, equal timestamps without division);
- the UNKNOWN / STATIONARY / MOVING states: first observation is the
  measurement anchor (UNKNOWN, no measurement), below/at/above the
  threshold classify deterministically, state_since is set on change and
  preserved on stays, thresholds are configuration-driven;
- jitter below the threshold never flaps the state;
- the 15.1 event-time discipline: deduplicated replays, within-window
  reorders (accept-with-no-rewind — never a measurement or rewind),
  beyond-window LateEventError, processing time irrelevant;
- isolation across tenant/venue/session/camera/configuration/track and
  rejection of cross-scope observations and mixed coordinate spaces;
- invalid inputs (missing key components, non-movement keys, wrong
  observation type, naive timestamps, non-finite points, out-of-range
  points) — all explicit, never repaired;
- checkpoint compatibility (round-trip, version/policy drift rejection);
- the pure-core boundary (no I/O, no current time, no hardcoded
  thresholds).

All fixtures use the REAL canonical contracts with fixed deterministic
IDs so replay comparisons are byte-exact.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from backend.app.intelligence.geometry.exceptions import InvalidCoordinateError
from backend.app.intelligence.temporal import (
    MOVEMENT_FSM,
    MovementEngine,
    MovementInput,
    MovementResult,
)
from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    LateEventError,
    StateKeyMismatchError,
)
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    FrameId,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
)
from contracts.geometry import CoordinateSpace
from contracts.spatial import (
    SpatialObservation,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.temporal import (
    MOVEMENT_STATES,
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    MovementCheckpoint,
    MovementMeasurement,
    MovementState,
    TemporalPolicy,
    TemporalStateKey,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
_TRACK = TrackId(UUID("60000000-0000-0000-0000-000000000001"))

_EVENT_BASE = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_PROCESSING_BASE = datetime(2026, 8, 1, 11, 0, 0, tzinfo=UTC)


# =============================================================================
# Fixture builders (real canonical contracts, deterministic IDs)
# =============================================================================


def _key(
    *,
    fsm_kind: str = "movement",
    tenant_id: TenantId = _TENANT,
    venue_id: VenueId = _VENUE,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG,
    track_id: TrackId = _TRACK,
    semantic_context: str | None = None,
) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind=fsm_kind,
        tenant_id=tenant_id,
        venue_id=venue_id,
        session_id=session_id,
        camera_id=camera_id,
        configuration_version_id=configuration_version_id,
        track_id=track_id,
        semantic_context=semantic_context,
    )


def _frame(index: int) -> FrameId:
    return FrameId(uuid5(TEMPORAL_ID_NAMESPACE, f"frame-{index}"))


def _event(seconds: int) -> datetime:
    return _EVENT_BASE + timedelta(seconds=seconds)


def _processing(seconds: int = 0) -> datetime:
    return _PROCESSING_BASE + timedelta(seconds=seconds)


def _spatial_obs(
    key: TemporalStateKey,
    *,
    x: float,
    y: float,
    event_time: datetime,
    frame_id: FrameId,
    coordinate_space: CoordinateSpace = CoordinateSpace.IMAGE_NORMALIZED,
    policy: SpatialPointPolicy = SpatialPointPolicy.FOOTPOINT,
) -> SpatialObservation:
    """A canonical SpatialObservation carrying the given canonical point."""
    return SpatialObservation(
        session_id=key.session_id,
        track_id=key.track_id,
        frame_id=frame_id,
        event_time=event_time,
        camera_id=key.camera_id,
        configuration_version_id=key.configuration_version_id,
        spatial_point=SpatialPointModel(x=x, y=y, coordinate_space=coordinate_space, policy=policy),
        status=SpatialStatus.INSIDE,
    )


def _move_engine(policy: TemporalPolicy | None = None, **kwargs) -> MovementEngine:
    return MovementEngine(fsm=MOVEMENT_FSM, policy=policy or TemporalPolicy(**kwargs))


def _apply(engine: MovementEngine, state: MovementState, obs: SpatialObservation) -> MovementResult:
    return engine.apply(
        state,
        MovementInput(key=state.key, observation=obs, processing_time=_processing()),
    )


def _chain(
    engine: MovementEngine,
    key: TemporalStateKey,
    timeline: tuple[tuple[float, float, int, int], ...],
) -> tuple[MovementState, list[MovementMeasurement]]:
    """Apply (x, y, seconds, frame index) observations; collect measurements."""
    state = engine.initial_state(key)
    measurements: list[MovementMeasurement] = []
    for x, y, seconds, frame_index in timeline:
        obs = _spatial_obs(key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index))
        result = _apply(engine, state, obs)
        state = result.state
        if result.measurement is not None:
            measurements.append(result.measurement)
    return state, measurements


# =============================================================================
# §10. Movement measurement math + provenance
# =============================================================================


class TestMovementMeasurement:
    """Distance and event-time delta of a consecutive pair."""

    def test_euclidean_distance_3_4_5(self) -> None:
        engine = _move_engine()
        key = _key()
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        ).state
        result = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.4, y=0.5, event_time=_event(2), frame_id=_frame(1)),
        )
        measurement = result.measurement
        assert measurement is not None
        assert measurement.distance == pytest.approx(0.5)  # sqrt(0.3^2 + 0.4^2)
        assert measurement.time_delta_seconds == pytest.approx(2.0)

    def test_axis_aligned_distance(self) -> None:
        engine = _move_engine()
        key = _key()
        _, measurements = _chain(engine, key, ((0.1, 0.5, 0, 0), (0.9, 0.5, 5, 5)))
        (measurement,) = measurements
        assert measurement.distance == pytest.approx(0.8)
        assert measurement.time_delta_seconds == pytest.approx(5.0)

    def test_zero_displacement_is_a_valid_measurement(self) -> None:
        engine = _move_engine()
        key = _key()
        _, measurements = _chain(engine, key, ((0.3, 0.3, 0, 0), (0.3, 0.3, 3, 3)))
        (measurement,) = measurements
        assert measurement.distance == pytest.approx(0.0)
        assert measurement.time_delta_seconds == pytest.approx(3.0)

    def test_measurement_preserves_full_provenance(self) -> None:
        engine = _move_engine()
        key = _key(semantic_context="z-lobby")
        _, measurements = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.2, 0.2, 1, 1)))
        (measurement,) = measurements
        assert measurement.key == key
        assert measurement.key.configuration_version_id == _CONFIG
        assert measurement.fsm_kind == "movement"
        assert (
            measurement.previous_position
            == _spatial_obs(
                key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)
            ).spatial_point
        )
        assert (
            measurement.current_position
            == _spatial_obs(
                key, x=0.2, y=0.2, event_time=_event(1), frame_id=_frame(1)
            ).spatial_point
        )
        assert measurement.previous_event_time == _event(0)
        assert measurement.event_time == _event(1)
        assert measurement.fsm_version == TEMPORAL_ENGINE_VERSION
        assert measurement.policy_revision == "v1"

    def test_measurement_id_content_derived_and_deterministic(self) -> None:
        engine = _move_engine()
        key = _key()
        _, first = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.2, 0.2, 1, 1)))
        _, second = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.2, 0.2, 1, 1)))
        assert first == second
        assert first[0].measurement_id == second[0].measurement_id

    def test_equal_timestamps_produce_zero_delta_without_division(self) -> None:
        # Two observations at the SAME event_time (later frame id) form a
        # valid pair: time_delta == 0, distance computed, never a divide
        # by zero.
        engine = _move_engine()
        key = _key()
        low_frame = FrameId(UUID("00000000-0000-0000-0000-000000000001"))
        high_frame = FrameId(UUID("00000000-0000-0000-0000-000000000002"))
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=low_frame),
        ).state
        result = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.3, y=0.1, event_time=_event(0), frame_id=high_frame),
        )
        measurement = result.measurement
        assert measurement is not None
        assert measurement.time_delta_seconds == pytest.approx(0.0)
        assert measurement.distance == pytest.approx(0.2)
        assert result.state.current_state == "moving"  # 0.2 >= default threshold 0.0

    def test_measurement_has_no_velocity(self) -> None:
        # Image-normalized displacement is deliberately NEVER converted
        # to physical velocity (Task 15.5.1 §4) — the fact carries no
        # speed/velocity field.
        engine = _move_engine()
        key = _key()
        _, measurements = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.2, 0.2, 1, 1)))
        (measurement,) = measurements
        assert not hasattr(measurement, "speed")
        assert not hasattr(measurement, "velocity")


# =============================================================================
# §6/§7/§11/§12. Movement states and configuration-driven classification
# =============================================================================


class TestMovementStates:
    """UNKNOWN / STATIONARY / MOVING with threshold-driven classification."""

    def test_initial_state_is_unknown(self) -> None:
        engine = _move_engine()
        assert engine.initial_state(_key()).current_state == "unknown"

    def test_first_observation_is_measurement_anchor(self) -> None:
        engine = _move_engine()
        key = _key()
        state = engine.initial_state(key)
        result = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        )
        assert result.state.current_state == "unknown"  # UNKNOWN stays UNKNOWN
        assert result.measurement is None  # a measurement is a PAIR
        assert result.state.previous_position is not None  # the anchor is set
        assert result.state.previous_event_time == _event(0)

    def test_below_threshold_is_stationary(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, measurements = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.12, 0.11, 1, 1)))
        (measurement,) = measurements
        assert measurement.distance < 0.1
        assert measurement.current_position.x == pytest.approx(0.12)
        assert state.current_state == "stationary"

    def test_above_threshold_is_moving(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, measurements = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1)))
        (measurement,) = measurements
        assert measurement.distance >= 0.1
        assert state.current_state == "moving"

    def test_at_threshold_is_stationary(self) -> None:
        # A pair whose distance EXACTLY equals the threshold is NOT
        # "exceeding" it — strictly-above semantics (Task 15.5.1 §12).
        # Coordinates are exactly representable in binary (quarters) so
        # the boundary assertion is exact, never float-noise-dependent.
        engine = _move_engine(movement_threshold=0.5)
        key = _key()
        state, _ = _chain(engine, key, ((0.25, 0.25, 0, 0), (0.75, 0.25, 1, 1)))
        assert state.current_state == "stationary"

    def test_stationary_to_moving_transition(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, _ = _chain(
            engine,
            key,
            (
                (0.1, 0.1, 0, 0),
                (0.11, 0.11, 1, 1),  # stationary
                (0.4, 0.4, 2, 2),  # moving
            ),
        )
        assert state.current_state == "moving"
        assert state.state_since == _event(2)  # state_since = entry of MOVING

    def test_moving_to_stationary_transition(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, _ = _chain(
            engine,
            key,
            (
                (0.1, 0.1, 0, 0),
                (0.5, 0.1, 1, 1),  # moving
                (0.51, 0.11, 2, 2),  # stationary (0.014 < 0.1)
            ),
        )
        assert state.current_state == "stationary"
        assert state.state_since == _event(2)

    def test_state_since_preserved_on_stay(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, _ = _chain(
            engine,
            key,
            (
                (0.1, 0.1, 0, 0),
                (0.5, 0.1, 1, 1),  # -> moving, state_since = t1
                (0.6, 0.2, 2, 2),  # still moving (stay), state_since kept
            ),
        )
        assert state.current_state == "moving"
        assert state.state_since == _event(1)  # never reset on a stay

    def test_threshold_is_configuration_driven(self) -> None:
        # The SAME trajectory (displacement 0.4) classifies differently
        # under different configured thresholds — never hardcoded.
        trajectory = ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1))
        strict, _ = _chain(_move_engine(movement_threshold=0.3), _key(), trajectory)
        lenient, _ = _chain(_move_engine(movement_threshold=0.5), _key(), trajectory)
        assert strict.current_state == "moving"
        assert lenient.current_state == "stationary"

    def test_only_three_states_exist(self) -> None:
        assert MOVEMENT_STATES == ("unknown", "stationary", "moving")
        assert MOVEMENT_FSM.states == MOVEMENT_STATES


# =============================================================================
# §13. Jitter never flaps the state
# =============================================================================


class TestJitter:
    """Small positional fluctuations below the threshold stay STATIONARY."""

    def test_jitter_below_threshold_no_flapping(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, measurements = _chain(
            engine,
            key,
            (
                (0.5, 0.5, 0, 0),
                (0.52, 0.51, 1, 1),
                (0.49, 0.52, 2, 2),
                (0.51, 0.49, 3, 3),
            ),
        )
        assert state.current_state == "stationary"
        assert len(measurements) == 3  # one per pair after the anchor
        for measurement in measurements:
            assert measurement.distance < 0.1  # every pair classified stationary
            assert measurement.distance > 0.0  # real (small) displacement, measured


# =============================================================================
# §5/§9. Event-time discipline and idempotency
# =============================================================================


class TestEventTimeOrdering:
    """The 15.1 ordering policy applied per-track; duplicates advance once."""

    def test_processing_time_never_affects_the_result(self) -> None:
        key = _key()
        obs = _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0))
        engine = _move_engine()
        early = engine.apply(
            engine.initial_state(key),
            MovementInput(key=key, observation=obs, processing_time=_processing(-9000)),
        )
        late = engine.apply(
            engine.initial_state(key),
            MovementInput(key=key, observation=obs, processing_time=_processing(9000)),
        )
        assert early.state == late.state
        assert early.measurement == late.measurement

    def test_duplicate_observation_is_deduplicated(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        ).state
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.5, y=0.1, event_time=_event(1), frame_id=_frame(1)),
        ).state
        assert state.current_state == "moving"
        replay = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.5, y=0.1, event_time=_event(1), frame_id=_frame(1)),
        )
        assert replay.deduplicated is True
        assert replay.measurement is None  # no duplicate distance
        assert replay.state == state  # no duplicate state transition

    def test_within_window_reorder_never_rewinds(self) -> None:
        # (10:00, 10:02) applied, then (10:01): within the window ->
        # REORDERED fact, no measurement, no anchor/state/watermark rewind.
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        ).state
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.5, y=0.1, event_time=_event(2), frame_id=_frame(2)),
        ).state
        assert state.current_state == "moving"
        late = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.2, y=0.1, event_time=_event(1), frame_id=_frame(1)),
        )
        assert late.reordered is True
        assert late.measurement is None  # reorders never measure
        assert late.state.current_state == "moving"  # never rewinds the state
        assert late.state.previous_event_time == _event(2)  # anchor untouched

    def test_replayed_reorder_reproduces_reordered(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        ).state
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.5, y=0.1, event_time=_event(2), frame_id=_frame(2)),
        ).state
        late_obs = _spatial_obs(key, x=0.2, y=0.1, event_time=_event(1), frame_id=_frame(1))
        first = _apply(engine, state, late_obs)
        second = _apply(engine, state, late_obs)
        assert first.reordered is True
        assert second.reordered is True
        assert second.deduplicated is False

    def test_late_beyond_window_rejected(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        ).state
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.5, y=0.1, event_time=_event(120), frame_id=_frame(120)),
        ).state
        with pytest.raises(LateEventError, match="reordering window"):
            _apply(
                engine,
                state,
                _spatial_obs(key, x=0.2, y=0.1, event_time=_event(30), frame_id=_frame(30)),
            )


# =============================================================================
# §8. Isolation
# =============================================================================


class TestIsolation:
    """Movement state never mixes across any canonical scope."""

    def _measure_for(self, engine: MovementEngine, key: TemporalStateKey) -> MovementMeasurement:
        _, measurements = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1)))
        return measurements[0]

    def test_tenant_isolation(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        other = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        a = self._measure_for(engine, _key())
        b = self._measure_for(engine, _key(tenant_id=other))
        assert a.key.tenant_id != b.key.tenant_id
        assert a.measurement_id != b.measurement_id

    def test_venue_isolation(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        other = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        a = self._measure_for(engine, _key())
        b = self._measure_for(engine, _key(venue_id=other))
        assert a.measurement_id != b.measurement_id

    def test_session_isolation(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        other = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        a = self._measure_for(engine, _key())
        b = self._measure_for(engine, _key(session_id=other))
        assert a.measurement_id != b.measurement_id

    def test_camera_isolation(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        other = CameraId(UUID("40000000-0000-0000-0000-000000000099"))
        a = self._measure_for(engine, _key())
        b = self._measure_for(engine, _key(camera_id=other))
        assert a.measurement_id != b.measurement_id

    def test_configuration_version_isolation(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        other = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000099"))
        a = self._measure_for(engine, _key())
        b = self._measure_for(engine, _key(configuration_version_id=other))
        assert a.measurement_id != b.measurement_id

    def test_track_isolation(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        other = TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        a = self._measure_for(engine, _key())
        b = self._measure_for(engine, _key(track_id=other))
        assert a.measurement_id != b.measurement_id

    def test_configuration_pinned_for_historical_sessions(self) -> None:
        # §15: a V1 session stays on V1 even after V2 is published — the
        # measurements carry the pinned configuration version and a V1
        # replay is byte-identical (the engine never queries "latest").
        engine = _move_engine(movement_threshold=0.1)
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        timeline = ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1))
        _, measurements_v1 = _chain(engine, _key(configuration_version_id=v1), timeline)
        _, measurements_v2 = _chain(engine, _key(configuration_version_id=v2), timeline)
        _, measurements_v1_replay = _chain(engine, _key(configuration_version_id=v1), timeline)
        assert measurements_v1[0].key.configuration_version_id == v1
        assert measurements_v2[0].key.configuration_version_id == v2
        assert measurements_v1[0].measurement_id != measurements_v2[0].measurement_id
        assert measurements_v1_replay == measurements_v1  # V1 unchanged after V2

    def test_same_track_in_two_sessions_is_independent(self) -> None:
        # §8: the same track identifier in two sessions must not share
        # state — sessions are separate keys.
        engine = _move_engine(movement_threshold=0.1)
        s1 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000001"))
        s2 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000002"))
        state1, _ = _chain(engine, _key(session_id=s1), ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1)))
        state2, _ = _chain(engine, _key(session_id=s2), ((0.9, 0.9, 0, 0),))
        assert state1.current_state == "moving"
        assert state2.current_state == "unknown"  # session 2's anchor is independent
        assert state1.key != state2.key

    def test_cross_session_observation_rejected(self) -> None:
        engine = _move_engine()
        key = _key()
        wrong = _spatial_obs(
            _key(session_id=VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))),
            x=0.1,
            y=0.1,
            event_time=_event(0),
            frame_id=_frame(0),
        )
        with pytest.raises(StateKeyMismatchError, match="session_id"):
            _apply(engine, engine.initial_state(key), wrong)

    def test_mixed_coordinate_spaces_rejected(self) -> None:
        # A VENUE_LOCAL metric point must never be measured against an
        # IMAGE_NORMALIZED point — displacement is undefined across
        # spaces (Task 15.5.1 §4).
        engine = _move_engine()
        key = _key()
        state = engine.initial_state(key)
        state = _apply(
            engine,
            state,
            _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0)),
        ).state
        venue_local = _spatial_obs(
            key,
            x=0.2,
            y=0.2,
            event_time=_event(1),
            frame_id=_frame(1),
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
        )
        with pytest.raises(InvalidTemporalInputError, match="coordinate space"):
            _apply(engine, state, venue_local)


# =============================================================================
# §14. Invalid inputs
# =============================================================================


class TestInvalidInputs:
    """Missing or malformed inputs fail explicitly — never fabricated."""

    def test_missing_track_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
            )

    def test_missing_session_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_tenant_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement",
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_venue_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement",
                tenant_id=_TENANT,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                track_id=_TRACK,
            )

    def test_non_movement_key_rejected(self) -> None:
        engine = _move_engine()
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.initial_state(_key(fsm_kind="presence"))

    def test_wrong_observation_type_rejected(self) -> None:
        engine = _move_engine()
        key = _key()
        with pytest.raises(InvalidTemporalInputError, match="SpatialObservation"):
            engine.apply(
                engine.initial_state(key),
                MovementInput(
                    key=key,
                    observation=object(),  # type: ignore[arg-type]
                    processing_time=_processing(),
                ),
            )

    def test_input_key_must_match_state_key(self) -> None:
        engine = _move_engine()
        key = _key()
        obs = _spatial_obs(key, x=0.1, y=0.1, event_time=_event(0), frame_id=_frame(0))
        with pytest.raises(InvalidTemporalInputError, match="must match the state key"):
            engine.apply(
                engine.initial_state(
                    _key(track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")))
                ),
                MovementInput(key=key, observation=obs, processing_time=_processing()),
            )

    def test_invalid_event_timestamp_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError):
            _spatial_obs(
                _key(),
                x=0.1,
                y=0.1,
                event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
                frame_id=_frame(0),
            )

    def test_non_finite_point_rejected_at_geometry_boundary(self) -> None:
        # VENUE_LOCAL is unbounded so a NaN slips past the pydantic
        # contract; the movement engine re-asserts the Task 14 Step 2
        # geometry boundary (validate_coordinate) before measuring.
        engine = _move_engine()
        key = _key()
        obs = _spatial_obs(
            key,
            x=float("nan"),
            y=0.5,
            event_time=_event(0),
            frame_id=_frame(0),
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
        )
        with pytest.raises(InvalidCoordinateError):
            _apply(engine, engine.initial_state(key), obs)

    def test_out_of_range_normalized_point_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            _spatial_obs(_key(), x=1.5, y=0.5, event_time=_event(0), frame_id=_frame(0))

    def test_corrupted_state_anchor_rejected_at_contract(self) -> None:
        # A previous position without its event time is a corrupted
        # anchor — rejected by the model invariant, never repaired.
        with pytest.raises(ValueError, match="set together"):
            MovementState(
                fsm_version=TEMPORAL_ENGINE_VERSION,
                key=_key(),
                current_state="stationary",
                previous_position=SpatialPointModel(
                    x=0.1, y=0.1, policy=SpatialPointPolicy.FOOTPOINT
                ),
                previous_event_time=None,
            )


# =============================================================================
# §15. Checkpoint compatibility
# =============================================================================


class TestCheckpointCompatibility:
    """Movement state is checkpointable under the existing discipline."""

    def _moving_state(self) -> tuple[MovementEngine, MovementState, TemporalStateKey]:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, _ = _chain(engine, key, ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1)))
        return engine, state, key

    def test_checkpoint_round_trip(self) -> None:
        engine, state, _ = self._moving_state()
        checkpoint = engine.checkpoint(state)
        data = checkpoint.to_dict()
        assert MovementCheckpoint.from_dict(data) == checkpoint
        assert engine.restore(checkpoint) == state

    def test_restore_rejects_engine_version_drift(self) -> None:
        engine, state, _ = self._moving_state()
        checkpoint = MovementCheckpoint(
            engine_version="9.9.9",
            policy_revision="v1",
            state=state,
        )
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            engine.restore(checkpoint)

    def test_restore_rejects_policy_drift(self) -> None:
        engine = _move_engine(policy=TemporalPolicy(revision="v2"))
        state = engine.initial_state(_key())
        checkpoint = MovementCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
            state=state,
        )
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            engine.restore(checkpoint)

    def test_restore_rejects_cross_fsm_checkpoint(self) -> None:
        engine = _move_engine()
        with pytest.raises(InvalidTemporalInputError, match="MovementCheckpoint"):
            engine.restore(object())  # type: ignore[arg-type]


# =============================================================================
# Full canonical chain + determinism
# =============================================================================


class TestChainAndDeterminism:
    """SpatialObservation (canonical points) -> movement, byte-identical."""

    def test_chain_with_real_canonical_points(self) -> None:
        # The full 15.5.1 chain: canonical SpatialObservation positions
        # (FOOTPOINT policy) drive the movement classification.
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        state, measurements = _chain(
            engine,
            key,
            (
                (0.2, 0.8, 0, 0),
                (0.25, 0.8, 1, 1),  # 0.05 -> stationary
                (0.7, 0.3, 2, 2),  # ~0.73 -> moving
                (0.71, 0.3, 3, 3),  # 0.01 -> stationary
            ),
        )
        assert state.current_state == "stationary"
        assert [m.distance for m in measurements] == pytest.approx([
            0.05,
            math.hypot(0.45, 0.5),
            0.01,
        ])
        assert (
            state.previous_position
            == _spatial_obs(
                key, x=0.71, y=0.3, event_time=_event(3), frame_id=_frame(3)
            ).spatial_point
        )

    def test_full_replay_is_identical(self) -> None:
        engine = _move_engine(movement_threshold=0.1)
        key = _key()
        timeline = ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1), (0.52, 0.12, 2, 2))
        state1, measurements1 = _chain(engine, key, timeline)
        state2, measurements2 = _chain(engine, key, timeline)
        assert state2 == state1
        assert measurements2 == measurements1
        assert [m.measurement_id for m in measurements1] == [
            m.measurement_id for m in measurements2
        ]

    def test_measurement_math_matches_geometry_library_formula(self) -> None:
        # The engine's displacement equals the canonical Euclidean
        # formula used by the Task 14 Step 2 geometry library (hypot).
        engine = _move_engine()
        key = _key()
        _, measurements = _chain(engine, key, ((0.1, 0.2, 0, 0), (0.4, 0.6, 1, 1)))
        (measurement,) = measurements
        assert measurement.distance == pytest.approx(math.hypot(0.4 - 0.1, 0.6 - 0.2))


# =============================================================================
# §16. Pure core
# =============================================================================


class TestMovementPurity:
    """The movement core performs no I/O and reads no current time."""

    def test_movement_core_is_pure(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
        text = (package_dir / "movement.py").read_text()
        forbidden = [
            "sqlalchemy",
            "redis",
            "httpx",
            "boto3",
            "botocore",
            "openai",
            "anthropic",
            "urllib",
            "requests",
            "socket",
            "asyncio",
            "random",
            "time",
        ]
        for module in forbidden:
            assert not re.search(rf"^\s*(from|import)\s+{module}\b", text, re.MULTILINE), (
                f"I/O/stateful module {module!r} leaked into movement.py"
            )
        assert "now(" not in text
        assert "utc_now" not in text
        assert "print(" not in text
