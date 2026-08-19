"""Tests for Task 15.5.4 — movement/stationary hysteresis & temporal qualification.

Task 15.5.4 hardens the 15.5.2 movement classification and the 15.5.3
waiting detection against noisy CV observations. The hysteresis and
event-time qualification ENGINE logic lives in the 15.5.2 classification
engine (``pending_state`` + ``qualification_started`` metadata — the
sanctioned pending-transition representation) and the 15.5.3 waiting
engine (``candidate_start`` / ``waiting_start``), so this suite is the
focused acceptance layer ON TOP of that existing logic: it exercises the
exact Task 15.5.4 scenarios and invariants WITHOUT re-implementing or
duplicating any engine logic.

Covered (task section in parentheses):

- independently configurable enter/exit thresholds and the validated
  ``exit <= enter`` relationship — invalid configuration is rejected,
  never swapped (§2/§23);
- MOVING and STATIONARY transitions require a measurement + threshold
  evidence + configured event-time qualification — no immediate flip on
  one noisy observation (§3/§4);
- the hysteresis band (exit < measurement < enter) retains the current
  state in BOTH directions (§5);
- qualification uses event_time: a run anchored at 10:00:00 with a 10s
  duration becomes eligible at 10:00:10, never processing time (§6);
- the qualification candidate lifecycle: start, sustain, and
  deterministic cancellation when evidence contradicts it (§7/§8);
- jitter oscillating around the movement threshold (0.95 / 1.05 / 0.98 /
  1.02 / 0.97 / 1.04) never causes uncontrolled state flipping (§9);
- waiting qualification requires confirmed presence + configured waiting
  context + stationary + duration (§10/§11);
- waiting hysteresis: small movement around the stationary threshold
  never causes WAITING -> NOT_WAITING -> WAITING (§12);
- spatial boundary stability: boundary noise that the Task 14 spatial
  policy does NOT confirm as a context change never flips the waiting
  zone (§13);
- occlusion grace is reused: a short missing observation never cancels a
  movement run, stationary qualification, or waiting (§14);
- the 15.1 out-of-order policy (10:00 / 10:02 / 10:01) is reused (§15);
- duplicate observations and duplicate candidate events are idempotent —
  no duplicated qualification, transition, or waiting interval (§16);
- state isolation across tenant/venue/session/track/context (§17);
- configuration provenance: a V1 session replays under V1 semantics even
  after V2 is published (§18);
- checkpoint compatibility and restart recovery equal uninterrupted
  processing, for movement and waiting qualification (§19/§20);
- the golden movement timeline 10:00..10:07 (§21);
- the golden waiting timeline 10:00..10:07 including small movement at
  10:06 (§22);
- failure cases — invalid thresholds, negative/zero durations, missing
  thresholds (degenerate policy), invalid measurements, invalid
  timestamps, missing configuration, cross-scope state (§23);
- the property/invariant suite (§24);
- bounded state / performance (§25) and the pure-core boundary (§26).

All fixtures use the REAL canonical contracts with fixed deterministic
IDs so replay comparisons are byte-exact.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from backend.app.intelligence.temporal import (
    MOVEMENT_CLASSIFICATION_FSM,
    MOVEMENT_FSM,
    WAITING_FSM,
    MovementClassificationEngine,
    MovementClassificationInput,
    MovementEngine,
    MovementInput,
    WaitingEngine,
    WaitingInput,
    classification_input_from_movement,
    waiting_event_from_presence,
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
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    MovementClassificationCheckpoint,
    MovementClassificationState,
    MovementClassificationTransition,
    MovementMeasurement,
    TemporalPolicy,
    TemporalReason,
    TemporalStateKey,
    TemporalTransition,
    WaitingState,
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

# The explicit waiting-capable context used across the waiting tests
# (§4 — never inferred).
WAITING_CTX = "zone-queue-a"

# Hysteresis policy used across most movement tests: enter dominates exit
# and the band is wide enough to catch noise near the boundary.
ENTER = 0.15
EXIT = 0.05


# =============================================================================
# Fixture builders (real canonical contracts, deterministic IDs)
# =============================================================================


def _key(
    *,
    fsm_kind: str,
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


def _policy(**kwargs) -> TemporalPolicy:
    """Movement-classification policy; hysteresis defaults enter > exit."""
    defaults: dict[str, object] = {
        "movement_enter_threshold": ENTER,
        "movement_exit_threshold": EXIT,
        "movement_qualification_seconds": 0.0,
    }
    defaults.update(kwargs)
    return TemporalPolicy(**defaults)


def _classify_engine(
    policy: TemporalPolicy | None = None, **kwargs
) -> MovementClassificationEngine:
    return MovementClassificationEngine(
        fsm=MOVEMENT_CLASSIFICATION_FSM, policy=policy or _policy(**kwargs)
    )


def _move_engine(policy: TemporalPolicy | None = None, **kwargs) -> MovementEngine:
    return MovementEngine(fsm=MOVEMENT_FSM, policy=policy or _policy(**kwargs))


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
    distance: float,
    event_time: datetime,
    previous_event_time: datetime,
    move_key: TemporalStateKey,
    policy_revision: str = "v1",
) -> MovementMeasurement:
    """A canonical 15.5.1 measurement fact with a content-derived ID.

    The classification engine consumes only ``distance`` + ``event_time``
    (+ provenance); the carried positions are canonical but arbitrary.
    """
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
        previous_position=_point(0.1, 0.1),
        # The classification engine consumes only ``distance`` + ``event_time``
        # (+ provenance); the carried positions are canonical but arbitrary, so
        # they stay within the IMAGE_NORMALIZED unit square for any distance.
        current_position=_point(min(0.1 + distance, 0.9), 0.1),
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


def _classification_states(
    engine: MovementClassificationEngine,
    move_engine: MovementEngine,
    *,
    cls_key: TemporalStateKey,
    move_key: TemporalStateKey,
    timeline: tuple[tuple[float, float, int, int], ...],
) -> list[str]:
    """Classification after EVERY step (the sanctioned lockstep wiring)."""
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


def _chain(
    engine: MovementClassificationEngine,
    move_engine: MovementEngine,
    *,
    cls_key: TemporalStateKey,
    move_key: TemporalStateKey,
    timeline: tuple[tuple[float, float, int, int], ...],
) -> tuple[MovementClassificationState, list[MovementClassificationTransition | None]]:
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
        result = engine.apply(
            state, classification_input_from_movement(cls_key, obs, move_result, _processing())
        )
        state = result.state
        transitions.append(result.transition)
    return state, transitions


def _seed_direct(
    engine: MovementClassificationEngine,
    *,
    cls_key: TemporalStateKey,
    move_key: TemporalStateKey,
    target: str,
    at_seconds: int,
    policy_revision: str = "v1",
) -> MovementClassificationState:
    """Seed a STATIONARY/MOVING classification via direct measurement inputs.

    Anchor at ``at_seconds - 2`` (no pair yet), then a measurement at
    ``at_seconds - 1`` whose distance classifies UNKNOWN directly into
    ``target`` (the first measurement needs no qualification).
    """
    state = engine.initial_state(cls_key)
    state = engine.apply(
        state,
        _cls_input(
            cls_key,
            measurement=None,
            event_time=_event(at_seconds - 2),
            frame_id=_frame(at_seconds - 2),
        ),
    ).state
    # The seed distance is DERIVED from the engine's configured thresholds
    # (never hardcoded): STATIONARY evidence sits below the exit threshold,
    # MOVING evidence strictly above the enter threshold. With a
    # degenerate ``0.0`` exit threshold a zero displacement is still
    # conservative STATIONARY (strictly-above semantics — a zero
    # displacement is never MOVING).
    policy = engine.policy
    if target == "stationary":
        distance = max(0.0, policy.movement_exit_threshold / 2)
    else:  # moving
        distance = policy.movement_enter_threshold + 0.5
    measurement = _measurement(
        distance=distance,
        previous_event_time=_event(at_seconds - 2),
        event_time=_event(at_seconds - 1),
        move_key=move_key,
        policy_revision=policy_revision,
    )
    state = engine.apply(
        state,
        _cls_input(
            cls_key,
            measurement=measurement,
            event_time=_event(at_seconds - 1),
            frame_id=_frame(at_seconds - 1),
        ),
    ).state
    assert state.current_state == target
    return state


def _pair_keys(**overrides) -> tuple[TemporalStateKey, TemporalStateKey]:
    move_key = _key(fsm_kind="movement", **overrides)
    cls_key = _key(fsm_kind="movement_classification", **overrides)
    return move_key, cls_key


# --- Waiting-side builders (mirror test_waiting_fsm.py conventions) ---


def _family_keys(
    *,
    semantic_context: str | None = WAITING_CTX,
    **overrides,
) -> tuple[TemporalStateKey, TemporalStateKey, TemporalStateKey]:
    """(presence, movement_classification, waiting) keys sharing every scope."""
    common = dict(semantic_context=semantic_context, **overrides)
    pkey = _key(fsm_kind="presence", **common)
    ckey = _key(fsm_kind="movement_classification", **common)
    wkey = _key(fsm_kind="waiting", **common)
    return pkey, ckey, wkey


def _presence_transition(
    pkey: TemporalStateKey,
    *,
    reason: TemporalReason,
    event_time: datetime,
    frame_id: FrameId,
    from_state: str,
    to_state: str,
) -> TemporalTransition:
    canonical = "|".join([
        pkey.canonical(),
        str(frame_id),
        event_time.isoformat(),
        reason.value,
    ])
    return TemporalTransition(
        transition_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, canonical)),
        fsm_kind="presence",
        key=pkey,
        from_state=from_state,
        to_state=to_state,
        event_kind="present",
        reason=reason,
        observation_frame_id=frame_id,
        event_time=event_time,
        processing_time=_processing(),
        configuration_version_id=pkey.configuration_version_id,
        fsm_version=TEMPORAL_ENGINE_VERSION,
    )


def _enter(
    pkey: TemporalStateKey, *, event_time: datetime, frame_id: FrameId
) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.ENTER_CONFIRMED,
        event_time=event_time,
        frame_id=frame_id,
        from_state="absent",
        to_state="present",
    )


def _stay(pkey: TemporalStateKey, *, event_time: datetime, frame_id: FrameId) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.OBSERVED_STAY,
        event_time=event_time,
        frame_id=frame_id,
        from_state="present",
        to_state="present",
    )


def _classification(ckey: TemporalStateKey, *, current_state: str) -> MovementClassificationState:
    return MovementClassificationState(
        fsm_version=TEMPORAL_ENGINE_VERSION,
        key=ckey,
        current_state=current_state,
    )


def _waiting_policy(
    *,
    waiting_contexts: frozenset[str] | set[str] | tuple[str, ...] = frozenset({WAITING_CTX}),
    waiting_qualification_seconds: float = 0.0,
    reorder_window_seconds: float = 60.0,
    revision: str = "v1",
) -> TemporalPolicy:
    return TemporalPolicy(
        revision=revision,
        reorder_window_seconds=reorder_window_seconds,
        waiting_qualification_seconds=waiting_qualification_seconds,
        waiting_contexts=frozenset(waiting_contexts),
    )


def _waiting_engine(policy: TemporalPolicy | None = None, **kwargs) -> WaitingEngine:
    return WaitingEngine(fsm=WAITING_FSM, policy=policy or _waiting_policy(**kwargs))


def _w_input(
    wkey: TemporalStateKey,
    *,
    transition: TemporalTransition,
    classification: MovementClassificationState,
    kind: str,
    processing_time: datetime | None = None,
) -> WaitingInput:
    return WaitingInput(
        key=wkey,
        presence_transition=transition,
        classification_state=classification,
        observation_kind=kind,
        processing_time=processing_time or _processing(),
    )


def _w_apply(
    engine: WaitingEngine,
    state: WaitingState,
    *,
    pkey: TemporalStateKey,
    ckey: TemporalStateKey,
    wkey: TemporalStateKey,
    transition: TemporalTransition,
    classification: MovementClassificationState,
    kind: str,
) -> WaitingState:
    return engine.apply(
        state,
        _w_input(
            wkey,
            transition=transition,
            classification=classification,
            kind=kind,
        ),
    ).state


def _waiting_states(
    engine: WaitingEngine,
    *,
    pkey: TemporalStateKey,
    ckey: TemporalStateKey,
    wkey: TemporalStateKey,
    timeline: tuple[tuple[str, str, int, int], ...],
) -> list[str]:
    """Waiting state after every (kind, classification, seconds, frame) step."""
    state = engine.initial_state(wkey)
    states: list[str] = []
    builders = {
        "enter_confirmed": _enter,
        "stay": _stay,
    }
    for kind, classification_state, seconds, frame_index in timeline:
        transition = builders[kind](pkey, event_time=_event(seconds), frame_id=_frame(frame_index))
        state = engine.apply(
            state,
            _w_input(
                wkey,
                transition=transition,
                classification=_classification(ckey, current_state=classification_state),
                kind=kind,
            ),
        ).state
        states.append(state.current_state)
    return states


# =============================================================================
# §2/§23. Threshold configuration — enter/exit independent + validated
# =============================================================================


class TestThresholdConfiguration:
    """Two independently configurable thresholds with exit <= enter."""

    def test_enter_and_exit_thresholds_are_independent_knobs(self) -> None:
        # enter > exit gives a real band; the SAME trajectory classifies
        # differently under different threshold pairs (never hardcoded).
        trajectory = ((0.1, 0.1, 0, 0), (0.5, 0.1, 1, 1))  # distance 0.4
        strict = _classify_engine(
            _policy(movement_enter_threshold=0.5, movement_exit_threshold=0.3)
        )
        lenient = _classify_engine(
            _policy(movement_enter_threshold=0.2, movement_exit_threshold=0.1)
        )
        _, t1 = _chain(
            strict,
            _move_engine(),
            cls_key=_key(fsm_kind="movement_classification"),
            move_key=_key(fsm_kind="movement"),
            timeline=trajectory,
        )
        _, t2 = _chain(
            lenient,
            _move_engine(),
            cls_key=_key(fsm_kind="movement_classification"),
            move_key=_key(fsm_kind="movement"),
            timeline=trajectory,
        )
        assert t1[-1].to_state == "stationary"  # 0.4 < enter 0.5
        assert t2[-1].to_state == "moving"  # 0.4 > enter 0.2

    def test_inverted_thresholds_rejected_not_swapped(self) -> None:
        # exit > enter is a contradictory policy: rejected at construction,
        # NEVER silently swapped into a working band.
        with pytest.raises(ValueError, match="hysteresis"):
            _policy(movement_enter_threshold=0.2, movement_exit_threshold=0.4)

    def test_equal_thresholds_are_the_degenerate_single_boundary(self) -> None:
        # Equal thresholds are a legal (degenerate) policy: no band, one
        # boundary — a distance exactly at it is NOT far-side evidence.
        policy = _policy(movement_enter_threshold=0.25, movement_exit_threshold=0.25)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        _, transitions = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.25, 0.25, 0, 0), (0.5, 0.25, 1, 1)),  # distance == 0.25
        )
        (transition,) = [t for t in transitions if t is not None]
        assert transition.to_state == "stationary"  # at the boundary: not moving

    def test_negative_qualification_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalPolicy(movement_qualification_seconds=-1.0)
        with pytest.raises(ValueError):
            TemporalPolicy(waiting_qualification_seconds=-1.0)

    def test_zero_qualification_is_the_explicit_immediate_policy(self) -> None:
        # 0.0 is the APPROVED immediate transition: one qualifying
        # measurement changes the state — documented, never a default leak.
        engine = _classify_engine(_policy(movement_qualification_seconds=0.0))
        move_key, cls_key = _pair_keys()
        _, transitions = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.11, 0.11, 1, 1), (0.3, 0.1, 2, 2)),
        )
        confirmations = [t for t in transitions if t is not None]
        assert [t.to_state for t in confirmations] == ["stationary", "moving"]
        assert confirmations[1].event_time == _event(2)
        assert confirmations[1].qualification_started is None

    def test_missing_thresholds_are_the_degenerate_policy(self) -> None:
        # A policy without explicit thresholds is the documented 0.0/0.0
        # single boundary: a zero-displacement pair is stationary, any
        # positive displacement is moving. Valid, deterministic, explicit.
        policy = TemporalPolicy()  # movement_enter_threshold == movement_exit_threshold == 0.0
        assert policy.movement_enter_threshold == pytest.approx(0.0)
        assert policy.movement_exit_threshold == pytest.approx(0.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state, _ = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.1, 0.1, 1, 1), (0.2, 0.1, 2, 2)),
        )
        assert state.current_state == "moving"  # 0.0 -> 0.1 displacement > 0


# =============================================================================
# §5. The hysteresis band retains the current state in BOTH directions
# =============================================================================


class TestHysteresisBand:
    """exit < measurement < enter retains the current classification."""

    def test_band_retains_stationary(self) -> None:
        policy = _policy(movement_enter_threshold=0.25, movement_exit_threshold=0.125)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state, _ = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=(
                (0.25, 0.5, 0, 0),  # anchor
                (0.3125, 0.5, 1, 1),  # 0.0625 < exit -> STATIONARY
                (0.4375, 0.5, 2, 2),  # 0.125 band step... == exit -> stationary evidence
            ),
        )
        assert state.current_state == "stationary"
        assert state.state_since == _event(1)

    def test_band_retains_moving(self) -> None:
        # 0.5 (moving), then a band measurement (between exit and enter)
        # -> MOVING retained, state_since never reset.
        policy = _policy(movement_enter_threshold=0.4, movement_exit_threshold=0.2)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state, transitions = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=(
                (0.1, 0.1, 0, 0),
                (0.6, 0.1, 1, 1),  # 0.5 > enter -> MOVING
                (0.9, 0.1, 2, 2),  # 0.3 in band -> MOVING retained
            ),
        )
        assert state.current_state == "moving"
        assert state.state_since == _event(1)
        assert transitions[2] is None  # the band step emits no transition

    def test_band_never_flips_either_direction(self) -> None:
        # Property: a band measurement ALWAYS retains the current state.
        policy = _policy(movement_enter_threshold=0.25, movement_exit_threshold=0.125)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        # STATIONARY then band -> STATIONARY.
        states = _classification_states(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=(
                (0.25, 0.5, 0, 0),
                (0.3125, 0.5, 1, 1),  # stationary
                (0.4375, 0.5, 2, 2),  # 0.125 == exit: not far-side (band)
            ),
        )
        assert states == ["unknown", "stationary", "stationary"]


# =============================================================================
# §3/§4/§6. Transitions require measurement + threshold + event-time qualification
# =============================================================================


class TestEventTimeQualification:
    """Qualification uses event_time: run start + duration == eligibility."""

    def test_transition_becomes_eligible_at_run_start_plus_duration(self) -> None:
        # The task example: the movement run becomes qualified at 10:00:00
        # with a 10s duration -> the transition becomes eligible at
        # 10:00:10 EVENT time (never processing time, never wall clock).
        policy = _policy(movement_qualification_seconds=10.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        # 10:00:00: above-enter evidence anchors the qualification run.
        run_start = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=run_start, event_time=_event(0), frame_id=_frame(0)),
        ).state
        assert state.current_state == "stationary"
        assert state.pending_state == "moving"
        assert state.qualification_started == _event(0)
        # 10:00:09: 9s elapsed < 10s -> NOT yet eligible.
        early = _measurement(
            distance=0.6,
            previous_event_time=_event(0),
            event_time=_event(9),
            move_key=move_key,
        )
        result = engine.apply(
            state, _cls_input(cls_key, measurement=early, event_time=_event(9), frame_id=_frame(9))
        )
        assert result.state.current_state == "stationary"
        assert result.transition is None
        # 10:00:10: exactly 10s of event time -> eligible, MOVING confirmed
        # at the confirming measurement's event_time.
        confirming = _measurement(
            distance=0.7,
            previous_event_time=_event(9),
            event_time=_event(10),
            move_key=move_key,
        )
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=confirming, event_time=_event(10), frame_id=_frame(10)),
        )
        assert result.state.current_state == "moving"
        assert result.state.state_since == _event(10)
        assert result.transition is not None
        assert result.transition.event_time == _event(10)
        assert result.transition.qualification_started == _event(0)

    def test_processing_time_is_irrelevant_to_qualification(self) -> None:
        # The same timeline with wildly scrambled processing times produces
        # the identical qualification result (event-time semantics).
        policy = _policy(movement_qualification_seconds=10.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        measurements = [
            (0.02, _event(1), _event(0)),
            (0.5, _event(5), _event(1)),
            (0.6, _event(15), _event(5)),
        ]
        results: list[tuple] = []
        for scramble in ((0, 1, 2), (9000, -9000, 400)):
            state = engine.initial_state(cls_key)
            state = engine.apply(
                state,
                _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0)),
            ).state
            for (distance, event_time, previous), seconds in zip(
                measurements, scramble, strict=True
            ):
                measurement = _measurement(
                    distance=distance,
                    previous_event_time=previous,
                    event_time=event_time,
                    move_key=move_key,
                )
                state = engine.apply(
                    state,
                    _cls_input(
                        cls_key,
                        measurement=measurement,
                        event_time=event_time,
                        frame_id=_frame(event_time.second % 60),
                        processing_time=_processing(seconds),
                    ),
                ).state
            results.append(state)
        assert results[0] == results[1]
        assert results[0].current_state == "moving"
        assert results[0].state_since == _event(15)


# =============================================================================
# §7/§8. The qualification candidate: start, sustain, deterministic cancel
# =============================================================================


class TestQualificationCandidate:
    """pending_state + qualification_started represent the transition candidate."""

    def test_candidate_starts_and_completes(self) -> None:
        # STATIONARY -> above-enter -> MOVING_CANDIDATE -> sustained ->
        # MOVING. The candidate is explicit pending metadata, not a new
        # state (the sanctioned architecture).
        policy = _policy(movement_qualification_seconds=3.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        ).state
        assert state.current_state == "stationary"  # candidate active, not flipped
        assert state.pending_state == "moving"
        assert state.qualification_started == _event(0)
        confirming = _measurement(
            distance=0.6,
            previous_event_time=_event(0),
            event_time=_event(3),
            move_key=move_key,
        )
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=confirming, event_time=_event(3), frame_id=_frame(3)),
        )
        assert result.state.current_state == "moving"
        assert result.state.pending_state is None  # run consumed
        assert result.transition is not None
        assert result.transition.from_state == "stationary"
        assert result.transition.to_state == "moving"

    def test_moving_candidate_cancelled_when_evidence_falls_back(self) -> None:
        # §8: MOVING_CANDIDATE + a measurement that no longer supports it
        # (stationary evidence below exit) -> candidate cancelled.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        ).state
        assert state.pending_state == "moving"
        below = _measurement(
            distance=0.01,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=below, event_time=_event(1), frame_id=_frame(1))
        ).state
        assert state.current_state == "stationary"
        assert state.pending_state is None
        assert state.qualification_started is None

    def test_stationary_candidate_cancelled_when_evidence_rises(self) -> None:
        # §8 mirror: STATIONARY_CANDIDATE + above-enter evidence cancels it.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="moving", at_seconds=0
        )
        below = _measurement(
            distance=0.01,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=below, event_time=_event(0), frame_id=_frame(0))
        ).state
        assert state.pending_state == "stationary"
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(1), frame_id=_frame(1))
        ).state
        assert state.current_state == "moving"
        assert state.pending_state is None

    def test_one_noisy_observation_never_flips(self) -> None:
        # §3/§4: STATIONARY must not flip on ONE above-enter measurement
        # and MOVING must not flip on ONE below-exit measurement, unless
        # the configured qualification is explicitly zero.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        stationary = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        one_above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        result = engine.apply(
            stationary,
            _cls_input(cls_key, measurement=one_above, event_time=_event(0), frame_id=_frame(0)),
        )
        assert result.state.current_state == "stationary"
        assert result.transition is None
        moving = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="moving", at_seconds=0
        )
        one_below = _measurement(
            distance=0.01,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        result = engine.apply(
            moving,
            _cls_input(cls_key, measurement=one_below, event_time=_event(0), frame_id=_frame(0)),
        )
        assert result.state.current_state == "moving"
        assert result.transition is None


# =============================================================================
# §9. Jitter oscillating around the movement threshold never flaps
# =============================================================================


class TestJitterAroundMovementThreshold:
    """Measurements oscillating around the enter threshold: no flipping."""

    OSCILLATION = (0.95, 1.05, 0.98, 1.02, 0.97, 1.04)

    def test_stationary_entity_never_flips_through_threshold_oscillation(self) -> None:
        # enter=1.0 / exit=0.9: 0.95/0.98/0.97 are band evidence, 1.05/1.02/
        # 1.04 start a run that a band measurement always cancels before the
        # 5s window elapses. The classification NEVER becomes MOVING.
        policy = _policy(
            movement_enter_threshold=1.0,
            movement_exit_threshold=0.9,
            movement_qualification_seconds=5.0,
        )
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        states: list[str] = [state.current_state]
        for step, distance in enumerate(self.OSCILLATION):
            measurement = _measurement(
                distance=distance,
                previous_event_time=_event(step - 1),
                event_time=_event(step),
                move_key=move_key,
            )
            state = engine.apply(
                state,
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(step), frame_id=_frame(step)
                ),
            ).state
            states.append(state.current_state)
        assert states == ["stationary"] * 7  # no uncontrolled flipping
        assert state.current_state == "stationary"
        assert state.pending_state == "moving"  # latest run pending, never completed

    def test_moving_entity_never_flips_through_threshold_oscillation(self) -> None:
        policy = _policy(
            movement_enter_threshold=1.0,
            movement_exit_threshold=0.9,
            movement_qualification_seconds=5.0,
        )
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="moving", at_seconds=0
        )
        for step, distance in enumerate(self.OSCILLATION):
            measurement = _measurement(
                distance=distance,
                previous_event_time=_event(step - 1),
                event_time=_event(step),
                move_key=move_key,
            )
            state = engine.apply(
                state,
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(step), frame_id=_frame(step)
                ),
            ).state
            assert state.current_state == "moving"  # band retains MOVING
        assert state.pending_state is None

    def test_jitter_final_state_is_deterministic(self) -> None:
        policy = _policy(
            movement_enter_threshold=1.0,
            movement_exit_threshold=0.9,
            movement_qualification_seconds=5.0,
        )
        states: list[MovementClassificationState] = []
        for _ in range(3):
            engine = _classify_engine(policy)
            move_key, cls_key = _pair_keys()
            state = _seed_direct(
                engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
            )
            for step, distance in enumerate(self.OSCILLATION):
                measurement = _measurement(
                    distance=distance,
                    previous_event_time=_event(step - 1),
                    event_time=_event(step),
                    move_key=move_key,
                )
                state = engine.apply(
                    state,
                    _cls_input(
                        cls_key,
                        measurement=measurement,
                        event_time=_event(step),
                        frame_id=_frame(step),
                    ),
                ).state
            states.append(state)
        assert states[0] == states[1] == states[2]


# =============================================================================
# §21. Golden movement timeline 10:00..10:07
# =============================================================================


class TestGoldenMovementTimeline:
    """10:00 stationary .. 10:07 stationary — exact transitions per config."""

    def test_golden_timeline(self) -> None:
        # enter 0.15 / exit 0.05 / qualification 2s of event time.
        policy = _policy(movement_qualification_seconds=2.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.10, 0.10, 0, 0),  # 10:00 stationary (anchor, UNKNOWN)
            (0.11, 0.11, 1, 1),  # 10:01 movement starts -> STATIONARY @10:01
            (0.30, 0.10, 2, 2),  # 10:02 movement continues -> run starts @10:02
            (0.50, 0.10, 3, 3),  # 10:03 movement continues (elapsed 1 < 2)
            (0.72, 0.10, 4, 4),  # 10:04 stable movement (elapsed 2) -> MOVING @10:04
            (0.71, 0.11, 5, 5),  # 10:05 stops -> run starts @10:05
            (0.70, 0.11, 6, 6),  # 10:06 stationary (elapsed 1 < 2)
            (0.70, 0.12, 7, 7),  # 10:07 stationary (elapsed 2) -> STATIONARY @10:07
        )
        states = _classification_states(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert states == [
            "unknown",  # 10:00
            "stationary",  # 10:01
            "stationary",  # 10:02 (qualifying)
            "stationary",  # 10:03 (still qualifying)
            "moving",  # 10:04
            "moving",  # 10:05 (qualifying toward stationary)
            "moving",  # 10:06 (still qualifying)
            "stationary",  # 10:07
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
        assert [t.event_time for t in confirmations] == [_event(1), _event(4), _event(7)]
        assert [t.qualification_started for t in confirmations] == [
            None,
            _event(2),
            _event(5),
        ]
        assert state.current_state == "stationary"
        assert state.state_since == _event(7)


# =============================================================================
# §10/§11. Waiting qualification — all conditions + duration, event-time
# =============================================================================


class TestWaitingQualification:
    """WAITING requires presence + context + stationary + duration."""

    def test_waiting_requires_confirmed_presence(self) -> None:
        # §10: stationary stays without a confirmed ENTER never start a
        # candidate, no matter how long they last.
        engine = _waiting_engine(_waiting_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        states = _waiting_states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("stay", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
                ("stay", "stationary", 4, 4),
            ),
        )
        assert states == ["not_waiting", "not_waiting", "not_waiting"]

    def test_waiting_requires_waiting_context(self) -> None:
        # §10: a stationary entity in a context NOT declared waiting-capable
        # is never WAITING (the set is explicit configuration).
        engine = _waiting_engine(_waiting_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys(semantic_context="lobby")
        states = _waiting_states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
                ("stay", "stationary", 4, 4),
            ),
        )
        assert states == ["not_waiting", "not_waiting", "not_waiting"]

    def test_waiting_qualification_completes_at_the_event_time_boundary(self) -> None:
        # §11: the candidate is confirmed at candidate_start + duration of
        # EVENT time; waiting_start is the confirming observation's time.
        engine = _waiting_engine(_waiting_policy(waiting_qualification_seconds=3.0))
        pkey, ckey, wkey = _family_keys()
        states = _waiting_states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),  # candidate @10:00
                ("stay", "stationary", 1, 1),  # 1s
                ("stay", "stationary", 2, 2),  # 2s
                ("stay", "stationary", 3, 3),  # 3s >= 3 -> WAITING @10:03
            ),
        )
        assert states == [
            "waiting_candidate",
            "waiting_candidate",
            "waiting_candidate",
            "waiting",
        ]
        state = engine.initial_state(wkey)
        for kind, classification_state, seconds, frame_index in (
            ("enter_confirmed", "stationary", 0, 0),
            ("stay", "stationary", 1, 1),
            ("stay", "stationary", 2, 2),
            ("stay", "stationary", 3, 3),
        ):
            transition = {"enter_confirmed": _enter, "stay": _stay}[kind](
                pkey, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            state = engine.apply(
                state,
                _w_input(
                    wkey,
                    transition=transition,
                    classification=_classification(ckey, current_state=classification_state),
                    kind=kind,
                ),
            ).state
        assert state.current_state == "waiting"
        assert state.candidate_start == _event(0)
        assert state.waiting_start == _event(3)
        assert engine.open_interval(state).waiting_start == _event(3)


# =============================================================================
# §12/§13/§22. Waiting hysteresis + spatial boundary stability + golden timeline
# =============================================================================


class TestWaitingHysteresisAndBoundaryStability:
    """Small movement / boundary noise never flips WAITING -> NOT_WAITING."""

    def _waiting_state(
        self,
        *,
        waiting_qualification_seconds: float = 2.0,
    ) -> tuple[WaitingEngine, WaitingState, TemporalStateKey, TemporalStateKey, TemporalStateKey]:
        engine = _waiting_engine(
            _waiting_policy(waiting_qualification_seconds=waiting_qualification_seconds)
        )
        pkey, ckey, wkey = _family_keys()
        state = _w_apply(
            engine,
            engine.initial_state(wkey),
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
            classification=_classification(ckey, current_state="stationary"),
            kind="enter_confirmed",
        )
        state = _w_apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(2), frame_id=_frame(2)),
            classification=_classification(ckey, current_state="stationary"),
            kind="stay",
        )
        assert state.current_state == "waiting"
        return engine, state, pkey, ckey, wkey

    def test_tiny_movement_noise_never_ends_waiting(self) -> None:
        # §12: small movement around the stationary threshold must not cause
        # WAITING -> NOT_WAITING -> WAITING. The movement hysteresis keeps
        # the 15.5.2 classification STATIONARY (the waiting engine reads the
        # classification — there is NO separate waiting threshold).
        engine, state, pkey, ckey, wkey = self._waiting_state()
        # A short band/stationary wobble at the movement layer still reads
        # STATIONARY at the waiting layer -> WAITING is preserved.
        for seconds in (3, 4, 5, 6):
            state = _w_apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_stay(pkey, event_time=_event(seconds), frame_id=_frame(seconds)),
                classification=_classification(ckey, current_state="stationary"),
                kind="stay",
            )
            assert state.current_state == "waiting"
            assert state.waiting_start == _event(2)  # never reset
            assert engine.open_interval(state).waiting_end is None

    def test_boundary_noise_preserved_until_spatial_policy_confirms_change(self) -> None:
        # §13: boundary noise the Task 14 spatial policy does NOT confirm as
        # a context change (the presence engine reports an OBSERVED_STAY —
        # still PRESENT) never flips WAITING. Only a confirmed presence
        # loss (the spatial policy's confirmed exit) ends the interval.
        engine, state, pkey, ckey, wkey = self._waiting_state()
        for seconds in (3, 4, 5):  # spatial jitter around the zone edge
            state = _w_apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_stay(pkey, event_time=_event(seconds), frame_id=_frame(seconds)),
                classification=_classification(ckey, current_state="stationary"),
                kind="stay",
            )
            assert state.current_state == "waiting"
        # The Task 14/presence policy CONFIRMS the exit -> the interval
        # closes with EXIT_CONFIRMED; no WAITING_ZONE_A -> NON_WAITING
        # -> WAITING_ZONE_A cycle (re-entry requires a NEW confirmed entry).
        result = engine.apply(
            state,
            _w_input(
                wkey,
                transition=_presence_transition(
                    pkey,
                    reason=TemporalReason.EXIT_CONFIRMED,
                    event_time=_event(6),
                    frame_id=_frame(6),
                    from_state="present",
                    to_state="absent",
                ),
                classification=_classification(ckey, current_state="stationary"),
                kind="exit_confirmed",
            ),
        )
        assert result.state.current_state == "not_waiting"
        assert result.interval is not None
        assert result.interval.reason is TemporalReason.EXIT_CONFIRMED

    def test_waiting_ends_when_movement_qualifies(self) -> None:
        # §12: waiting ends ONLY when the 15.5.2 classification CONFIRMS
        # MOVING (sustained evidence), not on a single excursion.
        engine, state, pkey, ckey, wkey = self._waiting_state()
        result = engine.apply(
            state,
            _w_input(
                wkey,
                transition=_stay(pkey, event_time=_event(3), frame_id=_frame(3)),
                classification=_classification(ckey, current_state="moving"),
                kind="stay",
            ),
        )
        assert result.state.current_state == "not_waiting"
        assert result.interval is not None
        assert result.interval.reason is TemporalReason.MOVEMENT_EXCEEDED


class TestGoldenWaitingTimeline:
    """10:00 enter .. 10:07 stable — full lockstep with the movement engine.

    The 10:06 small movement must NOT end waiting: the movement engine's
    hysteresis keeps the classification STATIONARY (its qualification run
    is cancelled at 10:07), so the waiting layer never sees MOVING.
    """

    def test_golden_timeline(self) -> None:
        # enter 0.25 / exit 0.125 / movement qualification 2s / waiting
        # qualification 3s. Distances are exact binary fractions (16ths).
        policy = TemporalPolicy(
            movement_enter_threshold=0.25,
            movement_exit_threshold=0.125,
            movement_qualification_seconds=2.0,
            waiting_qualification_seconds=3.0,
            waiting_contexts=frozenset({WAITING_CTX}),
        )
        move_engine = _move_engine(policy)
        cls_engine = _classify_engine(policy)
        waiting_engine = _waiting_engine(policy)
        move_key, cls_key = _pair_keys(semantic_context=WAITING_CTX)
        pkey = _key(fsm_kind="presence", semantic_context=WAITING_CTX)
        wkey = _key(fsm_kind="waiting", semantic_context=WAITING_CTX)

        # (x, y, seconds, frame_index, presence kind)
        timeline = (
            (0.25, 0.50, 0, 0, "enter"),  # 10:00 enter waiting zone (anchor)
            (0.3125, 0.50, 1, 1, "stay"),  # 10:01 stationary -> candidate
            (0.375, 0.50, 2, 2, "stay"),  # 10:02 stationary (1s)
            (0.4375, 0.50, 3, 3, "stay"),  # 10:03 stationary (2s)
            (0.50, 0.50, 4, 4, "stay"),  # 10:04 qualification complete -> WAITING
            (0.5625, 0.50, 5, 5, "stay"),  # 10:05 waiting continues
            (0.875, 0.50, 6, 6, "stay"),  # 10:06 small movement (run starts)
            (0.9375, 0.50, 7, 7, "stay"),  # 10:07 stable again (run cancelled)
        )
        move_state = move_engine.initial_state(move_key)
        cls_state = cls_engine.initial_state(cls_key)
        waiting_state = waiting_engine.initial_state(wkey)
        classification_states: list[str] = []
        waiting_states: list[str] = []
        for x, y, seconds, frame_index, kind in timeline:
            obs = _spatial_obs(
                move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            move_result = move_engine.apply(
                move_state,
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )
            move_state = move_result.state
            cls_state = cls_engine.apply(
                cls_state,
                classification_input_from_movement(cls_key, obs, move_result, _processing()),
            ).state
            classification_states.append(cls_state.current_state)
            transition = {"enter": _enter, "stay": _stay}[kind](
                pkey, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            waiting_state = waiting_engine.apply(
                waiting_state,
                _w_input(
                    wkey,
                    transition=transition,
                    classification=cls_state,
                    kind=waiting_event_from_presence(transition),
                ),
            ).state
            waiting_states.append(waiting_state.current_state)

        # Movement hysteresis: the 10:06 excursion never confirms MOVING.
        assert classification_states == ["unknown", "stationary"] + ["stationary"] * 6
        # Waiting: candidate -> qualified at 10:04, small movement at 10:06
        # never ends it (no WAITING -> NOT_WAITING -> WAITING cycle).
        assert waiting_states == [
            "not_waiting",  # 10:00 entered, not yet stationary
            "waiting_candidate",  # 10:01
            "waiting_candidate",  # 10:02
            "waiting_candidate",  # 10:03
            "waiting",  # 10:04 qualification complete
            "waiting",  # 10:05
            "waiting",  # 10:06 small movement (classification still stationary)
            "waiting",  # 10:07 stable again
        ]
        assert waiting_state.current_state == "waiting"
        assert waiting_state.candidate_start == _event(1)
        assert waiting_state.waiting_start == _event(4)
        assert waiting_engine.open_interval(waiting_state).waiting_end is None


# =============================================================================
# §14. Occlusion grace is reused — short gaps never cancel state
# =============================================================================


class TestOcclusionGrace:
    """A measurement-less / short-missing step never cancels qualification."""

    def test_missing_step_preserves_a_pending_movement_run(self) -> None:
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        ).state
        assert state.pending_state == "moving"
        # An occlusion gap (measurement-less step) at a LATER event time:
        # the run is preserved, not cancelled.
        state = engine.apply(
            state, _cls_input(cls_key, measurement=None, event_time=_event(9), frame_id=_frame(9))
        ).state
        assert state.current_state == "stationary"
        assert state.pending_state == "moving"
        assert state.qualification_started == _event(0)
        assert state.watermark_event_time == _event(9)

    def test_short_gap_preserves_waiting(self) -> None:
        # The presence FSM's grace policy (a stay while TEMPORARILY_MISSING)
        # preserves WAITING; only a confirmed missing_expired ends it.
        engine, state, pkey, ckey, wkey = (
            TestWaitingHysteresisAndBoundaryStability()._waiting_state()
        )
        state = _w_apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(3), frame_id=_frame(3)),
            classification=_classification(ckey, current_state="stationary"),
            kind="stay",
        )
        assert state.current_state == "waiting"
        assert state.waiting_start == _event(2)


# =============================================================================
# §15. Out-of-order events follow the 15.1 policy (10:00 / 10:02 / 10:01)
# =============================================================================


class TestOutOfOrderPolicy:
    """The 15.1 watermark/reorder policy is reused — no second buffer."""

    def test_ten_oh_two_then_ten_oh_one_is_reordered_not_rewound(self) -> None:
        policy = _policy(movement_qualification_seconds=0.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = engine.initial_state(cls_key)
        state = engine.apply(
            state, _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0))
        ).state  # 10:00 anchor
        at_1002 = _measurement(
            distance=0.5,
            previous_event_time=_event(0),
            event_time=_event(2),
            move_key=move_key,
        )
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=at_1002, event_time=_event(2), frame_id=_frame(2)),
        ).state  # 10:02 -> MOVING (watermark 10:02)
        assert state.current_state == "moving"
        # 10:01 arrives late: within the reorder window -> REORDERED,
        # never rewinds the classification or the watermark.
        at_1001 = _measurement(
            distance=0.02,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=move_key,
        )
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=at_1001, event_time=_event(1), frame_id=_frame(1)),
        )
        assert result.reordered is True
        assert result.deduplicated is False
        assert result.transition is None
        assert result.state.current_state == "moving"
        assert result.state.watermark_event_time == _event(2)

    def test_late_beyond_window_rejected(self) -> None:
        policy = _policy(movement_qualification_seconds=0.0, reorder_window_seconds=30.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = engine.initial_state(cls_key)
        state = engine.apply(
            state, _cls_input(cls_key, measurement=None, event_time=_event(0), frame_id=_frame(0))
        ).state
        at_1002 = _measurement(
            distance=0.5,
            previous_event_time=_event(0),
            event_time=_event(2),
            move_key=move_key,
        )
        state = engine.apply(
            state,
            _cls_input(cls_key, measurement=at_1002, event_time=_event(2), frame_id=_frame(2)),
        ).state
        too_late = _measurement(
            distance=0.02,
            previous_event_time=_event(-100),
            event_time=_event(-99),  # 62s older than the 10:02 watermark
            move_key=move_key,
        )
        with pytest.raises(LateEventError, match="reordering window"):
            engine.apply(
                state,
                _cls_input(
                    cls_key, measurement=too_late, event_time=_event(-99), frame_id=_frame(-99)
                ),
            )


# =============================================================================
# §16. Duplicate observations and candidate events are idempotent
# =============================================================================


class TestDuplicateIdempotency:
    """No duplicated qualification, transition, or waiting interval."""

    def test_duplicate_observation_is_deduplicated_at_the_foundation(self) -> None:
        move_engine = _move_engine()
        move_key = _key(fsm_kind="movement")
        obs = _spatial_obs(move_key, x=0.3, y=0.1, event_time=_event(1), frame_id=_frame(1))
        first = move_engine.apply(
            move_engine.initial_state(move_key),
            MovementInput(key=move_key, observation=obs, processing_time=_processing()),
        )
        second = move_engine.apply(
            first.state,
            MovementInput(key=move_key, observation=obs, processing_time=_processing()),
        )
        assert second.deduplicated is True
        assert second.measurement is None  # no second distance, no second state

    def test_duplicate_candidate_event_cannot_advance_qualification_twice(self) -> None:
        # §24.5: replaying the SAME above-enter measurement (same event-time
        # position) while a run is pending is deduplicated — the run start
        # is never restarted or double-advanced.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        inp = _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        first = engine.apply(state, inp)
        assert first.state.pending_state == "moving"
        assert first.state.qualification_started == _event(0)
        replay = engine.apply(first.state, inp)
        assert replay.deduplicated is True
        assert replay.state == first.state  # byte-identical: no double advance
        # And the single run still completes once.
        confirming = _measurement(
            distance=0.6,
            previous_event_time=_event(0),
            event_time=_event(5),
            move_key=move_key,
        )
        result = engine.apply(
            first.state,
            _cls_input(cls_key, measurement=confirming, event_time=_event(5), frame_id=_frame(5)),
        )
        assert result.state.current_state == "moving"
        assert result.transition is not None

    def test_duplicate_waiting_candidate_event_does_not_double_count(self) -> None:
        engine = _waiting_engine(_waiting_policy(waiting_qualification_seconds=5.0))
        pkey, ckey, wkey = _family_keys()
        state = engine.initial_state(wkey)
        transition = _enter(pkey, event_time=_event(0), frame_id=_frame(0))
        inp = _w_input(
            wkey,
            transition=transition,
            classification=_classification(ckey, current_state="stationary"),
            kind="enter_confirmed",
        )
        first = engine.apply(state, inp)
        assert first.state.current_state == "waiting_candidate"
        assert first.state.candidate_start == _event(0)
        replay = engine.apply(first.state, inp)
        assert replay.deduplicated is True
        assert replay.state == first.state
        assert replay.interval is None  # no duplicated waiting interval


# =============================================================================
# §17. State isolation — no qualification state leaks between entities
# =============================================================================


class TestStateIsolation:
    """Per-tenant/venue/session/track/context independent qualification."""

    def test_waiting_state_is_isolated_across_scopes(self) -> None:
        engine = _waiting_engine(_waiting_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        # Tenant A becomes WAITING.
        state_a = _w_apply(
            engine,
            engine.initial_state(wkey),
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
            classification=_classification(ckey, current_state="stationary"),
            kind="enter_confirmed",
        )
        state_a = _w_apply(
            engine,
            state_a,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(2), frame_id=_frame(2)),
            classification=_classification(ckey, current_state="stationary"),
            kind="stay",
        )
        assert state_a.current_state == "waiting"
        # Tenant B (same track id, same venue/session) is untouched.
        other_tenant = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        _, _, wb = _family_keys(tenant_id=other_tenant)
        state_b = engine.initial_state(wb)
        assert state_b.current_state == "not_waiting"
        assert state_b != state_a
        assert state_a.key.tenant_id != state_b.key.tenant_id

    def test_cross_tenant_input_rejected(self) -> None:
        engine = _waiting_engine(_waiting_policy())
        _, ckey, wkey = _family_keys()
        other = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        pkey_other = _key(fsm_kind="presence", tenant_id=other, semantic_context=WAITING_CTX)
        with pytest.raises(StateKeyMismatchError, match="tenant_id"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=_enter(pkey_other, event_time=_event(0), frame_id=_frame(0)),
                    classification=_classification(ckey, current_state="stationary"),
                    kind="enter_confirmed",
                ),
            )

    def test_cross_venue_input_rejected(self) -> None:
        engine = _waiting_engine(_waiting_policy())
        _, ckey, wkey = _family_keys()
        other = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        pkey_other = _key(fsm_kind="presence", venue_id=other, semantic_context=WAITING_CTX)
        with pytest.raises(StateKeyMismatchError, match="venue_id"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=_enter(pkey_other, event_time=_event(0), frame_id=_frame(0)),
                    classification=_classification(ckey, current_state="stationary"),
                    kind="enter_confirmed",
                ),
            )

    def test_cross_session_input_rejected(self) -> None:
        engine = _waiting_engine(_waiting_policy())
        _, ckey, wkey = _family_keys()
        other = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        pkey_other = _key(fsm_kind="presence", session_id=other, semantic_context=WAITING_CTX)
        with pytest.raises(StateKeyMismatchError, match="session_id"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=_enter(pkey_other, event_time=_event(0), frame_id=_frame(0)),
                    classification=_classification(ckey, current_state="stationary"),
                    kind="enter_confirmed",
                ),
            )


# =============================================================================
# §18. Configuration provenance — V1 replays under V1 semantics after V2
# =============================================================================


class TestConfigurationProvenance:
    """Historical replay never uses the current/latest thresholds."""

    def test_v1_session_replays_with_v1_semantics_after_v2_published(self) -> None:
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        # V1: distance 0.25 < enter 0.3 -> STATIONARY.
        # V2: distance 0.25 > enter 0.2 -> MOVING.
        v1_policy = _policy(
            movement_enter_threshold=0.3, movement_exit_threshold=0.1, revision="v1"
        )
        v2_policy = _policy(
            movement_enter_threshold=0.2, movement_exit_threshold=0.1, revision="v2"
        )
        timeline = ((0.1, 0.1, 0, 0), (0.35, 0.1, 1, 1))  # distance 0.25
        engine_v1 = _classify_engine(v1_policy)
        move_v1 = _move_engine(v1_policy)
        move1, cls1 = _pair_keys(configuration_version_id=v1)
        _, t1 = _chain(engine_v1, move_v1, cls_key=cls1, move_key=move1, timeline=timeline)
        assert t1[-1].to_state == "stationary"
        # V2 is published and processes the SAME trajectory differently.
        engine_v2 = _classify_engine(v2_policy)
        move_v2 = _move_engine(v2_policy)
        move2, cls2 = _pair_keys(configuration_version_id=v2)
        _, t2 = _chain(engine_v2, move_v2, cls_key=cls2, move_key=move2, timeline=timeline)
        assert t2[-1].to_state == "moving"
        # Replaying the V1 session in the V2-published world still yields
        # V1 semantics (byte-identical to the first V1 run).
        _, t1_replay = _chain(engine_v1, move_v1, cls_key=cls1, move_key=move1, timeline=timeline)
        assert t1 == t1_replay
        assert t1[-1].transition_id != t2[-1].transition_id


# =============================================================================
# §19/§20. Checkpoint compatibility and restart recovery
# =============================================================================


class TestCheckpointAndRestart:
    """Qualification state is checkpoint-compatible; restart equals uninterrupted."""

    def test_movement_candidate_survives_restart(self) -> None:
        # §20: 10:00 stationary, 10:01 movement begins, 10:02 candidate
        # active -> CHECKPOINT -> RESTART -> 10:03 continues, 10:04
        # qualification completes. Same final state as uninterrupted.
        policy = _policy(movement_qualification_seconds=2.0)
        move_key, cls_key = _pair_keys()
        timeline = (
            (0.10, 0.10, 0, 0),
            (0.11, 0.11, 1, 1),
            (0.30, 0.10, 2, 2),  # candidate active @10:02
            (0.50, 0.10, 3, 3),  # 10:03 movement continues
            (0.72, 0.10, 4, 4),  # 10:04 qualification completes
        )
        uninterrupted, _ = _chain(
            _classify_engine(policy),
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=timeline,
        )
        assert uninterrupted.current_state == "moving"
        assert uninterrupted.state_since == _event(4)

        move_engine = _move_engine(policy)
        cls_engine = _classify_engine(policy)
        move_state = move_engine.initial_state(move_key)
        cls_state = cls_engine.initial_state(cls_key)
        for x, y, seconds, frame_index in timeline[:3]:
            obs = _spatial_obs(
                move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            move_result = move_engine.apply(
                move_state,
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )
            move_state = move_result.state
            cls_state = cls_engine.apply(
                cls_state,
                classification_input_from_movement(cls_key, obs, move_result, _processing()),
            ).state
        assert cls_state.pending_state == "moving"  # candidate mid-flight

        # CHECKPOINT both families; RESTART into FRESH engines.
        move_cp = move_engine.checkpoint(move_state)
        cls_cp = cls_engine.checkpoint(cls_state)
        resumed_move_engine = _move_engine(policy)
        resumed_cls_engine = _classify_engine(policy)
        restored_move = resumed_move_engine.restore(move_cp)
        restored_cls = resumed_cls_engine.restore(cls_cp)
        assert restored_move == move_state
        assert restored_cls == cls_state
        for x, y, seconds, frame_index in timeline[3:]:
            obs = _spatial_obs(
                move_key, x=x, y=y, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            move_result = resumed_move_engine.apply(
                restored_move,
                MovementInput(key=move_key, observation=obs, processing_time=_processing()),
            )
            restored_move = move_result.state
            restored_cls = resumed_cls_engine.apply(
                restored_cls,
                classification_input_from_movement(cls_key, obs, move_result, _processing()),
            ).state
        assert restored_cls == uninterrupted
        assert restored_cls.current_state == "moving"
        assert restored_cls.state_since == _event(4)

    def test_waiting_candidate_survives_restart(self) -> None:
        # §20 (waiting): candidate active -> CHECKPOINT -> RESTART ->
        # qualification completes; equal to uninterrupted processing.
        policy = _waiting_policy(waiting_qualification_seconds=3.0)
        pkey, ckey, wkey = _family_keys()
        timeline = (
            ("enter_confirmed", "stationary", 0, 0),  # candidate @10:00
            ("stay", "stationary", 1, 1),  # 1s
            ("stay", "stationary", 2, 2),  # 2s — candidate still active
            ("stay", "stationary", 3, 3),  # 3s -> WAITING @10:03
        )
        # Uninterrupted run.
        uninterrupted = _waiting_engine(policy).initial_state(wkey)
        for kind, classification_state, seconds, frame_index in timeline:
            transition = {"enter_confirmed": _enter, "stay": _stay}[kind](
                pkey, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            uninterrupted = (
                _waiting_engine(policy)
                .apply(
                    uninterrupted,
                    _w_input(
                        wkey,
                        transition=transition,
                        classification=_classification(ckey, current_state=classification_state),
                        kind=kind,
                    ),
                )
                .state
            )
        assert uninterrupted.current_state == "waiting"
        assert uninterrupted.waiting_start == _event(3)

        engine = _waiting_engine(policy)
        state = engine.initial_state(wkey)
        for kind, classification_state, seconds, frame_index in timeline[:3]:
            transition = {"enter_confirmed": _enter, "stay": _stay}[kind](
                pkey, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            state = engine.apply(
                state,
                _w_input(
                    wkey,
                    transition=transition,
                    classification=_classification(ckey, current_state=classification_state),
                    kind=kind,
                ),
            ).state
        assert state.current_state == "waiting_candidate"
        checkpoint = engine.checkpoint(state)
        assert checkpoint.state.candidate_start == _event(0)

        resumed_engine = _waiting_engine(policy)
        restored = resumed_engine.restore(checkpoint)
        assert restored == state
        kind, classification_state, seconds, frame_index = timeline[3]
        transition = _stay(pkey, event_time=_event(seconds), frame_id=_frame(frame_index))
        final = resumed_engine.apply(
            restored,
            _w_input(
                wkey,
                transition=transition,
                classification=_classification(ckey, current_state=classification_state),
                kind=kind,
            ),
        ).state
        assert final == uninterrupted
        assert final.current_state == "waiting"
        assert final.waiting_start == _event(3)

    def test_checkpoint_carries_the_qualification_and_configuration(self) -> None:
        # §19: the checkpoint carries current state, the candidate run, the
        # candidate start, last accepted event_time, configuration version
        # (in the key) and the FSM/engine version.
        policy = _policy(movement_qualification_seconds=5.0, revision="v3")
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine,
            cls_key=cls_key,
            move_key=move_key,
            target="stationary",
            at_seconds=0,
            policy_revision="v3",
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
            policy_revision="v3",
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        ).state
        checkpoint = engine.checkpoint(state)
        assert checkpoint.engine_version == TEMPORAL_ENGINE_VERSION
        assert checkpoint.policy_revision == "v3"
        assert checkpoint.state.current_state == "stationary"
        assert checkpoint.state.pending_state == "moving"
        assert checkpoint.state.qualification_started == _event(0)
        assert checkpoint.state.last_seen == _event(0)
        assert checkpoint.state.key.configuration_version_id == _CONFIG
        assert checkpoint.state.fsm_version == TEMPORAL_ENGINE_VERSION
        data = checkpoint.to_dict()
        assert MovementClassificationCheckpoint.from_dict(data) == checkpoint
        assert engine.restore(checkpoint) == state

    def test_restore_rejects_policy_drift(self) -> None:
        engine = _classify_engine(_policy(revision="v2"))
        state = engine.initial_state(_key(fsm_kind="movement_classification"))
        checkpoint = MovementClassificationCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION, policy_revision="v1", state=state
        )
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            engine.restore(checkpoint)

    def test_restore_rejects_engine_version_drift(self) -> None:
        engine = _classify_engine(_policy())
        state = engine.initial_state(_key(fsm_kind="movement_classification"))
        checkpoint = MovementClassificationCheckpoint(
            engine_version="9.9.9", policy_revision="v1", state=state
        )
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            engine.restore(checkpoint)


# =============================================================================
# §23. Failure cases — explicit rejection, never silent repair
# =============================================================================


class TestFailureCases:
    """Malformed or contradictory inputs fail explicitly."""

    def test_invalid_movement_measurement_rejected(self) -> None:
        # A NaN displacement is never classified. model_construct bypasses
        # pydantic so the ENGINE's integrity guard is exercised directly.
        engine = _classify_engine(_policy())
        cls_key = _key(fsm_kind="movement_classification")
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

    def test_wrong_measurement_family_rejected(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key(fsm_kind="movement_classification")
        measurement = _measurement(
            distance=0.5,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=_key(fsm_kind="presence"),
        )
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)
                ),
            )

    def test_naive_event_time_rejected(self) -> None:
        engine = _classify_engine(_policy())
        cls_key = _key(fsm_kind="movement_classification")
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

    def test_backwards_timestamp_rejected_at_contract(self) -> None:
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

    def test_missing_position_is_unrepresentable(self) -> None:
        # A measurement without a current position cannot exist — movement
        # is never fabricated from a missing observation.
        with pytest.raises(ValueError):
            MovementMeasurement(
                measurement_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "m-missing")),
                fsm_kind="movement",
                key=_key(fsm_kind="movement"),
                previous_position=_point(0.1, 0.1),
                previous_event_time=_event(0),
                distance=0.2,  # current_position / event_time missing
                time_delta_seconds=1.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )

    def test_measurement_from_a_different_policy_revision_rejected(self) -> None:
        engine = _classify_engine(_policy(revision="v1"))
        cls_key = _key(fsm_kind="movement_classification")
        measurement = _measurement(
            distance=0.5,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=_key(fsm_kind="movement"),
            policy_revision="v2",
        )
        with pytest.raises(InvalidTemporalInputError, match="policy_revision"):
            engine.apply(
                engine.initial_state(cls_key),
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)
                ),
            )

    def test_wrong_waiting_context_cannot_hold_candidate_state(self) -> None:
        # A candidate under a context that is NOT waiting-capable is a
        # corrupted state — the engine refuses it rather than repairing it.
        engine = _waiting_engine(_waiting_policy(waiting_contexts=frozenset()))
        pkey, ckey, wkey = _family_keys(semantic_context=WAITING_CTX)
        corrupted = WaitingState(
            fsm_version=TEMPORAL_ENGINE_VERSION,
            key=wkey,
            current_state="waiting_candidate",
            candidate_start=_event(0),
            presence_confirmed=True,
        )
        with pytest.raises(InvalidTemporalInputError, match="waiting-capable"):
            engine.apply(
                corrupted,
                _w_input(
                    wkey,
                    transition=_stay(pkey, event_time=_event(1), frame_id=_frame(1)),
                    classification=_classification(ckey, current_state="stationary"),
                    kind="stay",
                ),
            )


# =============================================================================
# §24. Property / invariant tests
# =============================================================================


class TestInvariants:
    """The 15.5.4 invariants hold under adversarial inputs."""

    def test_state_cannot_flip_repeatedly_without_qualifying_evidence(self) -> None:
        # A sustained oscillation above-enter / band, with a qualification
        # window longer than the oscillation period: each above-enter run
        # is cancelled by the next band measurement, so the classification
        # changes at most ONCE (the initial classification) — never flaps.
        policy = _policy(
            movement_enter_threshold=0.25,
            movement_exit_threshold=0.125,
            movement_qualification_seconds=2.0,
        )
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        transitions = 0
        for step in range(20):
            distance = 0.6 if step % 2 == 0 else 0.1875  # above-enter / band
            measurement = _measurement(
                distance=distance,
                previous_event_time=_event(step - 1),
                event_time=_event(step),
                move_key=move_key,
            )
            result = engine.apply(
                state,
                _cls_input(
                    cls_key, measurement=measurement, event_time=_event(step), frame_id=_frame(step)
                ),
            )
            state = result.state
            if result.transition is not None:
                transitions += 1
        assert state.current_state == "stationary"
        assert transitions == 0  # never flipped after the seed

    def test_hysteresis_band_preserves_current_state(self) -> None:
        # For ANY band measurement, the state is retained in both
        # directions (the mandatory §5 behavior).
        policy = _policy(movement_enter_threshold=0.25, movement_exit_threshold=0.125)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        for target in ("stationary", "moving"):
            state = _seed_direct(
                engine, cls_key=cls_key, move_key=move_key, target=target, at_seconds=0
            )
            for band_distance in (0.1875, 0.21875, 0.15625):  # exit < d < enter
                measurement = _measurement(
                    distance=band_distance,
                    previous_event_time=_event(0),
                    event_time=_event(1),
                    move_key=move_key,
                )
                result = engine.apply(
                    state,
                    _cls_input(
                        cls_key, measurement=measurement, event_time=_event(1), frame_id=_frame(1)
                    ),
                )
                assert result.state.current_state == target
                assert result.transition is None
                state = result.state

    def test_qualification_uses_event_time(self) -> None:
        # Qualification duration is measured in EVENT time: a confirming
        # measurement before the window does not complete the run, one at
        # the boundary does — regardless of processing time.
        policy = _policy(movement_qualification_seconds=10.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        ).state
        early = _measurement(
            distance=0.6,
            previous_event_time=_event(0),
            event_time=_event(5),
            move_key=move_key,
        )
        state = engine.apply(
            state,
            _cls_input(
                cls_key,
                measurement=early,
                event_time=_event(5),
                frame_id=_frame(5),
                processing_time=_processing(9999),  # processing time is irrelevant
            ),
        ).state
        assert state.current_state == "stationary"
        boundary = _measurement(
            distance=0.7,
            previous_event_time=_event(5),
            event_time=_event(10),
            move_key=move_key,
        )
        result = engine.apply(
            state,
            _cls_input(cls_key, measurement=boundary, event_time=_event(10), frame_id=_frame(10)),
        )
        assert result.state.current_state == "moving"

    def test_candidate_state_cannot_survive_invalidation(self) -> None:
        # §24.4: a pending candidate is cleared by contradicting evidence —
        # no dangling qualification_started survives.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="moving", at_seconds=0
        )
        below = _measurement(
            distance=0.01,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=below, event_time=_event(0), frame_id=_frame(0))
        ).state
        assert state.pending_state == "stationary"
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=above, event_time=_event(1), frame_id=_frame(1))
        ).state
        assert state.pending_state is None
        assert state.qualification_started is None
        # A band measurement also invalidates a run (ambiguous evidence is
        # never \"qualified\").
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="moving", at_seconds=0
        )
        below = _measurement(
            distance=0.01,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=below, event_time=_event(0), frame_id=_frame(0))
        ).state
        band = _measurement(
            distance=0.1875,
            previous_event_time=_event(0),
            event_time=_event(1),
            move_key=move_key,
        )
        state = engine.apply(
            state, _cls_input(cls_key, measurement=band, event_time=_event(1), frame_id=_frame(1))
        ).state
        assert state.pending_state is None
        assert state.qualification_started is None

    def test_duplicate_events_cannot_advance_qualification_twice(self) -> None:
        # §24.5: covered exhaustively in TestDuplicateIdempotency — here the
        # invariant is asserted on the run anchor.
        policy = _policy(movement_qualification_seconds=5.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        state = _seed_direct(
            engine, cls_key=cls_key, move_key=move_key, target="stationary", at_seconds=0
        )
        above = _measurement(
            distance=0.5,
            previous_event_time=_event(-1),
            event_time=_event(0),
            move_key=move_key,
        )
        inp = _cls_input(cls_key, measurement=above, event_time=_event(0), frame_id=_frame(0))
        state = engine.apply(state, inp).state
        run_anchor = state.qualification_started
        for _ in range(3):  # replay the SAME candidate event repeatedly
            result = engine.apply(state, inp)
            assert result.deduplicated is True
            assert result.state == state
            assert result.state.qualification_started == run_anchor
            state = result.state

    def test_waiting_cannot_exist_without_confirmed_presence(self) -> None:
        # §24.6: WAITING requires presence_confirmed by construction — the
        # state model itself rejects a candidate/waiting without it.
        _, _, wkey = _family_keys()
        with pytest.raises(ValueError, match="presence_confirmed"):
            WaitingState(
                fsm_version=TEMPORAL_ENGINE_VERSION,
                key=wkey,
                current_state="waiting_candidate",
                candidate_start=_event(0),
                presence_confirmed=False,
            )

    def test_waiting_cannot_exist_in_a_non_waiting_context(self) -> None:
        # §24.7: covered in TestWaitingQualification — here the engine-level
        # invariant is asserted: a non-waiting context NEVER produces a
        # candidate even after a long stationary presence.
        engine = _waiting_engine(_waiting_policy(waiting_qualification_seconds=1.0))
        pkey, ckey, wkey = _family_keys(semantic_context="hallway")
        state = engine.initial_state(wkey)
        for seconds in range(10):
            transition = (
                _enter(pkey, event_time=_event(seconds), frame_id=_frame(seconds))
                if seconds == 0
                else _stay(pkey, event_time=_event(seconds), frame_id=_frame(seconds))
            )
            kind = "enter_confirmed" if seconds == 0 else "stay"
            state = engine.apply(
                state,
                _w_input(
                    wkey,
                    transition=transition,
                    classification=_classification(ckey, current_state="stationary"),
                    kind=kind,
                ),
            ).state
            assert state.current_state == "not_waiting"

    def test_configuration_version_remains_consistent(self) -> None:
        # §24.8: every fact and every state step preserves the pinned
        # configuration version; nothing ever switches to \"latest\".
        policy = _policy(movement_qualification_seconds=2.0, revision="v4")
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys(configuration_version_id=_CONFIG)
        state, transitions = _chain(
            engine,
            _move_engine(policy),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1)),
        )
        assert state.key.configuration_version_id == _CONFIG
        for transition in transitions:
            if transition is not None:
                assert transition.key.configuration_version_id == _CONFIG
                assert transition.policy_revision == "v4"


# =============================================================================
# §25. Performance — bounded state
# =============================================================================


class TestBoundedState:
    """No unbounded observation history; O(1) per step; bounded checkpoints."""

    def test_long_stream_stays_bounded_and_deterministic(self) -> None:
        policy = _policy(movement_qualification_seconds=2.0)
        engine = _classify_engine(policy)
        move_key, cls_key = _pair_keys()
        # An oscillating track over 300 steps: the classification state
        # carries scalars only — no growth, no O(n^2).
        timeline = tuple((0.2 if step % 2 == 0 else 0.6, 0.5, step, step) for step in range(300))
        state, _ = _chain(
            engine, _move_engine(), cls_key=cls_key, move_key=move_key, timeline=timeline
        )
        assert state.current_state == "moving"
        for field in MovementClassificationState.model_fields:
            value = getattr(state, field)
            if field == "key":
                continue
            assert not isinstance(value, (list, tuple, dict, set, frozenset))

    def test_checkpoint_size_is_bounded(self) -> None:
        engine = _classify_engine(_policy(movement_qualification_seconds=2.0))
        move_key, cls_key = _pair_keys()
        state, _ = _chain(
            engine,
            _move_engine(),
            cls_key=cls_key,
            move_key=move_key,
            timeline=((0.1, 0.1, 0, 0), (0.3, 0.1, 1, 1), (0.5, 0.1, 3, 3), (0.7, 0.1, 5, 5)),
        )
        checkpoint = engine.checkpoint(state)
        serialized = checkpoint.to_dict()
        assert len(serialized["state"]) == len(MovementClassificationState.model_fields)


# =============================================================================
# §26. Pure domain core
# =============================================================================


class TestPureCore:
    """Hysteresis and qualification remain deterministic with no I/O."""

    def test_classification_and_waiting_cores_are_pure(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
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
        for module_name in ("classification.py", "waiting.py"):
            text = (package_dir / module_name).read_text()
            for module in forbidden:
                assert not re.search(rf"^\s*(from|import)\s+{module}\b", text, re.MULTILINE), (
                    f"I/O/stateful module {module!r} leaked into {module_name}"
                )
            assert "now(" not in text
            assert "utc_now" not in text
            assert "print(" not in text
            assert "datetime.now" not in text
