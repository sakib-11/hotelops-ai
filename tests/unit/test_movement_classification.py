"""Tests for the Task 15.5.2 movement classification layer.

The classification layer sits ON TOP of the 15.5.1 measurement foundation:
it consumes ``MovementMeasurement`` facts (distance + event-time delta of a
consecutive pair — NEVER recomputed here) and applies hysteresis
(``movement_enter_threshold`` > ``movement_exit_threshold``) plus event-time
qualification (``movement_qualification_seconds``) to decide UNKNOWN /
STATIONARY / MOVING deterministically.

Covered:

- the classification model: UNKNOWN is the pristine state, the first
  measurement classifies directly, MOVING means the displacement EXCEEDS the
  movement policy, STATIONARY means it remains below the stationary policy;
- configuration-driven thresholds — the SAME trajectory classifies
  differently under different ``TemporalPolicy`` values (never hardcoded);
- hysteresis: enter > exit, the band retains the current state in BOTH
  directions, and a band measurement cancels an in-progress qualification run;
- temporal qualification: a state change requires the evidence to remain
  sustained for ``movement_qualification_seconds`` of EVENT time, the
  transition event_time is the confirming measurement (never the run start,
  never processing time), and ``0.0`` disables qualification;
- stable movement / stable stationary transitions at the correct event_time;
- jitter around a stationary entity never flaps the classification;
- the 15.1 event-time discipline: processing time is irrelevant, event_time
  is authoritative, out-of-order inputs follow the reorder/late policy,
  duplicate inputs are idempotent (content-derived transition IDs);
- occlusion: a measurement-less step never resets the classification or an
  in-progress qualification run;
- isolation across tenant/venue/session/camera/configuration/track/spatial
  context and rejection of cross-scope measurements;
- configuration provenance: transitions carry the pinned policy revision and
  configuration version; replay under the same revision is byte-identical;
- checkpoint compatibility (round-trip, restart recovery == uninterrupted,
  version/policy drift rejection);
- the golden timeline T1-T8 with exact state/transition/event_time;
- failure tests (missing scopes, malformed measurements, naive timestamps,
  backwards timestamps, non-finite distances, mismatched policies) — all
  explicit, never repaired, movement never fabricated;
- bounded state (no history accumulation) and the pure-core boundary.

All fixtures use the REAL canonical contracts with fixed deterministic IDs so
replay comparisons are byte-exact.
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
    MOVEMENT_CLASSIFICATION_FSM,
    MOVEMENT_FSM,
    MovementClassificationEngine,
    MovementClassificationInput,
    MovementClassificationResult,
    MovementEngine,
    MovementInput,
    classification_input_from_movement,
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
    EventId,
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
    MovementClassificationCheckpoint,
    MovementClassificationState,
    MovementClassificationTransition,
    MovementMeasurement,
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

# Hysteresis policy used across most tests: enter dominates exit and the
# band is wide enough to catch noise near the boundary.
ENTER = 0.15
EXIT = 0.05


# =============================================================================
# Fixture builders (real canonical contracts, deterministic IDs)
# =============================================================================


def _key(
    *,
    fsm_kind: str = "movement_classification",
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


def _point(x: float, y: float) -> SpatialPointModel:
    return SpatialPointModel(
        x=x,
        y=y,
        coordinate_space=CoordinateSpace.IMAGE_NORMALIZED,
        policy=SpatialPointPolicy.FOOTPOINT,
    )


def _spatial_obs(
    key: TemporalStateKey,
    *,
    x: float,
    y: float,
    event_time: datetime,
    frame_id: FrameId,
) -> SpatialObservation:
    return SpatialObservation(
        session_id=key.session_id,
        track_id=key.track_id,
        frame_id=frame_id,
        event_time=event_time,
        camera_id=key.camera_id,
        configuration_version_id=key.configuration_version_id,
        spatial_point=_point(x, y),
        status=SpatialStatus.INSIDE,
    )


def _measurement(
    *,
    previous: tuple[float, float],
    current: tuple[float, float],
    previous_event_time: datetime,
    event_time: datetime,
    distance: float,
    move_key: TemporalStateKey,
    policy_revision: str = "v1",
) -> MovementMeasurement:
    """A canonical 15.5.1 measurement fact with a content-derived ID."""
    canonical = "|".join([
        move_key.canonical(),
        previous_event_time.isoformat(),
        event_time.isoformat(),
        f"{distance:.9f}",
    ])
    return MovementMeasurement(
        measurement_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, canonical)),
        fsm_kind="movement",
        key=move_key,
        previous_position=_point(*previous),
        current_position=_point(*current),
        previous_event_time=previous_event_time,
        event_time=event_time,
        distance=distance,
        time_delta_seconds=(event_time - previous_event_time).total_seconds(),
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision=policy_revision,
    )


def _cls_input(
    cls_key: TemporalStateKey,
    *,
    measurement: MovementMeasurement | None,
    event_time: datetime,
    frame_id: FrameId,
    processing_time: datetime | None = None,
) -> MovementClassificationInput:
    return MovementClassificationInput(
        key=cls_key,
        measurement=measurement,
        event_time=event_time,
        frame_id=frame_id,
        processing_time=processing_time or _processing(),
    )


def _classify_engine(
    policy: TemporalPolicy | None = None, **kwargs
) -> MovementClassificationEngine:
    return MovementClassificationEngine(
        fsm=MOVEMENT_CLASSIFICATION_FSM, policy=policy or TemporalPolicy(**kwargs)
    )


def _move_engine(policy: TemporalPolicy | None = None, **kwargs) -> MovementEngine:
    return MovementEngine(fsm=MOVEMENT_FSM, policy=policy or TemporalPolicy(**kwargs))


def _policy(**kwargs) -> TemporalPolicy:
    defaults: dict[str, object] = {
        "movement_enter_threshold": ENTER,
        "movement_exit_threshold": EXIT,
        "movement_qualification_seconds": 0.0,
    }
    defaults.update(kwargs)
    return TemporalPolicy(**defaults)


def _chain(
    engine: MovementClassificationEngine,
    move_engine: MovementEngine,
    *,
    cls_key: TemporalStateKey,
    move_key: TemporalStateKey,
    timeline: tuple[tuple[float, float, int, int], ...],
) -> tuple[MovementClassificationState, list[MovementClassificationTransition | None]]:
    """The sanctioned lockstep wiring: movement engine -> classification.

    Returns (final classification state, per-step transition-or-None) so a
    test can assert both the state timeline AND which step changed it.
    """
    move_state = move_engine.initial_state(move_key)
    state = engine.initial_state(cls_key)
    transitions: list[MovementClassificationTransition | None] = []
    for x, y, seconds, frame_index in timeline:
        obs = _spatial_obs(
            move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
        )
        move_result = move_engine.apply(
            move_state, MovementInput(key=move_key, observation=obs, processing_time=_processing())
        )
        move_state = move_result.state
        inp = classification_input_from_movement(cls_key, obs, move_result, _processing())
        result = engine.apply(state, inp)
        state = result.state
        transitions.append(result.transition)
    return state, transitions


def _classification_states(
    engine: MovementClassificationEngine,
    move_engine: MovementEngine,
    *,
    cls_key: TemporalStateKey,
    move_key: TemporalStateKey,
    timeline: tuple[tuple[float, float, int, int], ...],
) -> list[str]:
    """Return the classification after EVERY step (for state timelines)."""
    move_state = move_engine.initial_state(move_key)
    state = engine.initial_state(cls_key)
    states: list[str] = []
    for x, y, seconds, frame_index in timeline:
        obs = _spatial_obs(
            move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
        )
        move_result = move_engine.apply(
            move_state, MovementInput(key=move_key, observation=obs, processing_time=_processing())
        )
        move_state = move_result.state
        state = engine.apply(
            state, classification_input_from_movement(cls_key, obs, move_result, _processing())
        ).state
        states.append(state.current_state)
    return states


def _pair_keys(**track_overrides) -> tuple[TemporalStateKey, TemporalStateKey]:
    move_key = _key(fsm_kind="movement", **track_overrides)
    cls_key = _key(fsm_kind="movement_classification", **track_overrides)
    return move_key, cls_key


# =============================================================================
# §2. Classification model
# =============================================================================


class TestClassificationModel:
    """UNKNOWN -> valid observations -> STATIONARY / MOVING."""

    def test_only_three_states_exist(self) -> None:
        assert MOVEMENT_STATES == ("unknown", "stationary", "moving")
        assert MOVEMENT_CLASSIFICATION_FSM.states == MOVEMENT_STATES
        assert MOVEMENT_CLASSIFICATION_FSM.initial_state == "unknown"

    def test_initial_state_is_unknown(self) -> None:
        engine = _classify_engine(_policy())
        state = engine.initial_state(_key())
        assert state.current_state == "unknown"
        assert state.state_since is None
        assert state.pending_state is None
        assert state.qualification_started is None

    def test_first_measurement_below_exit_is_stationary(self) -> None:
        engine = _classify_engine(_policy())
        move_engine = _move_engine()
        move_key, cls_key = _pair_keys()
        state, transitions = _chain(
            engine,
            move_engine,
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.11, 0.11, 1, 1)),  # 0.014 < exit
        )
        assert state.current_state == "stationary"
        assert state.state_since == _event(1)
        (transition,) = [t for t in transitions if t is not None]
        assert transition.from_state == "unknown"
        assert transition.to_state == "stationary"
        assert transition.event_time == _event(1)

    def test_first_measurement_above_enter_is_moving(self) -> None:
        engine = _classify_engine(_policy())
        move_engine = _move_engine()
        move_key, cls_key = _pair_keys()
        state, transitions = _chain(
            engine,
            move_engine,
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1)),  # 0.2 > enter
        )
        assert state.current_state == "moving"
        (transition,) = [t for t in transitions if t is not None]
        assert transition.from_state == "unknown"
        assert transition.to_state == "moving"

    def test_moving_means_exceeding_the_movement_policy(self) -> None:
        # MOVING requires the displacement to EXCEED movement_enter_threshold
        # — a pair exactly at the threshold is NOT moving evidence (strictly
        # above). 0.5 is exactly representable, so the boundary is exact.
        policy = _policy(movement_enter_threshold=0.5, movement_exit_threshold=0.25)
        engine = _classify_engine(policy)
        move_engine = _move_engine()
        move_key, cls_key = _pair_keys()
        state, transitions = _chain(
            engine,
            move_engine,
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.25, 0.25, 0, 0), (0.75, 0.25, 1, 1)),  # distance == 0.5
        )
        assert state.current_state == "stationary"  # at the threshold: not moving
        (transition,) = [t for t in transitions if t is not None]
        assert transition.to_state == "stationary"


# =============================================================================
# §3. Movement thresholds are configuration-driven
# =============================================================================


class TestConfigurationDrivenThresholds:
    """The same trajectory classifies differently under different policies."""

    def test_same_trajectory_different_thresholds(self) -> None:
        trajectory = ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1))  # distance 0.4
        strict = _classify_engine(_policy(movement_enter_threshold=0.3))
        lenient = _classify_engine(_policy(movement_enter_threshold=0.5))
        _, s1 = _chain(
            strict,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=trajectory,
        )
        _, s2 = _chain(
            lenient,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=trajectory,
        )
        assert s1[-1].to_state == "moving"
        assert s2[-1].to_state == "stationary"

    def test_qualification_duration_is_configuration_driven(self) -> None:
        # The SAME timeline transitions at a different step under a longer
        # configured qualification window (never hardcoded).
        timeline = ((0.1, 0.1, 0, 0), (0.11, 0.11, 1, 1), (0.3, 0.1, 2, 2), (0.5, 0.1, 4, 4))
        short = _classify_engine(_policy(movement_qualification_seconds=1.0))
        long = _classify_engine(_policy(movement_qualification_seconds=3.0))
        _, short_t = _chain(
            short,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=timeline,
        )
        _, long_t = _chain(
            long,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=timeline,
        )
        short_states = _classification_states(
            short,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=timeline,
        )
        long_states = _classification_states(
            long,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=timeline,
        )
        assert short_states[-1] == "moving"
        assert long_states[-1] == "stationary"  # 2s run < 3s qualification
        assert short_t[-1] is not None
        assert long_t[-1] is None

    def test_policy_rejects_inverted_hysteresis(self) -> None:
        # exit > enter is a contradictory policy — rejected at construction.
        with pytest.raises(ValueError, match="hysteresis"):
            _policy(movement_enter_threshold=0.2, movement_exit_threshold=0.4)


# =============================================================================
# §4/§8. Hysteresis band retains the current state
# =============================================================================


class TestHysteresisBand:
    """Above exit but below enter: retain the existing classification."""

    def _band_chain(self, timeline, *, enter: float = 0.4, exit_: float = 0.2):
        policy = _policy(movement_enter_threshold=enter, movement_exit_threshold=exit_)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        return _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=timeline,
        )

    def test_moving_band_retains_moving(self) -> None:
        # 0.5 (moving), then 0.3 (band: > 0.2 exit, < 0.4 enter) -> MOVING.
        state, transitions = self._band_chain((
            (0.1, 0.1, 0, 0),
            (0.6, 0.1, 1, 1),
            (0.9, 0.1, 2, 2),
        ))
        assert state.current_state == "moving"
        assert state.state_since == _event(1)  # never reset by the band
        assert transitions[2] is None  # no transition on the band step

    def test_stationary_band_retains_stationary(self) -> None:
        # 0.05 (stationary), then 0.3 (band) -> STATIONARY.
        state, transitions = self._band_chain((
            (0.1, 0.1, 0, 0),
            (0.15, 0.1, 1, 1),
            (0.45, 0.1, 2, 2),
        ))
        assert state.current_state == "stationary"
        assert state.state_since == _event(1)
        assert transitions[2] is None

    def test_band_cancels_an_in_progress_qualification_run(self) -> None:
        # STATIONARY, moving evidence starts a qualification run, then a band
        # measurement breaks it — "qualified" means every measurement since
        # the run started sustained it. All distances are exact binary
        # fractions (16ths) so the band boundary is float-noise-free:
        # exit=0.125, enter=0.25, band step = 0.1875.
        policy = _policy(
            movement_enter_threshold=0.25,
            movement_exit_threshold=0.125,
            movement_qualification_seconds=5.0,
        )
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.25, 0.5, 0, 0),  # anchor
            (0.3125, 0.5, 1, 1),  # 0.0625 < exit -> STATIONARY
            (0.625, 0.5, 2, 2),  # 0.3125 > enter -> run starts (pending moving)
            (0.8125, 0.5, 3, 3),  # 0.1875 -> band -> run CANCELLED
        )
        states = _classification_states(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert states == ["unknown", "stationary", "stationary", "stationary"]
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.pending_state is None
        assert state.qualification_started is None


# =============================================================================
# §4/§5/§6. Stable movement (qualification completes at the right event time)
# =============================================================================


class TestStableMovement:
    """STATIONARY -> above-enter evidence sustained -> MOVING."""

    def test_stationary_to_moving_after_qualification(self) -> None:
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),  # anchor
            (0.11, 0.11, 1, 1),  # 0.014 -> STATIONARY at t1
            (0.3, 0.1, 2, 2),  # 0.19 > enter -> run starts at t2
            (0.55, 0.1, 8, 8),  # 0.25 > enter, elapsed 6 >= 5 -> MOVING at t8
        )
        states = _classification_states(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert states == ["unknown", "stationary", "stationary", "moving"]
        state, transitions = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        # The classification did NOT change at t2 (run pending) — it changed
        # at t8, the confirming measurement's event_time.
        assert state.current_state == "moving"
        assert state.state_since == _event(8)
        assert state.qualification_started is None  # run consumed on completion
        confirmations = [t for t in transitions if t is not None]
        assert [t.to_state for t in confirmations] == ["stationary", "moving"]
        moving_transition = confirmations[1]
        assert moving_transition.event_time == _event(8)
        assert moving_transition.qualification_started == _event(2)
        assert moving_transition.from_state == "stationary"
        assert moving_transition.to_state == "moving"

    def test_qualification_requires_sustained_evidence(self) -> None:
        # A single above-enter measurement must NOT flip the state; the
        # state changes only once the evidence is sustained.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),
            (0.11, 0.11, 1, 1),  # STATIONARY
            (0.3, 0.1, 2, 2),  # one moving measurement — pending only
        )
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "stationary"
        assert state.pending_state == "moving"
        assert state.qualification_started == _event(2)

    def test_qualification_seconds_zero_changes_immediately(self) -> None:
        # The degenerate policy: one qualifying measurement flips the state
        # (the foundation's single-threshold behavior, explicit).
        policy = _policy(movement_qualification_seconds=0.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),
            (0.11, 0.11, 1, 1),  # STATIONARY
            (0.3, 0.1, 2, 2),  # moving evidence -> MOVING immediately
        )
        state, transitions = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "moving"
        assert state.pending_state is None
        confirmations = [t for t in transitions if t is not None]
        assert [t.to_state for t in confirmations] == ["stationary", "moving"]
        assert confirmations[1].event_time == _event(2)
        assert confirmations[1].qualification_started is None


# =============================================================================
# §7. Stable stationary (no immediate flip on one noisy observation)
# =============================================================================


class TestStableStationary:
    """MOVING -> below-exit evidence sustained -> STATIONARY."""

    def test_moving_to_stationary_after_qualification(self) -> None:
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),  # anchor
            (0.3, 0.1, 1, 1),  # 0.2 > enter -> MOVING at t1
            (0.31, 0.11, 2, 2),  # 0.014 < exit -> run starts at t2 (state MOVING)
            (0.33, 0.12, 4, 4),  # 0.022 < exit, elapsed 2 < 5 -> still MOVING
            (0.34, 0.13, 8, 8),  # 0.014 < exit, elapsed 6 >= 5 -> STATIONARY at t8
        )
        states = _classification_states(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert states == ["unknown", "moving", "moving", "moving", "stationary"]
        state, transitions = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "stationary"
        assert state.state_since == _event(8)
        confirmations = [t for t in transitions if t is not None]
        assert [t.to_state for t in confirmations] == ["moving", "stationary"]
        stationary_transition = confirmations[1]
        assert stationary_transition.event_time == _event(8)
        assert stationary_transition.qualification_started == _event(2)

    def test_one_noisy_observation_does_not_flip_moving(self) -> None:
        # §7: "Verify state does not immediately flip on one noisy
        # observation" — a single below-exit measurement starts a
        # qualification run; the state remains MOVING.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),
            (0.3, 0.1, 1, 1),  # MOVING
            (0.31, 0.11, 2, 2),  # one noisy stationary measurement
        )
        state, transitions = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "moving"
        assert state.pending_state == "stationary"
        assert transitions[2] is None

    def test_contradicting_evidence_cancels_the_stationary_run(self) -> None:
        # After the stationary run starts, a moving measurement cancels it —
        # the classification never leaves MOVING on ambiguous evidence.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),
            (0.3, 0.1, 1, 1),  # MOVING
            (0.31, 0.11, 2, 2),  # run starts (pending stationary)
            (0.7, 0.1, 3, 3),  # 0.39 > enter -> run cancelled, still MOVING
        )
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "moving"
        assert state.pending_state is None
        assert state.qualification_started is None


# =============================================================================
# §9. Jitter never flaps the classification
# =============================================================================


class TestJitter:
    """Realistic positional noise around a stationary entity."""

    def test_jitter_never_flaps(self) -> None:
        # Noise of +/-0.02 plus occasional 0.05 wobbles: distances stay below
        # the exit threshold or just inside the band — never above enter.
        # With qualification 3s even a short above-enter excursion cannot
        # sustain. Final state deterministic.
        policy = _policy(movement_qualification_seconds=3.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        jitter: list[tuple[float, float]] = []
        x, y = 0.3, 0.3
        for step in range(30):
            jitter.append((x, y))
            x += (-0.02, 0.01, 0.02, -0.01)[step % 4]
            y += (0.01, -0.02, 0.01, 0.02)[step % 4]
        timeline = tuple((px, py, step, step) for step, (px, py) in enumerate(jitter))
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "stationary"
        assert state.pending_state is None  # no run left dangling

    def test_jitter_final_state_is_deterministic(self) -> None:
        policy = _policy(movement_qualification_seconds=3.0)
        jitter = tuple(
            (0.3 + 0.02 * math.sin(step), 0.3 + 0.02 * math.cos(step), step, step)
            for step in range(40)
        )
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state_a, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=jitter
        )
        state_b, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=jitter
        )
        assert state_a == state_b
        assert state_a.current_state == "stationary"


# =============================================================================
# §10. Event-time is authoritative
# =============================================================================


class TestEventTimeAuthoritative:
    """Classification follows event time — never processing order."""

    def _timeline_result(self, processing_times: list[datetime]):
        policy = _policy(movement_qualification_seconds=3.0)
        engine = _classify_engine(policy)
        move_engine = _move_engine()
        move_key, cls_key = _pair_keys()
        # 10:00 stationary anchor, 10:01 stationary, 10:02 moving evidence,
        # 10:05 confirmation -> MOVING at 10:05. Processing times are fed in
        # a deliberately scrambled order.
        steps = [
            ((0.1, 0.1), _event(0), _frame(0)),
            ((0.11, 0.11), _event(1), _frame(1)),
            ((0.3, 0.1), _event(2), _frame(2)),
            ((0.55, 0.1), _event(5), _frame(5)),
        ]
        move_state = move_engine.initial_state(move_key)
        state = engine.initial_state(cls_key)
        transitions: list = []
        for ((x, y), event_time, frame_id), processing_time in zip(
            steps, processing_times, strict=True
        ):
            obs = _spatial_obs(move_key, x=x, y=y, event_time=event_time, frame_id=frame_id)
            move_result = move_engine.apply(
                move_state,
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )
            move_state = move_result.state
            inp = classification_input_from_movement(cls_key, obs, move_result, processing_time)
            result = engine.apply(state, inp)
            state = result.state
            if result.transition is not None:
                transitions.append(result.transition)
        return state, transitions

    def test_scrambled_processing_time_produces_identical_results(self) -> None:
        _, ordered_transitions = self._timeline_result([
            _processing(0),
            _processing(1),
            _processing(2),
            _processing(5),
        ])
        _, scrambled_transitions = self._timeline_result([
            _processing(9000),
            _processing(-9000),
            _processing(0),
            _processing(400),
        ])
        assert ordered_transitions == scrambled_transitions
        state, _ = self._timeline_result([
            _processing(9000),
            _processing(-9000),
            _processing(0),
            _processing(400),
        ])
        assert state.current_state == "moving"
        assert state.state_since == _event(5)

    def test_three_measurements_follow_event_time(self) -> None:
        # 10:00 / 10:01 / 10:02: the classification is driven purely by the
        # event-time deltas (the 1s qualification window completes at 10:03),
        # independent of when the inputs were processed.
        policy = _policy(movement_qualification_seconds=1.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.1, 0.1, 0, 0),  # 10:00 anchor
            (0.11, 0.11, 1, 1),  # 10:01 -> STATIONARY
            (0.3, 0.1, 2, 2),  # 10:02 moving evidence -> run starts
            (0.5, 0.1, 3, 3),  # 10:03 elapsed 1s -> MOVING
        )
        state, transitions = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "moving"
        assert transitions[3] is not None
        assert transitions[3].event_time == _event(3)
        assert transitions[3].to_state == "moving"


# =============================================================================
# §11. Out-of-order policy (15.1, reused)
# =============================================================================


class TestOutOfOrder:
    """Late/out-of-order inputs follow the 15.1 policy — no reordering system."""

    def _seeded_moving_state(self, *, reorder_window: float = 30.0):
        """Classification state at MOVING (watermark 10:02) via direct inputs."""
        engine = _classify_engine(
            _policy(reorder_window_seconds=reorder_window, movement_qualification_seconds=0.0)
        )
        move_key = _key(fsm_kind="movement")
        cls_key = _key()
        state = engine.initial_state(cls_key)
        # 10:00 anchor (no measurement).
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0)),
        ).state
        # 10:02 measurement, distance 0.2 > enter -> MOVING.
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(2),
            distance=0.2,
            move_key=move_key,
        )
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=measurement, event_time=_event(2), frame_id=_frame(2)),
        )
        assert result.state.current_state == "moving"
        return engine, result.state

    def test_older_within_window_is_reordered_not_rewound(self) -> None:
        engine, state = self._seeded_moving_state()
        # 10:01 arrives after 10:02: the input's event_time is older than
        # the watermark but within the 30s window -> reordered, never
        # rewinds the classification.
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.12, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.02,
            move_key=_key(fsm_kind="movement"),
        )
        result = engine.apply(
            state,
            _cls_input(
                cls_key=_key(), measurement=measurement, event_time=_event(1), frame_id=_frame(1)
            ),
        )
        assert result.reordered is True
        assert result.transition is None
        assert result.state.current_state == "moving"
        assert result.state.watermark_event_time == _event(2)  # no rewind

    def test_replayed_reorder_reproduces_reordered(self) -> None:
        engine, state = self._seeded_moving_state()
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.12, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.02,
            move_key=_key(fsm_kind="movement"),
        )
        first = engine.apply(
            state,
            _cls_input(
                cls_key=_key(), measurement=measurement, event_time=_event(1), frame_id=_frame(1)
            ),
        )
        second = engine.apply(
            state,
            _cls_input(
                cls_key=_key(), measurement=measurement, event_time=_event(1), frame_id=_frame(1)
            ),
        )
        assert first.reordered is True
        assert second.reordered is True
        assert second.deduplicated is False

    def test_late_beyond_window_rejected(self) -> None:
        engine, state = self._seeded_moving_state(reorder_window=30.0)
        # 9:58 is 4s late -> within window (reordered, no error); 9:00 is
        # 62s late -> LateEventError.
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.12, 0.1),
            previous_event_time=_event(-60),
            event_time=_event(-59),
            distance=0.02,
            move_key=_key(fsm_kind="movement"),
        )
        with pytest.raises(LateEventError, match="reordering window"):
            engine.apply(
                state,
                _cls_input(
                    cls_key=_key(),
                    measurement=measurement,
                    event_time=_event(-59),
                    frame_id=_frame(-59),
                ),
            )


# =============================================================================
# §12. Duplicate processing is idempotent
# =============================================================================


class TestDuplicateIdempotency:
    """Processing the same measurement twice yields no second transition."""

    def test_same_input_twice_is_deduplicated(self) -> None:
        engine = _classify_engine(_policy())
        move_key = _key(fsm_kind="movement")
        cls_key = _key()
        state = engine.initial_state(cls_key)
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0)),
        ).state
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(2),
            distance=0.2,
            move_key=move_key,
        )
        inp = _cls_input(cls_key, measurement=measurement, event_time=_event(2), frame_id=_frame(2))
        first = engine.apply(state, inp)
        assert first.transition is not None
        assert first.transition.to_state == "moving"
        replay = engine.apply(first.state, inp)
        assert replay.deduplicated is True
        assert replay.transition is None  # no duplicate classification change
        assert replay.state == first.state  # byte-identical state

    def test_replayed_timeline_reproduces_identical_transitions(self) -> None:
        policy = _policy(movement_qualification_seconds=2.0)
        timeline = ((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1), (0.5, 0.1, 3, 3))
        engine = _classify_engine(policy)
        _, t1 = _chain(
            engine,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=timeline,
        )
        _, t2 = _chain(
            engine,
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=timeline,
        )
        assert t1 == t2
        ids1 = [t.transition_id for t in t1 if t is not None]
        ids2 = [t.transition_id for t in t2 if t is not None]
        assert ids1 == ids2  # content-derived: replay is byte-identical


# =============================================================================
# §13. Occlusion never resets the classification
# =============================================================================


class TestOcclusion:
    """Temporary missing observations (no measurement) preserve the state."""

    def _moving_with_pending_stationary(
        self,
    ) -> tuple[MovementClassificationEngine, MovementClassificationState]:
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key = _key(fsm_kind="movement")
        cls_key = _key()
        state = engine.initial_state(cls_key)
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0)),
        ).state
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.2,
            move_key=move_key,
        )
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)),
        ).state  # MOVING
        stationary = _measurement(
            previous=(0.3, 0.1),
            current=(0.31, 0.11),
            previous_event_time=_event(1),
            event_time=_event(2),
            distance=0.014,
            move_key=move_key,
        )
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=stationary, event_time=_event(2), frame_id=_frame(2)),
        ).state  # pending stationary run starts at t2
        return engine, state

    def test_missing_observation_preserves_state_and_run(self) -> None:
        engine, state = self._moving_with_pending_stationary()
        assert state.current_state == "moving"
        assert state.pending_state == "stationary"
        # A measurement-less step (an occlusion gap) at a LATER event time:
        # the classification and the qualification run are preserved.
        result = engine.apply(
            state,
            _cls_input(cls_key=_key(), measurement=None, event_time=_event(9), frame_id=_frame(9)),
        )
        assert result.state.current_state == "moving"
        assert result.state.pending_state == "stationary"
        assert result.state.qualification_started == _event(2)
        assert result.state.watermark_event_time == _event(9)
        assert result.transition is None
        # The preserved run still completes when evidence resumes.
        confirming = _measurement(
            previous=(0.31, 0.11),
            current=(0.32, 0.11),
            previous_event_time=_event(2),
            event_time=_event(9),
            distance=0.01,
            move_key=_key(fsm_kind="movement"),
        )
        result = engine.apply(
            result.state,
            _cls_input(
                cls_key=_key(), measurement=confirming, event_time=_event(9), frame_id=_frame(10)
            ),
        )
        assert result.state.current_state == "stationary"
        assert result.transition is not None
        assert result.transition.to_state == "stationary"

    def test_anchor_gap_does_not_classify(self) -> None:
        # A measurement-less step never fabricates movement — it is a no-op
        # for the classification (the watermark advances only).
        engine = _classify_engine(_policy())
        cls_key = _key()
        state = engine.initial_state(cls_key)
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0)),
        )
        assert result.state.current_state == "unknown"
        assert result.transition is None
        assert result.state.watermark_event_time == _event(0)


# =============================================================================
# §14. State isolation
# =============================================================================


class TestIsolation:
    """Track A MOVING never affects Track B (or any other scope)."""

    def _moving_for(self, engine: MovementClassificationEngine, **overrides) -> tuple[list, list]:
        move_key = _key(fsm_kind="movement", **overrides)
        cls_key = _key(fsm_kind="movement_classification", **overrides)
        _, transitions = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1), (0.5, 0.1, 2, 2)),
        )
        return [t for t in transitions if t is not None], [
            t.key for t in transitions if t is not None
        ]

    def test_track_isolation(self) -> None:
        engine = _classify_engine(_policy())
        other = TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        t_a, _ = self._moving_for(engine)
        t_b, _ = self._moving_for(engine, track_id=other)
        assert t_a[0].key.track_id != t_b[0].key.track_id
        assert t_a[0].transition_id != t_b[0].transition_id

    def test_tenant_isolation(self) -> None:
        engine = _classify_engine(_policy())
        other = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        a, _ = self._moving_for(engine)
        b, _ = self._moving_for(engine, tenant_id=other)
        assert a[0].transition_id != b[0].transition_id

    def test_venue_isolation(self) -> None:
        engine = _classify_engine(_policy())
        other = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        a, _ = self._moving_for(engine)
        b, _ = self._moving_for(engine, venue_id=other)
        assert a[0].transition_id != b[0].transition_id

    def test_session_isolation(self) -> None:
        engine = _classify_engine(_policy())
        other = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        a, _ = self._moving_for(engine)
        b, _ = self._moving_for(engine, session_id=other)
        assert a[0].transition_id != b[0].transition_id

    def test_camera_isolation(self) -> None:
        engine = _classify_engine(_policy())
        other = CameraId(UUID("40000000-0000-0000-0000-000000000099"))
        a, _ = self._moving_for(engine)
        b, _ = self._moving_for(engine, camera_id=other)
        assert a[0].transition_id != b[0].transition_id

    def test_spatial_context_isolation(self) -> None:
        engine = _classify_engine(_policy())
        a, _ = self._moving_for(engine)
        b, _ = self._moving_for(engine, semantic_context="z-lobby")
        assert a[0].key != b[0].key
        assert a[0].transition_id != b[0].transition_id

    def test_same_track_in_two_sessions_is_independent(self) -> None:
        engine = _classify_engine(_policy())
        s1 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000001"))
        s2 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000002"))
        move_key1, _ = _pair_keys(session_id=s1)
        move_key2, _ = _pair_keys(session_id=s2)
        cls1 = _key(session_id=s1)
        cls2 = _key(session_id=s2)
        state1, _ = _chain(
            engine,
            _move_engine(),
            cls_key=cls1,
            move_key=move_key1,
            timeline=((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1)),
        )
        state2, _ = _chain(
            engine,
            _move_engine(),
            cls_key=cls2,
            move_key=move_key2,
            timeline=((0.1, 0.1, 0, 0),),
        )
        assert state1.current_state == "moving"
        assert state2.current_state == "unknown"  # session 2 is independent
        assert state1.key != state2.key

    def test_cross_track_measurement_rejected(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key()
        state = engine.initial_state(cls_key)
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.2,
            move_key=_key(
                fsm_kind="movement",
                track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")),
            ),
        )
        with pytest.raises(StateKeyMismatchError, match="track_id"):
            engine.apply(
                state,
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)
                ),
            )


# =============================================================================
# §15. Configuration provenance (pinned, never "latest")
# =============================================================================


class TestConfigurationProvenance:
    """Every classification preserves the pinned configuration version."""

    def test_transition_carries_policy_revision_and_config_version(self) -> None:
        engine = _classify_engine(_policy(revision="v7"))
        cls_key = _key()
        move_key = _key(fsm_kind="movement")
        _, transitions = _chain(
            engine,
            _move_engine(_policy(revision="v7")),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1)),
        )
        (transition,) = [t for t in transitions if t is not None]
        assert transition.policy_revision == "v7"
        assert transition.key.configuration_version_id == _CONFIG
        assert transition.fsm_version == TEMPORAL_ENGINE_VERSION
        assert transition.fsm_kind == "movement_classification"

    def test_historical_session_keeps_its_pinned_version(self) -> None:
        # A V1 session stays on V1 even after V2 is published — the key
        # carries the pinned configuration version and replay is identical.
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        engine = _classify_engine(_policy(revision="v1"))
        move_engine = _move_engine(_policy(revision="v1"))
        timeline = ((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1))
        move1, _ = _pair_keys(configuration_version_id=v1)
        move2, _ = _pair_keys(configuration_version_id=v2)
        _, t1 = _chain(
            engine,
            move_engine,
            cls_key=_key(configuration_version_id=v1),
            move_key=move1,
            timeline=timeline,
        )
        _, t2 = _chain(
            engine,
            move_engine,
            cls_key=_key(configuration_version_id=v2),
            move_key=move2,
            timeline=timeline,
        )
        _, t1_replay = _chain(
            engine,
            move_engine,
            cls_key=_key(configuration_version_id=v1),
            move_key=move1,
            timeline=timeline,
        )
        assert t1 == t1_replay  # V1 unchanged after V2 exists
        ids1 = [t.transition_id for t in t1 if t is not None]
        ids2 = [t.transition_id for t in t2 if t is not None]
        assert ids1 != ids2  # different configuration -> different identities

    def test_measurement_from_a_different_policy_revision_rejected(self) -> None:
        engine = _classify_engine(_policy(revision="v1"))
        cls_key = _key()
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.2,
            move_key=_key(fsm_kind="movement"),
            policy_revision="v2",  # mismatched configuration
        )
        with pytest.raises(InvalidTemporalInputError, match="policy_revision"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)
                ),
            )


# =============================================================================
# §16. Classification result preserves full provenance
# =============================================================================


class TestResultProvenance:
    """The transition fact carries every canonical scope, never duplicated."""

    def test_transition_preserves_all_provenance(self) -> None:
        policy = _policy(movement_qualification_seconds=2.0)
        engine = _classify_engine(policy)
        move_engine = _move_engine(policy)
        move_key, cls_key = _pair_keys(semantic_context="z-lobby")
        # STATIONARY at t1, moving evidence from t3, qualification completes
        # at t5 (elapsed 2s) -> the MOVING transition carries everything.
        _, transitions = _chain(
            engine,
            move_engine,
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.11, 0.11, 1, 1), (0.3, 0.1, 3, 3), (0.5, 0.1, 5, 5)),
        )
        transitions = [t for t in transitions if t is not None]
        (moving,) = [t for t in transitions if t.to_state == "moving"]
        assert moving.key == cls_key
        assert moving.key.tenant_id == _TENANT
        assert moving.key.venue_id == _VENUE
        assert moving.key.session_id == _SESSION
        assert moving.key.camera_id == _CAMERA
        assert moving.key.configuration_version_id == _CONFIG
        assert moving.key.track_id == _TRACK
        assert moving.key.semantic_context == "z-lobby"
        assert moving.event_time == _event(5)
        assert moving.fsm_kind == "movement_classification"
        assert moving.fsm_version == TEMPORAL_ENGINE_VERSION
        assert moving.policy_revision == "v1"
        # measurement_id references the driving 15.5.1 fact (never duplicated).
        assert moving.measurement_id is not None

    def test_measurement_fact_preserved_on_result(self) -> None:
        policy = _policy()
        engine = _classify_engine(policy)
        move_engine = _move_engine(policy)
        move_key, cls_key = _pair_keys()
        move_state = move_engine.initial_state(move_key)
        state = engine.initial_state(cls_key)
        measurement_result: MovementClassificationResult | None = None
        for x, y, seconds, frame_index in ((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1)):
            obs = _spatial_obs(
                move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            move_result = move_engine.apply(
                move_state,
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )
            move_state = move_result.state
            result = engine.apply(
                state,
                classification_input_from_movement(cls_key, obs, move_result, _processing()),
            )
            state = result.state
            measurement_result = result
        assert measurement_result is not None
        assert measurement_result.measurement is not None
        assert measurement_result.measurement.distance == pytest.approx(0.2)
        assert measurement_result.transition is not None
        assert (
            measurement_result.transition.measurement_id
            == measurement_result.measurement.measurement_id
        )


# =============================================================================
# §17. Checkpoint / restart recovery
# =============================================================================


class TestCheckpoint:
    """Classification state checkpoints and resumes under the 15.1 discipline."""

    TIMELINE = (
        (0.1, 0.1, 0, 0),  # anchor
        (0.11, 0.11, 1, 1),  # -> STATIONARY
        (0.3, 0.1, 2, 2),  # run starts
        (0.55, 0.1, 8, 8),  # -> MOVING
    )

    def test_checkpoint_round_trip(self) -> None:
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=self.TIMELINE
        )
        checkpoint = engine.checkpoint(state)
        data = checkpoint.to_dict()
        assert MovementClassificationCheckpoint.from_dict(data) == checkpoint
        assert engine.restore(checkpoint) == state

    def test_restart_recovery_matches_uninterrupted_processing(self) -> None:
        policy = _policy(movement_qualification_seconds=5.0)
        # Uninterrupted run over the full timeline.
        uninterrupted, _ = _chain(
            _classify_engine(policy),
            _move_engine(),
            cls_key=_key(),
            move_key=_key(fsm_kind="movement"),
            timeline=self.TIMELINE,
        )
        # Interrupted: run the first 3 steps, checkpoint, restore into a
        # FRESH engine, then process the remaining step.
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        move_state = _move_engine().initial_state(move_key)
        state = engine.initial_state(cls_key)
        for x, y, seconds, frame_index in self.TIMELINE[:3]:
            obs = _spatial_obs(
                move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            move_result = _move_engine().apply(
                move_state,
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )
            move_state = move_result.state
            state = engine.apply(
                state, classification_input_from_movement(cls_key, obs, move_result, _processing())
            ).state
        checkpoint = engine.checkpoint(state)
        assert checkpoint.state.pending_state == "moving"  # run mid-flight

        resumed_engine = _classify_engine(policy)
        restored = resumed_engine.restore(checkpoint)
        assert restored == state
        # Continue with the final step (fresh move engine, same move key).
        x, y, seconds, frame_index = self.TIMELINE[3]
        obs = _spatial_obs(
            move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
        )
        move_result = _move_engine().apply(
            move_state, MovementInput(key=move_key, observation=obs, processing_time=_processing())
        )
        final = resumed_engine.apply(
            restored, classification_input_from_movement(cls_key, obs, move_result, _processing())
        ).state
        assert final == uninterrupted
        assert final.current_state == "moving"
        assert final.state_since == _event(8)

    def test_restore_rejects_engine_version_drift(self) -> None:
        engine = _classify_engine(_policy())
        state = engine.initial_state(_key())
        checkpoint = MovementClassificationCheckpoint(
            engine_version="9.9.9", policy_revision="v1", state=state
        )
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            engine.restore(checkpoint)

    def test_restore_rejects_policy_drift(self) -> None:
        engine = _classify_engine(_policy(revision="v2"))
        state = engine.initial_state(_key())
        checkpoint = MovementClassificationCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION, policy_revision="v1", state=state
        )
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            engine.restore(checkpoint)

    def test_restore_rejects_cross_fsm_checkpoint(self) -> None:
        engine = _classify_engine(_policy())
        with pytest.raises(InvalidTemporalInputError, match="MovementClassificationCheckpoint"):
            engine.restore(object())  # type: ignore[arg-type]


# =============================================================================
# §18. Golden scenario
# =============================================================================


class TestGoldenScenario:
    """T1..T8: exact state, transition, and event_time per the configured policy."""

    def test_golden_timeline(self) -> None:
        # enter 0.15 / exit 0.05 / qualification 2s (event time).
        policy = _policy(movement_qualification_seconds=2.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.10, 0.10, 0, 0),  # T1 stationary (anchor, UNKNOWN)
            (0.11, 0.11, 1, 1),  # T2 stationary  -> STATIONARY @t1
            (0.30, 0.10, 2, 2),  # T3 movement begins -> run starts @t2
            (0.50, 0.10, 3, 3),  # T4 movement continues (elapsed 1 < 2)
            (0.72, 0.10, 4, 4),  # T5 movement continues (elapsed 2) -> MOVING @t4
            (0.71, 0.11, 5, 5),  # T6 movement stops -> run starts @t5
            (0.70, 0.11, 6, 6),  # T7 stationary (elapsed 1 < 2)
            (0.70, 0.12, 7, 7),  # T8 stationary (elapsed 2) -> STATIONARY @t7
        )
        states = _classification_states(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert states == [
            "unknown",  # T1
            "stationary",  # T2
            "stationary",  # T3 (qualifying, not yet MOVING)
            "stationary",  # T4 (still qualifying)
            "moving",  # T5
            "moving",  # T6
            "moving",  # T7 (qualifying)
            "stationary",  # T8
        ]
        state, transitions = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        confirmations = [t for t in transitions if t is not None]
        assert [(t.from_state, t.to_state) for t in confirmations] == [
            ("unknown", "stationary"),
            ("stationary", "moving"),
            ("moving", "stationary"),
        ]
        assert [t.event_time for t in confirmations] == [
            _event(1),
            _event(4),
            _event(7),
        ]
        assert [t.qualification_started for t in confirmations] == [
            None,  # first classification: no prior state to protect
            _event(2),  # T3 began the movement run
            _event(5),  # T6 began the stationary run
        ]
        assert state.current_state == "stationary"
        assert state.state_since == _event(7)
        assert state.pending_state is None


# =============================================================================
# §19. Failure tests — never fabricate movement
# =============================================================================


class TestFailureTests:
    """Malformed or contradictory inputs fail explicitly, never repaired."""

    def test_missing_track_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement_classification",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
            )

    def test_missing_session_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement_classification",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_tenant_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement_classification",
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_venue_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement_classification",
                tenant_id=_TENANT,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="movement_classification",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                track_id=_TRACK,
            )

    def test_missing_position_rejected_at_contract(self) -> None:
        # A measurement with no current position is unrepresentable — the
        # movement is never fabricated from a missing observation.
        with pytest.raises(ValueError):
            MovementMeasurement(  # missing current_position / event_time
                measurement_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "m-missing")),
                fsm_kind="movement",
                key=_key(fsm_kind="movement"),
                previous_position=_point(0.1, 0.1),
                previous_event_time=_event(0),
                event_time=_event(1),
                distance=0.2,
                time_delta_seconds=1.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )

    def test_non_finite_measurement_distance_rejected(self) -> None:
        # A NaN displacement is never classified. model_construct bypasses
        # pydantic so the ENGINE's integrity guard is exercised directly.
        engine = _classify_engine(_policy())
        cls_key = _key()
        move_key = _key(fsm_kind="movement")
        bad = MovementMeasurement.model_construct(
            measurement_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "m-nan")),
            fsm_kind="movement",
            key=move_key,
            previous_position=_point(0.1, 0.1),
            current_position=_point(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=float("nan"),
            time_delta_seconds=1.0,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        with pytest.raises(InvalidTemporalInputError, match="finite"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(cls_key, measurement=bad, event_time=_event(1), frame_id=_frame(1)),
            )

    def test_wrong_measurement_fsm_kind_rejected(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key()
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.2,
            move_key=_key(fsm_kind="presence"),  # not a movement measurement
        )
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)
                ),
            )

    def test_measurement_event_time_must_match_input(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key()
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(1),
            distance=0.2,
            move_key=_key(fsm_kind="movement"),
        )
        # Input claims a DIFFERENT event_time than the measurement carries —
        # a mis-wired movement -> classification step.
        with pytest.raises(InvalidTemporalInputError, match="must equal the input event_time"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(2), frame_id=_frame(2)
                ),
            )

    def test_naive_input_event_time_rejected(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key()
        with pytest.raises(InvalidTemporalInputError, match="timezone-aware"):
            engine.apply(
                engine.initial_state(cls_key),
                MovementClassificationInput(
                    key=cls_key,
                    measurement=None,
                    event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
                    frame_id=_frame(0),
                    processing_time=_processing(),
                ),
            )

    def test_naive_processing_time_rejected(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key()
        with pytest.raises(InvalidTemporalInputError, match="timezone-aware"):
            engine.apply(
                engine.initial_state(cls_key),
                MovementClassificationInput(
                    key=cls_key,
                    measurement=None,
                    event_time=_event(0),
                    frame_id=_frame(0),
                    processing_time=datetime(2026, 8, 1, 11, 0, 0),  # naive
                ),
            )

    def test_backwards_timestamp_rejected_at_contract(self) -> None:
        # event_time before previous_event_time is not a previous->current
        # pair — the field constraint (positive delta) passes so the model
        # invariant fires; rejected, never clamped.
        with pytest.raises(ValueError, match="must not precede"):
            MovementMeasurement(
                measurement_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "m-backwards")),
                fsm_kind="movement",
                key=_key(fsm_kind="movement"),
                previous_position=_point(0.1, 0.1),
                current_position=_point(0.3, 0.1),
                previous_event_time=_event(1),
                event_time=_event(0),  # backwards
                distance=0.2,
                time_delta_seconds=1.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )

    def test_equal_timestamps_are_valid_with_later_frame(self) -> None:
        # Two observations at the SAME event_time (later frame id): the pair
        # is valid, time_delta is 0, and classification still applies.
        policy = _policy()
        engine = _classify_engine(policy)
        cls_key = _key()
        state = engine.initial_state(cls_key)
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0)),
        ).state
        measurement = _measurement(
            previous=(0.1, 0.1),
            current=(0.3, 0.1),
            previous_event_time=_event(0),
            event_time=_event(0),
            distance=0.2,
            move_key=_key(fsm_kind="movement"),
        )
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=measurement, event_time=_event(0), frame_id=_frame(1)),
        )
        assert result.state.current_state == "moving"
        assert result.measurement is not None
        assert result.measurement.time_delta_seconds == pytest.approx(0.0)

    def test_out_of_range_normalized_point_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            _spatial_obs(_key(), x=1.5, y=0.5, event_time=_event(0), frame_id=_frame(0))

    def test_non_finite_point_rejected_at_geometry_boundary(self) -> None:
        # The movement engine re-asserts the geometry boundary; a NaN point
        # is never measured and therefore never classified.
        move_engine = _move_engine()
        move_key = _key(fsm_kind="movement")
        obs = SpatialObservation(
            session_id=move_key.session_id,
            track_id=move_key.track_id,
            frame_id=_frame(0),
            event_time=_event(0),
            camera_id=move_key.camera_id,
            configuration_version_id=move_key.configuration_version_id,
            spatial_point=SpatialPointModel(
                x=float("nan"),
                y=0.5,
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                policy=SpatialPointPolicy.FOOTPOINT,
            ),
            status=SpatialStatus.INSIDE,
        )
        with pytest.raises(InvalidCoordinateError):
            move_engine.apply(
                move_engine.initial_state(move_key),
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )

    def test_non_classification_key_rejected(self) -> None:
        engine = _classify_engine(_policy())
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.initial_state(_key(fsm_kind="presence"))

    def test_input_key_must_match_state_key(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key()
        other_key = _key(track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")))
        with pytest.raises(InvalidTemporalInputError, match="must match the state key"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(
                    other_key,
                    measurement=None,
                    event_time=_event(0),
                    frame_id=_frame(0),
                ),
            )


# =============================================================================
# §20. Bounded state / performance
# =============================================================================


class TestBoundedState:
    """Classification retains only what future steps need — no history."""

    def test_state_is_scalar_bounded_by_construction(self) -> None:
        # The classification state holds scalars + the nested key only — no
        # per-observation history, so a long session cannot grow it.
        engine = _classify_engine(_policy())
        state = engine.initial_state(_key())
        for field in MovementClassificationState.model_fields:
            value = getattr(state, field)
            if field == "key":
                continue
            assert not isinstance(value, (list, tuple, dict, set, frozenset)), (
                f"classification state must not accumulate {field}"
            )

    def test_long_stream_stays_bounded_and_deterministic(self) -> None:
        policy = _policy(movement_qualification_seconds=2.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        # An oscillating track: every step is a 0.4 displacement (sustained
        # moving evidence) — 200 steps, bounded state throughout.
        timeline = tuple((0.2 if step % 2 == 0 else 0.6, 0.5, step, step) for step in range(200))
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "moving"
        # Bounded: the state carries no growing collections after 200 steps.
        for field in MovementClassificationState.model_fields:
            value = getattr(state, field)
            if field == "key":
                continue
            assert not isinstance(value, (list, tuple, dict, set, frozenset))


# =============================================================================
# §21. Pure core
# =============================================================================


class TestClassificationPurity:
    """The classification core performs no I/O and reads no current time."""

    def test_classification_core_is_pure(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
        text = (package_dir / "classification.py").read_text()
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
                f"I/O/stateful module {module!r} leaked into classification.py"
            )
        assert "now(" not in text
        assert "utc_now" not in text
        assert "print(" not in text
        assert "datetime.now" not in text
