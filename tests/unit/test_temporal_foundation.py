"""Tests for the deterministic temporal engine foundation (Task 15 Step 1).

Covers the full Step 1 scope:

- event-time semantics: event_time is the authoritative ordering time,
  processing_time is metadata only (never used for ordering), and no
  current time is ever read;
- deterministic ordering: (event_time, frame_id) position, watermark,
  late-event policy (reject outside the configurable window), and
  out-of-order policy (accept within the window as REORDERED facts,
  never rewinding state);
- idempotency: the same observation applied twice produces one logical
  transition (DEDUPLICATED) with a content-derived transition identity;
- FSM foundation: reusable ``DeterministicFsm`` with explicit legal
  transitions (no arbitrary mutation), the presence FSM, and the
  structural ``presence_kind`` mapping from SpatialStatus;
- hysteresis/occlusion/grace: configurable entry/exit confirmation,
  dwell, grace, and occlusion tolerance — noisy observations near a
  boundary never flip state rapidly;
- checkpointable state: serializable checkpoints, restart recovery
  equivalent to uninterrupted processing, FSM/policy versioning;
- isolation: tenant/venue/session/configuration-version scoping plus
  key-vs-observation provenance rejection (``StateKeyMismatchError``);
- boundedness: a long session retains only the bounded recent-transition
  ring — no unbounded in-memory history.

All fixtures use the REAL canonical contracts (SpatialObservation,
TemporalStateKey, TemporalPolicy, TemporalCheckpoint) and fixed
deterministic IDs so replay comparisons are byte-exact.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.intelligence.temporal import (
    PRESENCE_FSM,
    DeterministicFsm,
    FsmRule,
    PresenceTemporalEngine,
    TemporalEngine,
    TemporalInput,
    presence_kind,
)
from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    InvalidTransitionError,
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
from contracts.spatial import (
    SpatialObservation,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    TemporalCheckpoint,
    TemporalOcclusionState,
    TemporalPolicy,
    TemporalReason,
    TemporalState,
    TemporalStateKey,
    TemporalTransition,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT = TenantId(UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(UUID("20000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(UUID("30000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
_TRACK = TrackId(UUID("60000000-0000-0000-0000-000000000001"))

_EVENT_BASE = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_PROCESSING_BASE = datetime(2026, 8, 1, 11, 0, 0, tzinfo=UTC)  # differs from event time


# =============================================================================
# Fixture builders (real canonical contracts, deterministic IDs)
# =============================================================================


def _key(
    *,
    fsm_kind: str = "presence",
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
    return FrameId(uuid.uuid5(TEMPORAL_ID_NAMESPACE, f"frame-{index}"))


def _event(seconds: int) -> datetime:
    return _EVENT_BASE + timedelta(seconds=seconds)


def _processing(seconds: int = 0) -> datetime:
    return _PROCESSING_BASE + timedelta(seconds=seconds)


def _obs(
    key: TemporalStateKey,
    *,
    kind: str,
    event_time: datetime,
    frame_id: FrameId,
) -> SpatialObservation:
    """Canonical SpatialObservation consistent with ``kind`` (and the key)."""
    if kind == "present":
        status = SpatialStatus.INSIDE
    elif kind == "absent":
        status = SpatialStatus.OUTSIDE
    else:  # not_observed / session_closed
        status = SpatialStatus.EXCLUDED if kind == "not_observed" else SpatialStatus.OUTSIDE
    return SpatialObservation(
        session_id=key.session_id,
        track_id=key.track_id,
        frame_id=frame_id,
        event_time=event_time,
        camera_id=key.camera_id,
        configuration_version_id=key.configuration_version_id,
        spatial_point=SpatialPointModel(x=0.5, y=0.5, policy=SpatialPointPolicy.FOOTPOINT),
        status=status,
    )


def _input(
    key: TemporalStateKey,
    obs: SpatialObservation,
    *,
    kind: str | None = None,
) -> TemporalInput:
    return TemporalInput(
        key=key,
        observation=obs,
        observation_kind=kind or presence_kind(obs),
        processing_time=_processing(),
    )


def _engine(**policy_kwargs) -> PresenceTemporalEngine:
    return PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=TemporalPolicy(**policy_kwargs))


def _process(
    engine: TemporalEngine,
    key: TemporalStateKey,
    timeline: tuple[tuple[str, int, int], ...],
) -> tuple[TemporalState, list[TemporalTransition]]:
    """Process ``(kind, event_seconds, frame_index)`` entries deterministically."""
    state = engine.initial_state(key)
    transitions: list[TemporalTransition] = []
    for kind, seconds, frame_index in timeline:
        obs = _obs(key, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
        result = engine.apply(state, _input(key, obs, kind=kind))
        state = result.state
        transitions.extend(result.transitions)
    return state, transitions


# =============================================================================
# 1. Normal chronological observations
# =============================================================================


class TestNormalChronological:
    """In-order observations advance the FSM deterministically."""

    def test_present_observations_confirm_entry(self) -> None:
        engine = _engine()
        state, transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 1, 1), ("present", 2, 2))
        )
        assert state.current_state == "present"
        assert state.state_since == _event(1)  # entered on the 2nd (confirmation=2)
        assert state.last_seen == _event(2)
        assert state.watermark_event_time == _event(2)
        assert state.last_applied_frame_id == _frame(2)
        assert [t.reason for t in transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.OBSERVED_STAY,
        ]

    def test_event_time_is_authoritative_over_processing_order(self) -> None:
        # Processing happens at 11:00 but events occurred at 10:00 — the
        # state must be driven entirely by event_time (no current time).
        engine = _engine()
        key = _key()
        state = engine.initial_state(key)
        first = engine.apply(
            state,
            _input(key, _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))),
        )
        assert first.state.last_seen == _event(0)
        assert first.state.watermark_event_time == _event(0)


# =============================================================================
# 2 / 4-6. Out-of-order, late, and regression policy
# =============================================================================


class TestOrderingPolicy:
    """Late/out-of-order events follow the configured window, never silently."""

    def test_out_of_order_within_window_reorders_without_corruption(self) -> None:
        # A(0) -> C(2) -> B(1): B arrives late but within the 60s window.
        engine = _engine()
        state, transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 2, 2), ("present", 1, 1))
        )
        assert state.current_state == "present"
        assert state.watermark_event_time == _event(2)  # watermark never rewinds
        assert [t.reason for t in transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.REORDERED,
        ]

    def test_late_within_allowed_window_accepted_as_reorder(self) -> None:
        engine = _engine()
        state, transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 60, 60), ("present", 30, 30))
        )
        assert state.current_state == "present"
        assert state.watermark_event_time == _event(60)
        assert transitions[-1].reason is TemporalReason.REORDERED

    def test_late_outside_allowed_window_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(LateEventError, match="reordering window"):
            _process(
                engine, _key(), (("present", 0, 0), ("present", 120, 120), ("present", 30, 30))
            )

    def test_timestamp_regression_rejected_beyond_window(self) -> None:
        engine = _engine(reorder_window_seconds=1.0)
        with pytest.raises(LateEventError):
            _process(engine, _key(), (("present", 3, 3), ("present", 1, 1)))

    def test_reorder_never_rewinds_state_or_counters(self) -> None:
        # A reorder cannot flip state that later in-order observations built.
        engine = _engine()
        state, transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 1, 1), ("present", 0, 9))
        )
        # The duplicate-position check: same event_time, different frame is a
        # reorder; state stays present, entry confirmation not re-counted.
        assert state.current_state == "present"
        assert transitions[-1].reason is TemporalReason.REORDERED
        assert state.watermark_event_time == _event(1)

    def test_processing_time_never_used_for_ordering(self) -> None:
        # Same observations, wildly different processing_times — identical
        # state. Processing time is metadata only.
        key = _key()
        engine = _engine()
        obs_a = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        obs_b = _obs(key, kind="present", event_time=_event(1), frame_id=_frame(1))
        state = engine.initial_state(key)
        state = engine.apply(
            state,
            TemporalInput(
                key=key,
                observation=obs_a,
                observation_kind="present",
                processing_time=_processing(9999),
            ),
        ).state
        state = engine.apply(
            state,
            TemporalInput(
                key=key,
                observation=obs_b,
                observation_kind="present",
                processing_time=_processing(0),
            ),
        ).state
        assert state.watermark_event_time == _event(1)
        assert state.current_state == "present"


# =============================================================================
# 3. Duplicate observation / idempotency
# =============================================================================


class TestIdempotency:
    """The same observation applied twice yields ONE logical transition."""

    def test_duplicate_observation_is_deduplicated(self) -> None:
        engine = _engine()
        key = _key()
        state = engine.initial_state(key)
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        first = engine.apply(state, _input(key, obs))
        assert not first.deduplicated
        second = engine.apply(first.state, _input(key, obs))
        assert second.deduplicated
        assert second.state == first.state  # no advance
        assert second.transitions[0].reason is TemporalReason.DEDUPLICATED
        assert second.transitions[0].to_state == first.state.current_state

    def test_duplicate_produces_deterministic_transition_identity(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        first = engine.apply(engine.initial_state(key), _input(key, obs))
        again = engine.apply(first.state, _input(key, obs))
        third = engine.apply(again.state, _input(key, obs))
        # Content-derived identity: replaying the same observation from a
        # fresh state reproduces the same transition ID, and every dedup of
        # the same observation reproduces the same dedup ID.
        replayed = engine.apply(engine.initial_state(key), _input(key, obs))
        assert replayed.transitions[0].transition_id == first.transitions[0].transition_id
        assert again.transitions[0].transition_id == third.transitions[0].transition_id
        assert again.deduplicated and third.deduplicated


# =============================================================================
# 11 / 12. FSM transitions and invalid-transition rejection
# =============================================================================


class TestFsmTransitions:
    """Explicit legal transitions; no arbitrary state mutation."""

    def test_enter_and_exit_transition(self) -> None:
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        state, transitions = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("absent", 3, 3),
                ("absent", 4, 4),
            ),
        )
        assert state.current_state == "absent"
        assert [t.reason for t in transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.EXIT_CONFIRMED,
        ]
        assert state.state_since == _event(4)

    def test_invalid_transition_rejected_by_fsm_contract(self) -> None:
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            PRESENCE_FSM.transition("present", "enter_confirmed")
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            PRESENCE_FSM.transition("absent", "exit_confirmed")

    def test_fsm_duplicate_rule_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="duplicate rule"):
            DeterministicFsm(
                name="bad",
                version="0.1.0",
                states=("a", "b"),
                initial_state="a",
                rules=(
                    FsmRule("a", "go", "b"),
                    FsmRule("a", "go", "a"),
                ),
            )

    def test_fsm_unknown_state_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="undeclared state"):
            DeterministicFsm(
                name="bad",
                version="0.1.0",
                states=("a",),
                initial_state="a",
                rules=(FsmRule("a", "go", "zzz"),),
            )

    def test_session_closed_transition(self) -> None:
        engine = _engine()
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("present", 1, 1), ("session_closed", 2, 2)),
        )
        assert state.current_state == "absent"
        assert transitions[-1].reason is TemporalReason.SESSION_CLOSED


# =============================================================================
# 22. Hysteresis / jitter / occlusion foundation
# =============================================================================


class TestHysteresisAndOcclusion:
    """Configurable knobs; noisy boundaries never flip state rapidly."""

    def test_jitter_does_not_flip_state_within_grace(self) -> None:
        # Default policy: exit grace 30s. Rapid present/absent alternation
        # never counts toward exit and never flips PRESENT.
        engine = _engine()
        state, transitions = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("present", 3, 3),
                ("absent", 4, 4),
                ("present", 5, 5),
            ),
        )
        assert state.current_state == "present"
        assert all(t.reason is not TemporalReason.EXIT_CONFIRMED for t in transitions)

    def test_short_occlusion_gap_is_temporarily_missing_not_exit(self) -> None:
        engine = _engine(occlusion_tolerance_seconds=60.0, minimum_dwell_seconds=0)
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("present", 1, 1), ("not_observed", 10, 10)),
        )
        assert state.current_state == "present"  # NOT flipped by a gap
        assert state.occlusion_state is TemporalOcclusionState.TEMPORARILY_MISSING
        assert state.missing_since == _event(10)
        assert transitions[-1].reason is TemporalReason.OBSERVED_STAY

    def test_occlusion_beyond_tolerance_confirms_exit(self) -> None:
        engine = _engine(occlusion_tolerance_seconds=60.0, minimum_dwell_seconds=0)
        state, transitions = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("present", 12, 12),  # last positive presence
                ("not_observed", 80, 80),  # gap 68s > tolerance
            ),
        )
        assert state.current_state == "absent"
        assert transitions[-1].reason is TemporalReason.MISSING_EXPIRED

    def test_return_after_short_gap_resets_occlusion(self) -> None:
        engine = _engine(occlusion_tolerance_seconds=60.0, minimum_dwell_seconds=0)
        state, _ = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("not_observed", 10, 10),
                ("present", 12, 12),
            ),
        )
        assert state.current_state == "present"
        assert state.occlusion_state is TemporalOcclusionState.OBSERVED
        assert state.last_present_seen == _event(12)

    def test_entry_confirmation_streak_reset_by_absence(self) -> None:
        # present, absent, present, present -> enter only on the 4th.
        engine = _engine()
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("absent", 1, 1), ("present", 2, 2), ("present", 3, 3)),
        )
        assert state.current_state == "present"
        reasons = [t.reason for t in transitions]
        assert reasons.count(TemporalReason.ENTER_CONFIRMED) == 1
        assert state.state_since == _event(3)

    def test_policy_values_are_configuration_driven_not_hardcoded(self) -> None:
        # entry_confirmation=3 and a single qualifying absent exits.
        engine = _engine(
            entry_confirmation=3, exit_confirmation=1, minimum_dwell_seconds=0, exit_grace_seconds=0
        )
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("present", 1, 1), ("present", 2, 2), ("absent", 3, 3)),
        )
        assert state.current_state == "absent"
        assert [t.reason for t in transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
        ]


# =============================================================================
# 13-15 / 21. Checkpoints and restart recovery
# =============================================================================


class TestCheckpointAndRestart:
    """Serializable state; restore == uninterrupted processing."""

    def _base_timeline(self) -> tuple[tuple[str, int, int], ...]:
        return (("present", 0, 0), ("present", 1, 1), ("present", 2, 2))

    def test_checkpoint_serialization_round_trip(self) -> None:
        engine = _engine()
        state, _ = _process(engine, _key(), self._base_timeline())
        checkpoint = engine.checkpoint(state)
        data = checkpoint.to_dict()
        restored = TemporalCheckpoint.from_dict(data)
        assert restored == checkpoint
        assert restored.state == state
        assert restored.engine_version == TEMPORAL_ENGINE_VERSION

    def test_checkpoint_restoration(self) -> None:
        engine = _engine()
        state, _ = _process(engine, _key(), self._base_timeline())
        checkpoint = engine.checkpoint(state)
        assert engine.restore(checkpoint) == state

    def test_restart_recovery_equals_uninterrupted(self) -> None:
        key = _key()
        engine = _engine()
        # Uninterrupted: A B C D E
        full_state, full_transitions = _process(
            engine,
            key,
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("present", 2, 2),
                ("present", 3, 3),
                ("present", 4, 4),
            ),
        )
        # Restarted: A B C -> checkpoint -> (new engine instance) -> D E
        mid_state, mid_transitions = _process(
            engine,
            key,
            (("present", 0, 0), ("present", 1, 1), ("present", 2, 2)),
        )
        checkpoint = engine.checkpoint(mid_state)
        restarted = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=TemporalPolicy())
        restored = restarted.restore(checkpoint)
        restarted_state = restored
        restarted_transitions = list(mid_transitions)
        for kind, seconds, frame_index in (
            ("present", 3, 3),
            ("present", 4, 4),
        ):
            obs = _obs(key, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
            result = restarted.apply(restarted_state, _input(key, obs, kind=kind))
            restarted_state = result.state
            restarted_transitions.extend(result.transitions)

        assert restarted_state == full_state
        assert restarted_transitions == full_transitions

    def test_fsm_version_mismatch_rejected(self) -> None:
        engine = _engine()
        state, _ = _process(engine, _key(), self._base_timeline())
        checkpoint = engine.checkpoint(state)
        drifted = checkpoint.model_copy(
            update={"state": state.model_copy(update={"fsm_version": "9.9.9"})}
        )
        with pytest.raises(FsmVersionMismatchError, match="FSM version"):
            engine.restore(drifted)

    def test_engine_version_mismatch_rejected(self) -> None:
        engine = _engine()
        state, _ = _process(engine, _key(), self._base_timeline())
        checkpoint = engine.checkpoint(state)
        drifted = checkpoint.model_copy(update={"engine_version": "9.9.9"})
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            engine.restore(drifted)

    def test_policy_revision_mismatch_rejected(self) -> None:
        engine = _engine()
        state, _ = _process(engine, _key(), self._base_timeline())
        checkpoint = engine.checkpoint(state)
        drifted = checkpoint.model_copy(update={"policy_revision": "v9"})
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            engine.restore(drifted)

    def test_checkpoint_carries_configuration_version_provenance(self) -> None:
        engine = _engine()
        config_version = ConfigurationVersionId(UUID("99990000-0000-0000-0000-000000000001"))
        key = _key(configuration_version_id=config_version)
        state, _ = _process(engine, key, self._base_timeline())
        checkpoint = engine.checkpoint(state)
        assert checkpoint.state.key.configuration_version_id == config_version
        restored = engine.restore(checkpoint)
        assert restored.key.configuration_version_id == config_version


# =============================================================================
# 7-10. Missing/invalid inputs and provenance
# =============================================================================


class TestInvalidInputs:
    """Missing or malformed inputs fail with typed errors — never repaired."""

    def test_missing_timestamp_rejected_at_contract(self) -> None:
        # The canonical contract requires a timezone-aware event_time; the
        # engine defensively rejects any that slip through.
        with pytest.raises(ValueError):
            SpatialObservation(
                session_id=_SESSION,
                track_id=_TRACK,
                frame_id=_frame(0),
                event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                spatial_point=SpatialPointModel(x=0.5, y=0.5, policy=SpatialPointPolicy.FOOTPOINT),
                status=SpatialStatus.INSIDE,
            )

    def test_naive_processing_time_rejected_by_engine(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        with pytest.raises(InvalidTemporalInputError, match="processing_time"):
            engine.apply(
                engine.initial_state(key),
                TemporalInput(
                    key=key,
                    observation=obs,
                    observation_kind="present",
                    processing_time=datetime(2026, 8, 1, 11, 0, 0),  # naive
                ),
            )

    def test_unknown_observation_kind_rejected(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(
                engine.initial_state(key),
                TemporalInput(
                    key=key, observation=obs, observation_kind="warp", processing_time=_processing()
                ),
            )

    def test_unknown_kind_rejected_on_dedup_path(self) -> None:
        # Kind membership is validated BEFORE the ordering/dedup branches,
        # so a duplicate observation with a bad kind is rejected, never a
        # silent DEDUPLICATED fact.
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        state = engine.apply(engine.initial_state(key), _input(key, obs)).state
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(
                state,
                TemporalInput(
                    key=key, observation=obs, observation_kind="warp", processing_time=_processing()
                ),
            )

    def test_unknown_kind_rejected_on_reorder_path(self) -> None:
        engine = _engine()
        key = _key()
        state = engine.apply(
            engine.initial_state(key),
            _input(key, _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))),
        ).state
        state = engine.apply(
            state,
            _input(key, _obs(key, kind="present", event_time=_event(2), frame_id=_frame(2))),
        ).state
        late = _obs(key, kind="present", event_time=_event(1), frame_id=_frame(1))
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(
                state,
                TemporalInput(
                    key=key,
                    observation=late,
                    observation_kind="warp",
                    processing_time=_processing(),
                ),
            )

    def test_cross_fsm_state_rejected(self) -> None:
        # A state whose key names a DIFFERENT FSM family must never be
        # processed by this engine (silently wrong semantics).
        engine = _engine()
        dwell_key = _key(fsm_kind="dwell")
        obs = _obs(dwell_key, kind="present", event_time=_event(0), frame_id=_frame(0))
        state = TemporalState(
            fsm_version=TEMPORAL_ENGINE_VERSION,
            key=dwell_key,
            current_state="absent",
        )
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.apply(state, _input(dwell_key, obs))

    def test_cross_fsm_restore_rejected(self) -> None:
        engine = _engine()
        dwell_key = _key(fsm_kind="dwell")
        state = TemporalState(
            fsm_version=TEMPORAL_ENGINE_VERSION,
            key=dwell_key,
            current_state="absent",
        )
        checkpoint = TemporalCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision=engine.policy.revision,
            state=state,
        )
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.restore(checkpoint)

    def test_wrong_input_types_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(InvalidTemporalInputError, match="state"):
            engine.apply(
                "not-a-state",
                _input(
                    _key(), _obs(_key(), kind="present", event_time=_event(0), frame_id=_frame(0))
                ),
            )  # type: ignore[arg-type]
        with pytest.raises(InvalidTemporalInputError, match="input"):
            engine.apply(engine.initial_state(_key()), "not-an-input")  # type: ignore[arg-type]

    def test_missing_track_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="presence",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
            )

    def test_missing_session_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="presence",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="presence",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                track_id=_TRACK,
            )

    def test_track_mismatch_between_key_and_observation(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        wrong = _key(track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")))
        with pytest.raises(StateKeyMismatchError, match="track_id"):
            engine.apply(engine.initial_state(key), _input(wrong, obs))

    def test_session_mismatch_between_key_and_observation(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        wrong = _key(session_id=VideoSessionId(UUID("30000000-0000-0000-0000-000000000099")))
        with pytest.raises(StateKeyMismatchError, match="session_id"):
            engine.apply(engine.initial_state(key), _input(wrong, obs))

    def test_configuration_version_mismatch_between_key_and_observation(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        wrong = _key(
            configuration_version_id=ConfigurationVersionId(
                UUID("50000000-0000-0000-0000-000000000099")
            )
        )
        with pytest.raises(StateKeyMismatchError, match="configuration_version_id"):
            engine.apply(engine.initial_state(key), _input(wrong, obs))


# =============================================================================
# 16-19. Isolation: tenant / venue / session / configuration version
# =============================================================================


class TestIsolation:
    """State never mixes across tenant, venue, session, or config version."""

    def test_tenant_isolation(self) -> None:
        engine = _engine()
        key_a = _key(tenant_id=TenantId(UUID("10000000-0000-0000-0000-000000000001")))
        key_b = _key(tenant_id=TenantId(UUID("10000000-0000-0000-0000-000000000099")))
        state_a, _ = _process(engine, key_a, (("present", 0, 0), ("present", 1, 1)))
        state_b, _ = _process(engine, key_b, (("present", 0, 0), ("present", 1, 1)))
        assert state_a.key.tenant_id != state_b.key.tenant_id
        assert state_a.current_state == "present"
        assert state_b.current_state == "present"
        # Identities differ — transitions are scoped to their key.
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_venue_isolation(self) -> None:
        engine = _engine()
        key_a = _key(venue_id=_VENUE)
        key_b = _key(venue_id=VenueId(UUID("20000000-0000-0000-0000-000000000099")))
        state_a, _ = _process(engine, key_a, (("present", 0, 0), ("present", 1, 1)))
        state_b, _ = _process(engine, key_b, (("present", 0, 0), ("present", 1, 1)))
        assert state_a.key.venue_id != state_b.key.venue_id
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_session_isolation(self) -> None:
        engine = _engine()
        state_a, _ = _process(engine, _key(), (("present", 0, 0), ("present", 1, 1)))
        state_b, _ = _process(
            engine,
            _key(session_id=VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))),
            (("present", 0, 0), ("present", 1, 1)),
        )
        assert state_a.key.session_id != state_b.key.session_id
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_configuration_version_isolation(self) -> None:
        engine = _engine()
        state_a, _ = _process(engine, _key(), (("present", 0, 0), ("present", 1, 1)))
        state_b, _ = _process(
            engine,
            _key(
                configuration_version_id=ConfigurationVersionId(
                    UUID("50000000-0000-0000-0000-000000000099")
                )
            ),
            (("present", 0, 0), ("present", 1, 1)),
        )
        assert state_a.key.configuration_version_id != state_b.key.configuration_version_id
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_semantic_context_scopes_state(self) -> None:
        engine = _engine()
        zone = _key(semantic_context="z-lobby")
        table = _key(semantic_context="t-12")
        state_zone, _ = _process(engine, zone, (("present", 0, 0), ("present", 1, 1)))
        state_table, _ = _process(engine, table, (("present", 0, 0), ("present", 1, 1)))
        assert state_zone.key.canonical() != state_table.key.canonical()


# =============================================================================
# 19-21. Golden timeline, reordering, and restart (mandatory scenarios)
# =============================================================================


class TestGoldenTimeline:
    """The same timeline replayed produces byte-identical output."""

    TIMELINE = (
        ("present", 0, 0),
        ("present", 1, 1),
        ("present", 2, 2),
        ("present", 3, 3),
        ("present", 4, 4),
    )

    def _run(self, engine, key):
        return _process(engine, key, self.TIMELINE)

    def test_replay_is_identical(self) -> None:
        engine = _engine()
        first_state, first_transitions = self._run(engine, _key())
        second_state, second_transitions = self._run(engine, _key())
        assert second_state == first_state
        assert second_transitions == first_transitions
        # The recorded ledger fields are exactly as specified.
        assert [t.reason for t in first_transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.OBSERVED_STAY,
        ]
        assert first_state.state_since == _event(1)
        assert first_state.last_seen == _event(4)
        assert first_state.watermark_event_time == _event(4)

    def test_transition_ids_are_deterministic_across_runs(self) -> None:
        engine = _engine()
        _, first = self._run(engine, _key())
        _, second = self._run(engine, _key())
        assert [t.transition_id for t in first] == [t.transition_id for t in second]


class TestReorderingScenario:
    """A -> C -> B must not corrupt state; final state is deterministic."""

    def test_reordered_sequence_matches_chronological_final_state(self) -> None:
        engine = _engine()
        key = _key()
        reordered_state, _ = _process(
            engine, key, (("present", 0, 0), ("present", 2, 2), ("present", 1, 1))
        )
        chronological_state, _ = _process(
            engine, key, (("present", 0, 0), ("present", 1, 1), ("present", 2, 2))
        )
        assert reordered_state.current_state == chronological_state.current_state
        assert reordered_state.watermark_event_time == chronological_state.watermark_event_time
        # Deterministic replay of the reordered sequence is identical.
        replay_state, replay_transitions = _process(
            engine, key, (("present", 0, 0), ("present", 2, 2), ("present", 1, 1))
        )
        assert replay_state == reordered_state
        assert [t.reason for t in replay_transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.REORDERED,
        ]


# =============================================================================
# 23. Long-duration / boundedness
# =============================================================================


class TestLongDuration:
    """A long session stays bounded, serializable, and watermark-progressing."""

    def test_state_remains_bounded_and_checkpointable(self) -> None:
        engine = _engine()
        key = _key()
        state = engine.initial_state(key)
        limit = engine.policy.transition_history_limit
        for i in range(10_000):
            kind = "present" if i % 97 != 0 else "absent"  # deterministic noise
            obs = _obs(key, kind=kind, event_time=_event(i), frame_id=_frame(i))
            result = engine.apply(state, _input(key, obs, kind=kind))
            state = result.state
            # Watermark always advances in event-time order.
            assert state.watermark_event_time == _event(i)
        assert len(state.recent_transitions) == min(limit, 10_000)
        assert state.watermark_event_time == _event(9_999)
        assert state.last_applied_frame_id == _frame(9_999)
        # The checkpoint is serializable at full size.
        checkpoint = engine.checkpoint(state)
        assert TemporalCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint
        # Bounded: the ring never grows past the configured limit.
        assert len(state.recent_transitions) <= limit


# =============================================================================
# Presence-kind structural mapping
# =============================================================================


class TestPresenceKindMapping:
    """SpatialStatus -> presence-kind is deterministic and structural."""

    def test_mapping(self) -> None:
        assert presence_kind(_status_obs(SpatialStatus.INSIDE)) == "present"
        assert presence_kind(_status_obs(SpatialStatus.AMBIGUOUS)) == "present"
        assert presence_kind(_status_obs(SpatialStatus.OUTSIDE)) == "absent"
        assert presence_kind(_status_obs(SpatialStatus.EXCLUDED)) == "not_observed"
        assert presence_kind(_status_obs(SpatialStatus.PRIVACY)) == "not_observed"


def _status_obs(status: SpatialStatus) -> SpatialObservation:
    return SpatialObservation(
        session_id=_SESSION,
        track_id=_TRACK,
        frame_id=_frame(0),
        event_time=_event(0),
        camera_id=_CAMERA,
        configuration_version_id=_CONFIG,
        spatial_point=SpatialPointModel(x=0.5, y=0.5, policy=SpatialPointPolicy.FOOTPOINT),
        status=status,
    )


# =============================================================================
# Pure-core boundary
# =============================================================================


class TestEnginePurity:
    """The temporal package performs no I/O and has no hidden state."""

    def test_no_io_imports_in_temporal_package(self) -> None:
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
        for path in sorted(package_dir.glob("*.py")):
            text = path.read_text()
            for module in forbidden:
                assert not re.search(rf"^\s*(from|import)\s+{module}\b", text, re.MULTILINE), (
                    f"I/O/stateful module {module!r} leaked into {path.name}"
                )

    def test_no_print_or_debug_leftovers(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
        for path in sorted(package_dir.glob("*.py")):
            text = path.read_text()
            assert "print(" not in text, f"print() leaked into {path.name}"
            assert "TODO" not in text and "FIXME" not in text, f"TODO/FIXME leaked into {path.name}"

    def test_no_current_time_read_in_engine(self) -> None:
        # The engine must never read the current time to resolve event order.
        engine_text = (
            Path(__file__).resolve().parents[2]
            / "backend"
            / "app"
            / "intelligence"
            / "temporal"
            / "engine.py"
        ).read_text()
        assert "now(" not in engine_text
        assert "utc_now" not in engine_text
