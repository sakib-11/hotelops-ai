"""Tests for the Task 15.2 enter/exit FSM (full four-state presence model).

Built on the Task 15.1 foundation (event-time discipline, watermark,
late/out-of-order policy, idempotent dedup, checkpoint/restore,
isolation) — this suite verifies the enter/exit semantics themselves:

- the four explicit states ABSENT / ENTERING / PRESENT / EXITING and the
  six canonical transitions (entry, entry confirmation, entry abort,
  exit, exit confirmation, exit recovery);
- entry/exit confirmation are config-driven (never hardcoded), including
  the degenerate ``entry_confirmation == 1`` / ``exit_confirmation == 1``
  direct paths;
- hysteresis/grace/occlusion prevent repeated ENTER -> EXIT -> ENTER
  from boundary jitter and short track loss;
- idempotency (duplicate observation advances once), ordering (late and
  out-of-order follow the 15.1 policy), checkpoint/restart recovery
  equal to uninterrupted processing;
- isolation across tenant / venue / session / track / configuration
  version / zone-table semantic context;
- golden timeline (exact states, transitions, timestamps) and failure
  timeline (grace prevents an incorrect exit);
- long-session boundedness, invalid-input rejection, and the pure-core
  boundary (no I/O, no current-time reads).

All fixtures use the REAL canonical contracts (SpatialObservation,
TemporalStateKey, TemporalPolicy, TemporalCheckpoint) with fixed
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
    PresenceTemporalEngine,
    TemporalEngine,
    TemporalInput,
    presence_kind,
)
from backend.app.intelligence.temporal.exceptions import (
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
_PROCESSING_BASE = datetime(2026, 8, 1, 11, 0, 0, tzinfo=UTC)


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


def _status_obs(
    key: TemporalStateKey,
    *,
    status: SpatialStatus,
    event_time: datetime,
    frame_id: FrameId,
) -> SpatialObservation:
    """Canonical SpatialObservation with an explicit spatial status."""
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


def _obs(
    key: TemporalStateKey,
    *,
    kind: str,
    event_time: datetime,
    frame_id: FrameId,
) -> SpatialObservation:
    """Canonical SpatialObservation consistent with a presence ``kind``."""
    if kind == "present":
        status = SpatialStatus.INSIDE
    elif kind == "absent":
        status = SpatialStatus.OUTSIDE
    else:  # not_observed / session_closed
        status = SpatialStatus.EXCLUDED if kind == "not_observed" else SpatialStatus.OUTSIDE
    return _status_obs(key, status=status, event_time=event_time, frame_id=frame_id)


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


def _process_steps(
    engine: TemporalEngine,
    key: TemporalStateKey,
    timeline: tuple[tuple[str, int, int], ...],
) -> list[tuple[str, TemporalTransition]]:
    """Record the state and transition after every step (deterministic)."""
    state = engine.initial_state(key)
    steps: list[tuple[str, TemporalTransition]] = []
    for kind, seconds, frame_index in timeline:
        obs = _obs(key, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
        result = engine.apply(state, _input(key, obs, kind=kind))
        state = result.state
        steps.append((state.current_state, result.transitions[0]))
    return steps


# =============================================================================
# §3/§20(1-8). Canonical state transitions
# =============================================================================


class TestPresenceTransitions:
    """The six canonical transitions of the four-state enter/exit FSM."""

    def test_absent_to_entering_on_first_present(self) -> None:
        # 1. ABSENT -> ENTERING on the first positive observation.
        engine = _engine()  # entry_confirmation=2
        state, transitions = _process(engine, _key(), (("present", 0, 0),))
        assert state.current_state == "entering"
        assert state.entry_confirm_count == 1
        assert state.state_since == _event(0)
        assert transitions[0].reason is TemporalReason.OBSERVED_STAY
        assert transitions[0].from_state == "absent"
        assert transitions[0].to_state == "entering"

    def test_entering_to_present_on_entry_confirmation(self) -> None:
        # 2. ENTERING -> PRESENT once entry confirmation is satisfied.
        engine = _engine()
        state, transitions = _process(engine, _key(), (("present", 0, 0), ("present", 1, 1)))
        assert state.current_state == "present"
        assert state.state_since == _event(1)
        assert transitions[1].reason is TemporalReason.ENTER_CONFIRMED
        assert transitions[1].from_state == "entering"
        assert transitions[1].to_state == "present"

    def test_entering_to_absent_when_presence_lost(self) -> None:
        # 3. ENTERING -> ABSENT when presence is lost before confirmation.
        engine = _engine()
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("absent", 1, 1), ("present", 2, 2), ("present", 3, 3)),
        )
        assert state.current_state == "present"
        assert transitions[1].from_state == "entering"
        assert transitions[1].to_state == "absent"  # entry aborted
        # The abort resets the streak: entry confirmed only at event(3).
        assert [t.reason for t in transitions].count(TemporalReason.ENTER_CONFIRMED) == 1
        assert state.state_since == _event(3)

    def test_present_to_exiting_on_qualified_absent(self) -> None:
        # 4. PRESENT -> EXITING on the first dwell/grace-qualified absence.
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("present", 1, 1), ("absent", 2, 2)),
        )
        assert state.current_state == "exiting"
        assert state.exit_confirm_count == 1
        assert state.state_since == _event(2)
        assert transitions[2].from_state == "present"
        assert transitions[2].to_state == "exiting"
        assert transitions[2].reason is TemporalReason.OBSERVED_STAY  # no EXIT fact yet

    def test_exiting_to_absent_on_exit_confirmation(self) -> None:
        # 5. EXITING -> ABSENT once exit confirmation is satisfied.
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
        assert state.state_since == _event(4)
        assert transitions[4].reason is TemporalReason.EXIT_CONFIRMED
        assert transitions[4].from_state == "exiting"
        assert transitions[4].to_state == "absent"

    def test_exiting_to_present_on_return(self) -> None:
        # 6. EXITING -> PRESENT when presence returns during confirmation.
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        state, transitions = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("present", 3, 3),
            ),
        )
        assert state.current_state == "present"
        assert state.state_since == _event(3)
        assert state.exit_confirm_count == 0  # streak reset by recovery
        assert transitions[3].from_state == "exiting"
        assert transitions[3].to_state == "present"
        # Recovery is NOT a new ENTER/EXIT fact (the entity never left).
        assert [t.reason for t in transitions].count(TemporalReason.ENTER_CONFIRMED) == 1
        assert [t.reason for t in transitions].count(TemporalReason.EXIT_CONFIRMED) == 0

    def test_instant_entry_and_exit_when_confirmation_is_one(self) -> None:
        # Degenerate configured policy: a single observation confirms both.
        engine = _engine(
            entry_confirmation=1, exit_confirmation=1, minimum_dwell_seconds=0, exit_grace_seconds=0
        )
        _, transitions = _process(engine, _key(), (("present", 0, 0), ("absent", 1, 1)))
        assert [t.from_state for t in transitions] == ["absent", "present"]
        assert [t.to_state for t in transitions] == ["present", "absent"]
        assert [t.reason for t in transitions] == [
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
        ]

    def test_stable_present_observations_keep_state_and_state_since(self) -> None:
        engine = _engine()
        state, _ = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("present", 2, 2),
                ("present", 3, 3),
                ("present", 4, 4),
            ),
        )
        assert state.current_state == "present"
        assert state.state_since == _event(1)  # never reset while staying PRESENT
        assert state.last_seen == _event(4)
        assert state.last_present_seen == _event(4)

    def test_stable_absent_state(self) -> None:
        engine = _engine()
        state, _ = _process(engine, _key(), (("absent", 0, 0), ("absent", 1, 1), ("absent", 2, 2)))
        assert state.current_state == "absent"
        assert state.entry_confirm_count == 0
        assert state.watermark_event_time == _event(2)


# =============================================================================
# §6/§7/§20(9-10). Confirmation configuration
# =============================================================================


class TestConfirmationConfiguration:
    """Entry/exit confirmation thresholds are configuration-driven."""

    def test_entry_confirmation_is_config_driven(self) -> None:
        engine = _engine(entry_confirmation=3)
        steps = _process_steps(
            engine, _key(), (("present", 0, 0), ("present", 1, 1), ("present", 2, 2))
        )
        assert [state for state, _ in steps] == ["entering", "entering", "present"]
        assert [t.reason for _, t in steps] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
        ]

    def test_exit_confirmation_is_config_driven(self) -> None:
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=2)
        steps = _process_steps(
            engine,
            _key(),
            (("present", 0, 0), ("present", 1, 1), ("absent", 2, 2), ("absent", 3, 3)),
        )
        assert [state for state, _ in steps] == ["entering", "present", "exiting", "absent"]
        assert [t.reason for _, t in steps] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.OBSERVED_STAY,
            TemporalReason.EXIT_CONFIRMED,
        ]


# =============================================================================
# §8/§9/§20(11-12)/§22. Occlusion and boundary jitter
# =============================================================================


class TestOcclusionAndJitter:
    """Short occlusion and boundary jitter never flip state rapidly."""

    def test_short_occlusion_does_not_exit(self) -> None:
        engine = _engine(occlusion_tolerance_seconds=60.0, minimum_dwell_seconds=0)
        state, transitions = _process(
            engine,
            _key(),
            (("present", 0, 0), ("present", 1, 1), ("present", 12, 12), ("not_observed", 20, 20)),
        )
        assert state.current_state == "present"  # gap 8s < tolerance
        assert state.occlusion_state is TemporalOcclusionState.TEMPORARILY_MISSING
        assert state.missing_since == _event(20)
        assert [t.reason for t in transitions].count(TemporalReason.EXIT_CONFIRMED) == 0

    def test_occlusion_during_entry_aborts_unconfirmed_entry(self) -> None:
        # Recorded decision: presence lost (even via missing) before entry
        # confirmation aborts the entry — the entity was never confirmed.
        engine = _engine()
        state, _ = _process(engine, _key(), (("present", 0, 0), ("not_observed", 1, 1)))
        assert state.current_state == "absent"

    def test_occlusion_tolerance_does_not_apply_during_entering(self) -> None:
        # Recorded decision: occlusion tolerance protects only the
        # CONFIRMED PRESENT state — a missing observation during ENTERING
        # aborts regardless of gap length.
        engine = _engine(occlusion_tolerance_seconds=60.0)
        steps = _process_steps(engine, _key(), (("present", 0, 0), ("not_observed", 100, 100)))
        assert [state for state, _ in steps] == ["entering", "absent"]

    def test_missing_during_exit_confirmation_counts_toward_exit(self) -> None:
        # A not_observed while EXITING is additional departure evidence
        # and counts toward exit confirmation (consistent with absent).
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        steps = _process_steps(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("not_observed", 3, 3),
                ("absent", 4, 4),
            ),
        )
        assert [state for state, _ in steps] == [
            "entering",
            "present",
            "exiting",
            "exiting",
            "absent",
        ]
        assert [t.reason for _, t in steps].count(TemporalReason.EXIT_CONFIRMED) == 1
        assert steps[-1][1].event_time == _event(4)

    def test_session_closed_from_entering_and_exiting(self) -> None:
        # Explicit session closure finalizes the FSM from ANY state.
        engine = _engine()
        entering_state, entering_transitions = _process(
            engine, _key(), (("present", 0, 0), ("session_closed", 1, 1))
        )
        assert entering_state.current_state == "absent"
        assert entering_transitions[-1].reason is TemporalReason.SESSION_CLOSED
        assert entering_transitions[-1].from_state == "entering"

        exiting_engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        exiting_state, exiting_transitions = _process(
            exiting_engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("session_closed", 3, 3),
            ),
        )
        assert exiting_state.current_state == "absent"
        assert exiting_transitions[-1].reason is TemporalReason.SESSION_CLOSED
        assert exiting_transitions[-1].from_state == "exiting"

    def test_occlusion_expiry_after_tolerance_confirms_exit(self) -> None:
        engine = _engine(occlusion_tolerance_seconds=60.0, minimum_dwell_seconds=0)
        state, transitions = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("present", 12, 12),
                ("not_observed", 80, 80),
            ),
        )
        assert state.current_state == "absent"
        assert transitions[-1].reason is TemporalReason.MISSING_EXPIRED

    def test_boundary_jitter_after_entry_produces_no_repeat_transitions(self) -> None:
        # inside/outside/inside/outside/inside around a PRESENT entity:
        # hysteresis (grace) means the entity remains present — no repeated
        # ENTER/EXIT facts.
        engine = _engine()  # exit_grace=30s, exit_confirmation=3, dwell=10s
        state, transitions = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("present", 2, 2),
                ("absent", 3, 3),
                ("present", 4, 4),
                ("absent", 5, 5),
                ("present", 6, 6),
            ),
        )
        assert state.current_state == "present"
        assert [t.reason for t in transitions].count(TemporalReason.ENTER_CONFIRMED) == 1
        assert [t.reason for t in transitions].count(TemporalReason.EXIT_CONFIRMED) == 0

    def test_boundary_jitter_before_entry_never_confirms(self) -> None:
        # inside/outside/inside/outside/inside before any confirmation:
        # each absence aborts the pending entry, so no ENTER is emitted.
        engine = _engine(entry_confirmation=2)
        state, transitions = _process(
            engine,
            _key(),
            (
                ("absent", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("present", 3, 3),
                ("absent", 4, 4),
                ("present", 5, 5),
            ),
        )
        assert state.current_state == "entering"
        assert [t.reason for t in transitions].count(TemporalReason.ENTER_CONFIRMED) == 0
        assert [t.reason for t in transitions].count(TemporalReason.EXIT_CONFIRMED) == 0

    def test_failure_timeline_grace_prevents_incorrect_exit(self) -> None:
        # §22: PRESENT, missing, missing, PRESENT — the configured grace
        # policy must prevent an incorrect exit (no EXITING, no EXIT).
        engine = _engine()  # occlusion_tolerance=60s
        steps = _process_steps(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("not_observed", 2, 2),
                ("not_observed", 3, 3),
                ("present", 4, 4),
            ),
        )
        assert [state for state, _ in steps] == [
            "entering",
            "present",
            "present",
            "present",
            "present",
        ]
        assert all(state != "exiting" for state, _ in steps)
        assert all(t.reason is not TemporalReason.EXIT_CONFIRMED for _, t in steps)


# =============================================================================
# §11/§12/§20(13-16). Idempotency and ordering
# =============================================================================


class TestOrderingAndIdempotency:
    """Duplicates advance once; late/out-of-order follow the 15.1 policy."""

    def test_duplicate_observation_is_idempotent(self) -> None:
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        first = engine.apply(engine.initial_state(key), _input(key, obs))
        second = engine.apply(first.state, _input(key, obs))
        assert not first.deduplicated
        assert second.deduplicated
        assert second.state == first.state  # one logical transition only
        assert second.transitions[0].reason is TemporalReason.DEDUPLICATED
        # Deterministic content-derived identity across replay.
        replayed = engine.apply(engine.initial_state(key), _input(key, obs))
        assert replayed.transitions[0].transition_id == first.transitions[0].transition_id

    def test_late_observation_within_window_is_reordered(self) -> None:
        engine = _engine()
        state, transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 2, 2), ("present", 1, 1))
        )
        assert state.current_state == "present"
        assert state.watermark_event_time == _event(2)  # watermark never rewinds
        assert transitions[-1].reason is TemporalReason.REORDERED

    def test_late_observation_beyond_window_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(LateEventError, match="reordering window"):
            _process(
                engine, _key(), (("present", 0, 0), ("present", 120, 120), ("present", 30, 30))
            )

    def test_out_of_order_does_not_corrupt_state(self) -> None:
        # A(0) inside, C(2) inside, B(1) outside: B is within the window
        # and must NOT count toward exit — accept-with-no-rewind.
        engine = _engine()
        state, transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 2, 2), ("absent", 1, 1))
        )
        assert state.current_state == "present"
        assert [t.reason for t in transitions] == [
            TemporalReason.OBSERVED_STAY,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.REORDERED,
        ]
        # Deterministic replay of the same reordered sequence.
        replay_state, replay_transitions = _process(
            engine, _key(), (("present", 0, 0), ("present", 2, 2), ("absent", 1, 1))
        )
        assert replay_state == state
        assert replay_transitions == transitions

    def test_timestamp_regression_rejected(self) -> None:
        engine = _engine(reorder_window_seconds=1.0)
        with pytest.raises(LateEventError):
            _process(engine, _key(), (("present", 3, 3), ("present", 1, 1)))


# =============================================================================
# §17/§18/§20(17-18). Checkpoint and restart recovery
# =============================================================================


class TestCheckpointAndRestart:
    """Checkpoints serialize the 4-state FSM; restart equals uninterrupted."""

    def test_checkpoint_serialization_round_trip(self) -> None:
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        state, _ = _process(
            engine,
            _key(),
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("absent", 3, 3),
            ),
        )
        assert state.current_state == "exiting"
        checkpoint = engine.checkpoint(state)
        restored = TemporalCheckpoint.from_dict(checkpoint.to_dict())
        assert restored == checkpoint
        assert restored.state == state
        assert restored.engine_version == TEMPORAL_ENGINE_VERSION

    def test_restart_recovery_equals_uninterrupted(self) -> None:
        # ABSENT -> ENTERING -> checkpoint -> restart -> PRESENT (§18).
        key = _key()
        engine = _engine()
        uninterrupted, _ = _process(
            engine, key, (("present", 0, 0), ("present", 1, 1), ("present", 2, 2))
        )
        mid_state, _ = _process(engine, key, (("present", 0, 0), ("present", 1, 1)))
        assert mid_state.current_state == "present"
        checkpoint = engine.checkpoint(mid_state)
        restarted = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=TemporalPolicy())
        restored = restarted.restore(checkpoint)
        result = restarted.apply(
            restored,
            _input(key, _obs(key, kind="present", event_time=_event(2), frame_id=_frame(2))),
        )
        assert result.state == uninterrupted

    def test_restart_mid_exit_equals_uninterrupted(self) -> None:
        # Checkpoint while EXITING (after the first qualified absence).
        key = _key()
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        uninterrupted, uninterrupted_transitions = _process(
            engine,
            key,
            (
                ("present", 0, 0),
                ("present", 1, 1),
                ("absent", 2, 2),
                ("absent", 3, 3),
                ("absent", 4, 4),
            ),
        )
        mid_state, _ = _process(
            engine,
            key,
            (("present", 0, 0), ("present", 1, 1), ("absent", 2, 2)),
        )
        assert mid_state.current_state == "exiting"
        checkpoint = engine.checkpoint(mid_state)
        restarted = PresenceTemporalEngine(
            fsm=PRESENCE_FSM,
            policy=TemporalPolicy(
                minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3
            ),
        )
        restored = restarted.restore(checkpoint)
        resumed = restored
        resumed_transitions: list[TemporalTransition] = []
        for kind, seconds, frame_index in (("absent", 3, 3), ("absent", 4, 4)):
            obs = _obs(key, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
            result = restarted.apply(resumed, _input(key, obs, kind=kind))
            resumed = result.state
            resumed_transitions.extend(result.transitions)
        assert resumed == uninterrupted
        assert resumed_transitions == uninterrupted_transitions[3:]


# =============================================================================
# §5/§17/§20(19-24). Isolation across every canonical scope
# =============================================================================


class TestIsolation:
    """State never mixes across tenant, venue, session, track, or config."""

    def _entered(self, engine: TemporalEngine, key: TemporalStateKey) -> TemporalState:
        state, _ = _process(engine, key, (("present", 0, 0), ("present", 1, 1)))
        return state

    def test_tenant_isolation(self) -> None:
        engine = _engine()
        other_tenant = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        state_a = self._entered(engine, _key())
        state_b = self._entered(engine, _key(tenant_id=other_tenant))
        assert state_a.key.tenant_id != state_b.key.tenant_id
        assert state_a.current_state == "present"
        assert state_b.current_state == "present"
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_venue_isolation(self) -> None:
        engine = _engine()
        other_venue = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        state_a = self._entered(engine, _key())
        state_b = self._entered(engine, _key(venue_id=other_venue))
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_session_isolation(self) -> None:
        engine = _engine()
        other_session = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        state_a = self._entered(engine, _key())
        state_b = self._entered(engine, _key(session_id=other_session))
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_track_isolation(self) -> None:
        engine = _engine()
        other_track = TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        state_a = self._entered(engine, _key())
        state_b = self._entered(engine, _key(track_id=other_track))
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_configuration_version_isolation(self) -> None:
        engine = _engine()
        other_config = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000099"))
        state_a = self._entered(engine, _key())
        state_b = self._entered(engine, _key(configuration_version_id=other_config))
        assert (
            state_a.recent_transitions[0].transition_id
            != state_b.recent_transitions[0].transition_id
        )

    def test_zone_and_table_do_not_share_state(self) -> None:
        # §5: Track + Zone and Track + Table must not accidentally share
        # state — the semantic_context component of the key scopes them.
        engine = _engine()
        zone = _key(semantic_context="z-lobby")
        table = _key(semantic_context="t-12")
        state_zone = self._entered(engine, zone)
        state_table = self._entered(engine, table)
        assert zone.canonical() != table.canonical()
        assert state_zone.key.semantic_context == "z-lobby"
        assert state_table.key.semantic_context == "t-12"
        assert (
            state_zone.recent_transitions[0].transition_id
            != state_table.recent_transitions[0].transition_id
        )

    def test_configuration_version_pinned_for_historical_sessions(self) -> None:
        # §17: a session pinned to Config V1 keeps V1 semantics even after
        # V2 is published — the key carries the pinned version.
        engine = _engine()
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        key_v1 = _key(configuration_version_id=v1)
        state_v1 = self._entered(engine, key_v1)
        assert state_v1.key.configuration_version_id == v1
        # V2 processing is an independent state machine.
        state_v2 = self._entered(engine, _key(configuration_version_id=v2))
        assert state_v2.key.configuration_version_id == v2
        assert (
            state_v1.recent_transitions[0].transition_id
            != state_v2.recent_transitions[0].transition_id
        )


# =============================================================================
# §13/§14. ENTER/EXIT fact provenance
# =============================================================================


class TestEnterExitFactProvenance:
    """The ENTER/EXIT temporal facts preserve the full canonical provenance."""

    def test_enter_fact_provenance(self) -> None:
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=3)
        key = _key(semantic_context="z-lobby")
        state, transitions = _process(
            engine, key, (("present", 0, 0), ("present", 1, 1), ("absent", 2, 2))
        )
        enter = next(t for t in transitions if t.reason is TemporalReason.ENTER_CONFIRMED)
        assert enter.event_time == _event(1)
        assert enter.configuration_version_id == key.configuration_version_id
        assert enter.fsm_version == TEMPORAL_ENGINE_VERSION
        assert enter.key is not None and enter.key == key
        assert enter.event_kind == "present"
        assert enter.observation_frame_id == _frame(1)
        # The spatial context is carried on the key (never invented anew).
        assert enter.key.semantic_context == "z-lobby"
        # EXIT has not been emitted yet — the entity is EXITING.
        assert state.current_state == "exiting"

    def test_exit_fact_provenance(self) -> None:
        engine = _engine(minimum_dwell_seconds=0, exit_grace_seconds=0, exit_confirmation=1)
        key = _key(semantic_context="t-12")
        state, transitions = _process(
            engine, key, (("present", 0, 0), ("present", 1, 1), ("absent", 2, 2))
        )
        exit_fact = next(t for t in transitions if t.reason is TemporalReason.EXIT_CONFIRMED)
        assert exit_fact.event_time == _event(2)
        assert exit_fact.configuration_version_id == key.configuration_version_id
        assert exit_fact.fsm_version == TEMPORAL_ENGINE_VERSION
        assert exit_fact.key == key
        assert exit_fact.key.semantic_context == "t-12"
        assert state.current_state == "absent"


# =============================================================================
# §19. Invalid inputs — never silently create state from bad data
# =============================================================================


class TestInvalidInputs:
    """Missing or malformed inputs fail with typed errors — never repaired."""

    def test_missing_tenant_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="presence",
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_venue_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="presence",
                tenant_id=_TENANT,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
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

    def test_missing_camera_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="presence",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

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

    def test_missing_spatial_context_is_valid_and_scoped_by_track(self) -> None:
        # semantic_context is optional by contract: context-agnostic
        # presence is legitimate, and the track component still scopes it.
        engine = _engine()
        key = _key(semantic_context=None)
        state, _ = _process(engine, key, (("present", 0, 0), ("present", 1, 1)))
        assert state.current_state == "present"
        assert key.semantic_context is None

    def test_empty_spatial_context_rejected(self) -> None:
        with pytest.raises(ValueError):
            _key(semantic_context="")

    def test_invalid_event_timestamp_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError):
            _status_obs(
                _key(),
                status=SpatialStatus.INSIDE,
                event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
                frame_id=_frame(0),
            )

    def test_invalid_observation_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(InvalidTemporalInputError, match="observation"):
            engine.apply(
                engine.initial_state(_key()),
                TemporalInput(
                    key=_key(),
                    observation="not-an-observation",  # type: ignore[arg-type]
                    observation_kind="present",
                    processing_time=_processing(),
                ),
            )

    def test_incompatible_provenance_rejected(self) -> None:
        # A state key naming track A applied to an observation for track B.
        engine = _engine()
        key = _key()
        obs = _obs(key, kind="present", event_time=_event(0), frame_id=_frame(0))
        wrong = _key(track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")))
        with pytest.raises(StateKeyMismatchError, match="track_id"):
            engine.apply(engine.initial_state(key), _input(wrong, obs))

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


# =============================================================================
# §21. Golden timeline — exact states, transitions, timestamps; replay
# =============================================================================


class TestGoldenTimeline:
    """§21: OUTSIDE/INSIDE timeline with exact expected state semantics."""

    # 10:00:00 OUTSIDE, 10:00:01-03 INSIDE, 10:00:04-06 OUTSIDE.
    TIMELINE: tuple[tuple[SpatialStatus, int, int], ...] = (
        (SpatialStatus.OUTSIDE, 0, 0),
        (SpatialStatus.INSIDE, 1, 1),
        (SpatialStatus.INSIDE, 2, 2),
        (SpatialStatus.INSIDE, 3, 3),
        (SpatialStatus.OUTSIDE, 4, 4),
        (SpatialStatus.OUTSIDE, 5, 5),
        (SpatialStatus.OUTSIDE, 6, 6),
    )

    def _policy(self) -> TemporalPolicy:
        return TemporalPolicy(
            entry_confirmation=2,
            exit_confirmation=3,
            minimum_dwell_seconds=0,
            exit_grace_seconds=0,
        )

    def _run(self) -> tuple[list[tuple[str, TemporalTransition]], TemporalState]:
        engine = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=self._policy())
        key = _key()
        state = engine.initial_state(key)
        steps: list[tuple[str, TemporalTransition]] = []
        for status, seconds, frame_index in self.TIMELINE:
            obs = _status_obs(
                key, status=status, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            result = engine.apply(state, _input(key, obs))  # kind via presence_kind
            state = result.state
            steps.append((state.current_state, result.transitions[0]))
        return steps, state

    def test_golden_timeline_exact_states_and_timestamps(self) -> None:
        steps, state = self._run()
        assert [s for s, _ in steps] == [
            "absent",
            "entering",
            "present",
            "present",
            "exiting",
            "exiting",
            "absent",
        ]
        assert [t.reason for _, t in steps] == [
            TemporalReason.OBSERVED_STAY,  # 00 outside while absent
            TemporalReason.OBSERVED_STAY,  # 01 first inside -> ENTERING
            TemporalReason.ENTER_CONFIRMED,  # 02 confirmation -> PRESENT
            TemporalReason.OBSERVED_STAY,  # 03 stable PRESENT
            TemporalReason.OBSERVED_STAY,  # 04 first qualified absence -> EXITING
            TemporalReason.OBSERVED_STAY,  # 05 confirming
            TemporalReason.EXIT_CONFIRMED,  # 06 confirmation -> ABSENT
        ]
        # Exact timestamps: ENTER at 10:00:02, EXIT at 10:00:06.
        enter = next(t for _, t in steps if t.reason is TemporalReason.ENTER_CONFIRMED)
        exit_fact = next(t for _, t in steps if t.reason is TemporalReason.EXIT_CONFIRMED)
        assert enter.event_time == _event(2)
        assert exit_fact.event_time == _event(6)
        assert enter.from_state == "entering" and enter.to_state == "present"
        assert exit_fact.from_state == "exiting" and exit_fact.to_state == "absent"
        # Ledger fields exactly as specified.
        assert state.current_state == "absent"
        assert state.state_since == _event(6)
        assert state.last_seen == _event(6)
        assert state.watermark_event_time == _event(6)
        assert state.last_applied_frame_id == _frame(6)

    def test_golden_timeline_replay_is_identical(self) -> None:
        first_steps, first_state = self._run()
        second_steps, second_state = self._run()
        assert second_state == first_state
        assert second_steps == first_steps
        assert [t.transition_id for _, t in first_steps] == [
            t.transition_id for _, t in second_steps
        ]


# =============================================================================
# §23. Long session — bounded, watermark-progressing, checkpointable
# =============================================================================


class TestLongSession:
    """A long-running session stays PRESENT without memory growth."""

    def test_long_session_stays_present_and_bounded(self) -> None:
        engine = _engine()  # entry_confirmation=2, dwell=10s, grace=30s
        key = _key()
        state = engine.initial_state(key)
        # Enter once, then oscillate within grace (every 1s) for 5000 steps.
        for i in range(2):
            obs = _obs(key, kind="present", event_time=_event(i), frame_id=_frame(i))
            state = engine.apply(state, _input(key, obs, kind="present")).state
        assert state.current_state == "present"
        entry_time = state.state_since
        for i in range(2, 5002):
            kind = "present" if i % 2 == 0 else "absent"
            obs = _obs(key, kind=kind, event_time=_event(i), frame_id=_frame(i))
            result = engine.apply(state, _input(key, obs, kind=kind))
            state = result.state
        assert state.current_state == "present"
        assert state.state_since == entry_time  # never reset while PRESENT
        assert state.last_seen == _event(5001)
        assert state.watermark_event_time == _event(5001)
        # Bounded: the recent-transition ring never grows past the limit.
        assert len(state.recent_transitions) <= engine.policy.transition_history_limit
        # The checkpoint remains serializable at full size.
        checkpoint = engine.checkpoint(state)
        assert TemporalCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint
        assert checkpoint.state == state


# =============================================================================
# §24. Pure FSM — no I/O, no current-time reads
# =============================================================================


class TestEnterExitPurity:
    """The enter/exit core performs no I/O and reads no current time."""

    def test_no_forbidden_imports_or_current_time_in_enter_exit_core(self) -> None:
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
        for name in ("presence.py", "fsm.py", "engine.py"):
            text = (package_dir / name).read_text()
            for module in forbidden:
                assert not re.search(rf"^\s*(from|import)\s+{module}\b", text, re.MULTILINE), (
                    f"I/O/stateful module {module!r} leaked into {name}"
                )
            assert "now(" not in text, f"current-time read leaked into {name}"
            assert "utc_now" not in text, f"current-time helper leaked into {name}"
