"""Tests for the Task 15.3 dwell FSM (deterministic dwell-time intelligence).

Dwell = the event-time span during which an entity is continuously
PRESENT (per the Task 15.2 Enter/Exit FSM) within the same spatial
context. The dwell FSM is a separate family (fsm_kind="dwell") on the
same Task 15.1 foundation, driven by the presence engine's transitions
via ``dwell_event_from_presence`` — so it inherits dedup, event-time
ordering, watermark, isolation, and checkpointing without duplicating
any of it.

Covered:

- the idle/dwelling states and the legal transitions (enter_confirmed,
  exit_confirmed, missing_expired, session_closed, stay), including
  explicit rejection of mis-wired events;
- dwell starts at the confirmed-PRESENT event_time and ends at the
  confirmed-ABSENT event_time (never processing time, never the first
  observation, never wall clock);
- running (open) intervals: dwell_end is None, last_seen advances, the
  interval id is stable while open;
- minimum-dwell qualification is configuration-driven and NEVER alters
  the recorded interval (actual presence is preserved);
- the four golden timelines (§17 normal, §18 occlusion, §19 re-entry,
  §20 duplicate replay);
- late/out-of-order follows the 15.1 policy; duplicates are idempotent;
- checkpoint/restart while an interval is open equals uninterrupted
  processing;
- isolation across tenant/venue/session/track/configuration/spatial
  context; configuration-version pinning for historical sessions;
- invalid inputs and corrupted-domain-state rejection;
- long-session boundedness; the pure-core boundary (no I/O, no
  current-time reads).

All fixtures use the REAL canonical contracts (SpatialObservation,
TemporalStateKey, TemporalPolicy, TemporalCheckpoint, DwellInterval)
with fixed deterministic IDs so replay comparisons are byte-exact.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.intelligence.temporal import (
    DWELL_FSM,
    PRESENCE_FSM,
    DwellEngine,
    PresenceTemporalEngine,
    TemporalInput,
    dwell_event_from_presence,
    presence_kind,
)
from backend.app.intelligence.temporal.exceptions import (
    InvalidTemporalInputError,
    InvalidTransitionError,
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
from contracts.spatial import (
    SpatialObservation,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    DwellInterval,
    TemporalCheckpoint,
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
    key: TemporalStateKey, obs: SpatialObservation, *, kind: str | None = None
) -> TemporalInput:
    return TemporalInput(
        key=key,
        observation=obs,
        observation_kind=kind or presence_kind(obs),
        processing_time=_processing(),
    )


def _dwell_input(key: TemporalStateKey, obs: SpatialObservation, kind: str) -> TemporalInput:
    """Dwell-engine input: the presence-derived dwell event kind."""
    return TemporalInput(
        key=key, observation=obs, observation_kind=kind, processing_time=_processing()
    )


def _presence_engine(policy: TemporalPolicy | None = None, **kwargs) -> PresenceTemporalEngine:
    return PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy or TemporalPolicy(**kwargs))


def _dwell_engine(policy: TemporalPolicy | None = None, **kwargs) -> DwellEngine:
    return DwellEngine(fsm=DWELL_FSM, policy=policy or TemporalPolicy(**kwargs))


def _continue(
    presence: PresenceTemporalEngine,
    dwell: DwellEngine,
    pstate: TemporalState,
    dstate: TemporalState,
    *,
    pkey: TemporalStateKey,
    dkey: TemporalStateKey,
    timeline: tuple[tuple[str, int, int], ...],
) -> tuple[TemporalState, TemporalState, list[DwellInterval]]:
    """Run the canonical chain (SpatialObservation -> presence -> dwell)
    over already-initialized states."""
    intervals: list[DwellInterval] = []
    for kind, seconds, frame_index in timeline:
        obs = _obs(pkey, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
        presence_result = presence.apply(pstate, _input(pkey, obs, kind=kind))
        pstate = presence_result.state
        dwell_kind = dwell_event_from_presence(presence_result.transitions[0])
        dwell_result = dwell.apply(dstate, _dwell_input(dkey, obs, dwell_kind))
        dstate = dwell_result.state
        intervals.extend(dwell_result.dwell_intervals)
    return pstate, dstate, intervals


def _chain(
    presence: PresenceTemporalEngine,
    dwell: DwellEngine,
    *,
    pkey: TemporalStateKey,
    dkey: TemporalStateKey,
    timeline: tuple[tuple[str, int, int], ...],
) -> tuple[TemporalState, TemporalState, list[DwellInterval]]:
    pstate = presence.initial_state(pkey)
    dstate = dwell.initial_state(dkey)
    return _continue(presence, dwell, pstate, dstate, pkey=pkey, dkey=dkey, timeline=timeline)


# Golden timelines (§17/§18/§19)
GOLDEN_NORMAL = (
    ("absent", 0, 0),
    ("present", 1, 1),
    ("present", 3, 3),
    ("present", 5, 5),
    ("present", 8, 8),
    ("absent", 10, 10),
    ("absent", 12, 12),
)
GOLDEN_OCCLUSION = (
    ("present", 0, 0),
    ("present", 1, 1),
    ("not_observed", 2, 2),
    ("not_observed", 3, 3),
    ("present", 4, 4),
    ("present", 5, 5),
    ("absent", 6, 6),
)
GOLDEN_REENTRY = (
    ("present", 0, 0),
    ("absent", 5, 5),
    ("present", 10, 10),
    ("absent", 20, 20),
)


def _normal_engines() -> tuple[PresenceTemporalEngine, DwellEngine]:
    presence = _presence_engine(
        entry_confirmation=2, exit_confirmation=2, minimum_dwell_seconds=0, exit_grace_seconds=0
    )
    return presence, _dwell_engine()


# =============================================================================
# Isolated dwell FSM transitions (explicit dwell events)
# =============================================================================


class TestDwellTransitions:
    """The dwell FSM legal transitions and explicit rejections."""

    def test_idle_to_dwelling_on_enter_confirmed(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        result = engine.apply(
            engine.initial_state(dkey),
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        )
        assert result.state.current_state == "dwelling"
        assert result.state.state_since == _event(0)  # dwell start = confirmed PRESENT
        assert result.transitions[0].reason is TemporalReason.ENTER_CONFIRMED
        assert result.transitions[0].from_state == "idle"
        assert result.transitions[0].to_state == "dwelling"
        assert result.dwell_intervals == ()

    def test_stay_while_dwelling_keeps_interval_open(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey, _obs(dkey, kind="present", event_time=_event(5), frame_id=_frame(5)), "stay"
            ),
        )
        state = result.state
        assert state.current_state == "dwelling"
        assert state.state_since == _event(0)  # dwell_start is NOT reset
        assert state.last_seen == _event(5)
        assert result.dwell_intervals == ()  # stays never emit a fact

    def test_exit_confirmed_closes_interval_with_fact(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="absent", event_time=_event(10), frame_id=_frame(10)),
                "exit_confirmed",
            ),
        )
        assert result.state.current_state == "idle"
        assert result.state.state_since == _event(10)
        (interval,) = result.dwell_intervals
        assert interval.dwell_start == _event(0)
        assert interval.dwell_end == _event(10)
        assert interval.duration_seconds == pytest.approx(10.0)
        assert interval.last_seen == _event(10)
        assert interval.reason is TemporalReason.EXIT_CONFIRMED
        assert interval.key == dkey
        assert result.transitions[0].reason is TemporalReason.EXIT_CONFIRMED

    def test_missing_expired_closes_interval(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="not_observed", event_time=_event(8), frame_id=_frame(8)),
                "missing_expired",
            ),
        )
        (interval,) = result.dwell_intervals
        assert interval.dwell_end == _event(8)
        assert interval.duration_seconds == pytest.approx(8.0)
        assert interval.reason is TemporalReason.MISSING_EXPIRED

    def test_session_closed_closes_open_interval(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="session_closed", event_time=_event(7), frame_id=_frame(7)),
                "session_closed",
            ),
        )
        (interval,) = result.dwell_intervals
        assert interval.dwell_start == _event(0)
        assert interval.dwell_end == _event(7)
        assert interval.reason is TemporalReason.SESSION_CLOSED
        assert result.state.current_state == "idle"

    def test_session_closed_while_idle_emits_no_interval(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        result = engine.apply(
            engine.initial_state(dkey),
            _dwell_input(
                dkey,
                _obs(dkey, kind="session_closed", event_time=_event(0), frame_id=_frame(0)),
                "session_closed",
            ),
        )
        assert result.state.current_state == "idle"
        assert result.dwell_intervals == ()

    def test_enter_confirmed_while_dwelling_rejected(self) -> None:
        # Mis-wired orchestration (a second entry while dwelling) is
        # rejected explicitly — never silently resetting the interval.
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            engine.apply(
                state,
                _dwell_input(
                    dkey,
                    _obs(dkey, kind="present", event_time=_event(2), frame_id=_frame(2)),
                    "enter_confirmed",
                ),
            )

    def test_exit_confirmed_while_idle_rejected(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            engine.apply(
                engine.initial_state(dkey),
                _dwell_input(
                    dkey,
                    _obs(dkey, kind="absent", event_time=_event(0), frame_id=_frame(0)),
                    "exit_confirmed",
                ),
            )

    def test_unknown_kind_rejected(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(
                engine.initial_state(dkey),
                _dwell_input(
                    dkey,
                    _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                    "warp",
                ),
            )


# =============================================================================
# §11. Running (open) dwell interval
# =============================================================================


class TestRunningDwell:
    """An open interval has no fabricated dwell_end and a stable id."""

    def _dwelling(self, engine: DwellEngine, dkey: TemporalStateKey) -> TemporalState:
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        state = engine.apply(
            state,
            _dwell_input(
                dkey, _obs(dkey, kind="present", event_time=_event(5), frame_id=_frame(5)), "stay"
            ),
        ).state
        return engine.apply(
            state,
            _dwell_input(
                dkey, _obs(dkey, kind="present", event_time=_event(10), frame_id=_frame(10)), "stay"
            ),
        ).state

    def test_open_interval_representation(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = self._dwelling(engine, dkey)
        interval = engine.open_interval(state)
        assert interval is not None
        assert interval.dwell_start == _event(0)
        assert interval.dwell_end is None  # never fabricated
        assert interval.last_seen == _event(10)
        assert interval.duration_seconds == pytest.approx(10.0)
        assert interval.reason is None

    def test_open_interval_id_stable_while_open(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        early = engine.open_interval(state)
        state = engine.apply(
            state,
            _dwell_input(
                dkey, _obs(dkey, kind="present", event_time=_event(50), frame_id=_frame(50)), "stay"
            ),
        ).state
        late = engine.open_interval(state)
        assert early is not None and late is not None
        assert early.interval_id == late.interval_id  # id does not churn as last_seen advances
        assert late.last_seen == _event(50)

    def test_open_interval_none_when_idle(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        assert engine.open_interval(engine.initial_state(dkey)) is None


# =============================================================================
# §9. Minimum-dwell qualification (config-driven, never corrupts presence)
# =============================================================================


class TestMinimumDwell:
    """The threshold flags facts; it never alters the recorded interval."""

    def _closed(self, engine: DwellEngine, dkey: TemporalStateKey) -> DwellInterval:
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="absent", event_time=_event(2), frame_id=_frame(2)),
                "exit_confirmed",
            ),
        )
        return result.dwell_intervals[0]

    def test_short_interval_preserved_but_not_qualified(self) -> None:
        # Actual presence 2s, minimum 5s: the interval is REAL (2s, kept
        # verbatim) — it is simply not qualified.
        engine = _dwell_engine(dwell_minimum_seconds=5)
        interval = self._closed(engine, _key(fsm_kind="dwell"))
        assert interval.dwell_start == _event(0)
        assert interval.dwell_end == _event(2)
        assert interval.duration_seconds == pytest.approx(2.0)
        assert interval.qualified is False
        assert interval.minimum_dwell_seconds == pytest.approx(5.0)

    def test_at_threshold_qualified(self) -> None:
        engine = _dwell_engine(dwell_minimum_seconds=5)
        dkey = _key(fsm_kind="dwell")
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="absent", event_time=_event(5), frame_id=_frame(5)),
                "exit_confirmed",
            ),
        )
        assert result.dwell_intervals[0].duration_seconds == pytest.approx(5.0)
        assert result.dwell_intervals[0].qualified is True

    def test_threshold_is_config_driven(self) -> None:
        # The helper interval is 2s long: a 10s threshold disqualifies it,
        # a 1s threshold qualifies it — same actual interval, different
        # configuration.
        strict = _dwell_engine(dwell_minimum_seconds=10)
        lenient = _dwell_engine(dwell_minimum_seconds=1)
        dkey = _key(fsm_kind="dwell")
        strict_interval = self._closed(strict, dkey)
        lenient_interval = self._closed(lenient, dkey)
        assert strict_interval.duration_seconds == pytest.approx(2.0)
        assert lenient_interval.duration_seconds == pytest.approx(2.0)  # actual presence unchanged
        assert strict_interval.qualified is False
        assert lenient_interval.qualified is True


# =============================================================================
# §17-20. Golden timelines (full canonical chain)
# =============================================================================


class TestGoldenNormalDwell:
    """§17: outside/entering/present/exiting/absent -> one 9s interval."""

    def test_golden_normal_dwell(self) -> None:
        presence, dwell = _normal_engines()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        pstate, dstate, intervals = _chain(
            presence, dwell, pkey=pkey, dkey=dkey, timeline=GOLDEN_NORMAL
        )
        assert len(intervals) == 1
        (interval,) = intervals
        # dwell_start = confirmed PRESENT (10:00:03), NOT the first inside
        # observation (10:00:01); dwell_end = confirmed ABSENT (10:00:12).
        assert interval.dwell_start == _event(3)
        assert interval.dwell_end == _event(12)
        assert interval.duration_seconds == pytest.approx(9.0)
        assert interval.qualified is True
        assert interval.reason is TemporalReason.EXIT_CONFIRMED
        assert interval.key.configuration_version_id == pkey.configuration_version_id
        assert dstate.current_state == "idle"
        assert pstate.current_state == "absent"


class TestGoldenOcclusion:
    """§18: a covered missing gap keeps ONE continuous dwell interval."""

    def test_golden_occlusion_single_continuous_interval(self) -> None:
        presence = _presence_engine(
            entry_confirmation=2,
            exit_confirmation=1,
            minimum_dwell_seconds=0,
            exit_grace_seconds=0,
            occlusion_tolerance_seconds=60,
        )
        dwell = _dwell_engine()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        pstate, dstate, intervals = _chain(
            presence, dwell, pkey=pkey, dkey=dkey, timeline=GOLDEN_OCCLUSION
        )
        assert len(intervals) == 1  # the gap never split the interval
        (interval,) = intervals
        assert interval.dwell_start == _event(1)  # confirmed PRESENT
        assert interval.dwell_end == _event(6)
        assert interval.duration_seconds == pytest.approx(5.0)
        assert dstate.current_state == "idle"
        assert pstate.current_state == "absent"


class TestGoldenReEntry:
    """§19: independent presence sessions produce independent intervals."""

    def test_golden_reentry_two_independent_intervals(self) -> None:
        presence = _presence_engine(
            entry_confirmation=1, exit_confirmation=1, minimum_dwell_seconds=0, exit_grace_seconds=0
        )
        dwell = _dwell_engine()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        pstate, dstate, intervals = _chain(
            presence, dwell, pkey=pkey, dkey=dkey, timeline=GOLDEN_REENTRY
        )
        assert len(intervals) == 2  # never merged
        first, second = intervals
        assert first.dwell_start == _event(0)
        assert first.dwell_end == _event(5)
        assert first.duration_seconds == pytest.approx(5.0)
        assert second.dwell_start == _event(10)
        assert second.dwell_end == _event(20)
        assert second.duration_seconds == pytest.approx(10.0)
        assert first.interval_id != second.interval_id
        assert dstate.current_state == "idle"
        assert pstate.current_state == "absent"


class TestGoldenDuplicate:
    """§20: replaying identical observations reproduces identical output."""

    def test_replay_is_identical(self) -> None:
        presence, dwell = _normal_engines()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        p1, d1, intervals_1 = _chain(presence, dwell, pkey=pkey, dkey=dkey, timeline=GOLDEN_NORMAL)
        p2, d2, intervals_2 = _chain(presence, dwell, pkey=pkey, dkey=dkey, timeline=GOLDEN_NORMAL)
        assert d2 == d1
        assert p2 == p1
        assert intervals_2 == intervals_1
        assert [i.interval_id for i in intervals_1] == [i.interval_id for i in intervals_2]

    def test_reapplied_final_observation_is_deduplicated(self) -> None:
        presence, dwell = _normal_engines()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        pstate, dstate, intervals = _chain(
            presence, dwell, pkey=pkey, dkey=dkey, timeline=GOLDEN_NORMAL
        )
        assert len(intervals) == 1
        kind, seconds, frame_index = GOLDEN_NORMAL[-1]
        obs = _obs(pkey, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
        presence_result = presence.apply(pstate, _input(pkey, obs, kind=kind))
        assert presence_result.deduplicated
        dwell_result = dwell.apply(
            dstate,
            _dwell_input(dkey, obs, dwell_event_from_presence(presence_result.transitions[0])),
        )
        assert dwell_result.deduplicated
        assert dwell_result.dwell_intervals == ()  # no duplicated interval
        assert dwell_result.state == dstate  # no duplicated state advancement


# =============================================================================
# §12/§13. Event-time ordering and idempotency
# =============================================================================


class TestOrderingAndIdempotency:
    """Dwell follows the 15.1 event-time policy; duplicates advance once."""

    def test_out_of_order_observation_follows_event_time_policy(self) -> None:
        # 10:00 present, 10:02 present, 10:01 present (late), 10:03 exit.
        # dwell_start must be 10:00:02 (the CONFIRMED-PRESENT event time),
        # not insertion order and not the late observation.
        presence = _presence_engine(
            entry_confirmation=2, exit_confirmation=1, minimum_dwell_seconds=0, exit_grace_seconds=0
        )
        dwell = _dwell_engine()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        _, dstate, intervals = _chain(
            presence,
            dwell,
            pkey=pkey,
            dkey=dkey,
            timeline=(
                ("present", 0, 0),
                ("present", 2, 2),
                ("present", 1, 1),
                ("absent", 3, 3),
            ),
        )
        assert dstate.current_state == "idle"
        (interval,) = intervals
        assert interval.dwell_start == _event(2)
        assert interval.dwell_end == _event(3)
        assert interval.duration_seconds == pytest.approx(1.0)

    def test_late_beyond_window_rejected(self) -> None:
        presence, dwell = _normal_engines()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        with pytest.raises(LateEventError, match="reordering window"):
            _chain(
                presence,
                dwell,
                pkey=pkey,
                dkey=dkey,
                timeline=(("present", 0, 0), ("present", 120, 120), ("present", 30, 30)),
            )


# =============================================================================
# §14. Checkpoint / restart while an interval is open
# =============================================================================


class TestCheckpointAndRestart:
    """Restarting with an open interval equals uninterrupted processing."""

    _POLICY = TemporalPolicy(
        entry_confirmation=1, exit_confirmation=1, minimum_dwell_seconds=0, exit_grace_seconds=0
    )
    _TIMELINE = (("present", 0, 0), ("present", 5, 5), ("present", 10, 10), ("absent", 15, 15))

    def test_checkpoint_while_dwelling_restart_equals_uninterrupted(self) -> None:
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        # Uninterrupted: enter, present, present, exit.
        uninterrupted_presence = _presence_engine(policy=self._POLICY)
        uninterrupted_dwell = _dwell_engine()
        p_full, d_full, intervals_full = _chain(
            uninterrupted_presence,
            uninterrupted_dwell,
            pkey=pkey,
            dkey=dkey,
            timeline=self._TIMELINE,
        )
        assert len(intervals_full) == 1

        # Restarted: enter + present -> CHECKPOINT -> new engines -> present + exit.
        presence_a = _presence_engine(policy=self._POLICY)
        dwell_a = _dwell_engine()
        p_mid, d_mid, _ = _chain(
            presence_a,
            dwell_a,
            pkey=pkey,
            dkey=dkey,
            timeline=(("present", 0, 0), ("present", 5, 5)),
        )
        assert d_mid.current_state == "dwelling"
        presence_checkpoint = presence_a.checkpoint(p_mid)
        dwell_checkpoint = dwell_a.checkpoint(d_mid)
        data = dwell_checkpoint.to_dict()
        assert TemporalCheckpoint.from_dict(data) == dwell_checkpoint

        presence_b = _presence_engine(policy=self._POLICY)
        dwell_b = _dwell_engine()
        p_restored = presence_b.restore(presence_checkpoint)
        d_restored = dwell_b.restore(dwell_checkpoint)
        p_resumed, d_resumed, intervals_resumed = _continue(
            presence_b,
            dwell_b,
            p_restored,
            d_restored,
            pkey=pkey,
            dkey=dkey,
            timeline=(("present", 10, 10), ("absent", 15, 15)),
        )
        (full,) = intervals_full
        (resumed,) = intervals_resumed
        assert resumed.dwell_start == full.dwell_start  # same dwell_start
        assert resumed.dwell_end == full.dwell_end  # same dwell_end
        assert resumed.duration_seconds == full.duration_seconds  # same duration
        assert resumed.duration_seconds == pytest.approx(15.0)
        assert d_resumed == d_full  # same final temporal state
        assert p_resumed == p_full


# =============================================================================
# §15/§16. Configuration provenance and isolation
# =============================================================================


class TestIsolation:
    """Dwell state never mixes across any canonical scope."""

    def _interval_for(self, engine: DwellEngine, dkey: TemporalStateKey) -> DwellInterval:
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        result = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="absent", event_time=_event(10), frame_id=_frame(10)),
                "exit_confirmed",
            ),
        )
        return result.dwell_intervals[0]

    def test_tenant_isolation(self) -> None:
        engine = _dwell_engine()
        other = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        a = self._interval_for(engine, _key(fsm_kind="dwell"))
        b = self._interval_for(engine, _key(fsm_kind="dwell", tenant_id=other))
        assert a.key.tenant_id != b.key.tenant_id
        assert a.interval_id != b.interval_id

    def test_venue_isolation(self) -> None:
        engine = _dwell_engine()
        other = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        a = self._interval_for(engine, _key(fsm_kind="dwell"))
        b = self._interval_for(engine, _key(fsm_kind="dwell", venue_id=other))
        assert a.interval_id != b.interval_id

    def test_session_isolation(self) -> None:
        engine = _dwell_engine()
        other = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        a = self._interval_for(engine, _key(fsm_kind="dwell"))
        b = self._interval_for(engine, _key(fsm_kind="dwell", session_id=other))
        assert a.interval_id != b.interval_id

    def test_track_isolation(self) -> None:
        engine = _dwell_engine()
        other = TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        a = self._interval_for(engine, _key(fsm_kind="dwell"))
        b = self._interval_for(engine, _key(fsm_kind="dwell", track_id=other))
        assert a.interval_id != b.interval_id

    def test_configuration_version_isolation(self) -> None:
        engine = _dwell_engine()
        other = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000099"))
        a = self._interval_for(engine, _key(fsm_kind="dwell"))
        b = self._interval_for(engine, _key(fsm_kind="dwell", configuration_version_id=other))
        assert a.interval_id != b.interval_id

    def test_spatial_context_isolation(self) -> None:
        engine = _dwell_engine()
        zone = self._interval_for(engine, _key(fsm_kind="dwell", semantic_context="z-lobby"))
        table = self._interval_for(engine, _key(fsm_kind="dwell", semantic_context="t-12"))
        assert zone.key.semantic_context == "z-lobby"
        assert table.key.semantic_context == "t-12"
        assert zone.interval_id != table.interval_id

    def test_configuration_pinned_for_historical_sessions(self) -> None:
        # §15: a V1 session stays on V1 even after V2 is published — the
        # interval carries the pinned configuration version.
        presence, dwell = _normal_engines()
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        pkey_v1, dkey_v1 = (
            _key(configuration_version_id=v1),
            _key(fsm_kind="dwell", configuration_version_id=v1),
        )
        pkey_v2, dkey_v2 = (
            _key(configuration_version_id=v2),
            _key(fsm_kind="dwell", configuration_version_id=v2),
        )
        _, _, intervals_v1 = _chain(
            presence, dwell, pkey=pkey_v1, dkey=dkey_v1, timeline=GOLDEN_NORMAL
        )
        _, _, intervals_v2 = _chain(
            presence, dwell, pkey=pkey_v2, dkey=dkey_v2, timeline=GOLDEN_NORMAL
        )
        assert intervals_v1[0].key.configuration_version_id == v1
        assert intervals_v2[0].key.configuration_version_id == v2
        assert intervals_v1[0].interval_id != intervals_v2[0].interval_id


# =============================================================================
# §19/§22. Invalid inputs and corrupted-domain rejection
# =============================================================================


class TestInvalidInputs:
    """Missing or malformed inputs fail explicitly — never repaired."""

    def test_missing_tenant_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="dwell",
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_venue_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="dwell",
                tenant_id=_TENANT,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_session_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="dwell",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_camera_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="dwell",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_track_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="dwell",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
            )

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="dwell",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                track_id=_TRACK,
            )

    def test_missing_spatial_context_is_valid_scope(self) -> None:
        # semantic_context is optional by contract (context-agnostic dwell);
        # the track component still scopes the state.
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell", semantic_context=None)
        state = engine.initial_state(dkey)
        state = engine.apply(
            state,
            _dwell_input(
                dkey,
                _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0)),
                "enter_confirmed",
            ),
        ).state
        assert state.current_state == "dwelling"
        assert dkey.semantic_context is None

    def test_invalid_event_timestamp_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError):
            _status_obs(
                _key(),
                status=SpatialStatus.INSIDE,
                event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
                frame_id=_frame(0),
            )

    def test_incompatible_provenance_rejected(self) -> None:
        engine = _dwell_engine()
        dkey = _key(fsm_kind="dwell")
        obs = _obs(dkey, kind="present", event_time=_event(0), frame_id=_frame(0))
        wrong = _key(
            fsm_kind="dwell", track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        )
        with pytest.raises(StateKeyMismatchError, match="track_id"):
            engine.apply(
                engine.initial_state(dkey),
                TemporalInput(
                    key=wrong,
                    observation=obs,
                    observation_kind="enter_confirmed",
                    processing_time=_processing(),
                ),
            )

    def test_negative_duration_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError, match="duration_seconds"):
            DwellInterval(
                interval_id=EventId(uuid.uuid4()),
                fsm_kind="dwell",
                key=_key(fsm_kind="dwell"),
                dwell_start=_event(0),
                dwell_end=_event(10),
                last_seen=_event(10),
                duration_seconds=-1.0,
                minimum_dwell_seconds=0.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )

    def test_dwell_end_before_start_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError, match="dwell_end"):
            DwellInterval(
                interval_id=EventId(uuid.uuid4()),
                fsm_kind="dwell",
                key=_key(fsm_kind="dwell"),
                dwell_start=_event(10),
                dwell_end=_event(5),
                last_seen=_event(5),
                duration_seconds=5.0,
                minimum_dwell_seconds=0.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )

    def test_last_seen_before_start_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError, match="last_seen"):
            DwellInterval(
                interval_id=EventId(uuid.uuid4()),
                fsm_kind="dwell",
                key=_key(fsm_kind="dwell"),
                dwell_start=_event(10),
                last_seen=_event(5),
                duration_seconds=None,
                minimum_dwell_seconds=0.0,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )


# =============================================================================
# §21. Long-duration session — bounded, watermark-progressing
# =============================================================================


class TestLongSession:
    """A long PRESENT session stays bounded and checkpointable."""

    def test_long_session_stays_open_and_bounded(self) -> None:
        presence, dwell = _normal_engines()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        pstate = presence.initial_state(pkey)
        dstate = dwell.initial_state(dkey)
        # Enter once, then alternate present/absent every 1s for 5000 steps.
        # _normal_engines() uses grace=0 / exit_confirmation=2: each absent
        # qualifies into EXITING but the next present recovers, so the exit
        # is never CONFIRMED and the dwell interval stays open the whole
        # session (recovery-driven oscillation — the anti-jitter guarantee
        # that a short gap never closes dwell).
        timeline = (
            ("present", 0, 0),
            ("present", 1, 1),
        )
        pstate, dstate, _ = _continue(
            presence, dwell, pstate, dstate, pkey=pkey, dkey=dkey, timeline=timeline
        )
        assert dstate.current_state == "dwelling"
        dwell_start = dstate.state_since
        early = dwell.open_interval(dstate)
        assert early is not None
        steps = tuple(("present" if i % 2 == 0 else "absent", i, i) for i in range(2, 5002))
        pstate, dstate, intervals = _continue(
            presence, dwell, pstate, dstate, pkey=pkey, dkey=dkey, timeline=steps
        )
        assert dstate.current_state == "dwelling"
        assert dstate.state_since == dwell_start  # dwell_start never reset
        assert dstate.last_seen == _event(5001)  # last_seen progresses
        assert intervals == []  # interval still OPEN — no fabricated end
        assert len(dstate.recent_transitions) <= dwell.policy.transition_history_limit
        # Checkpoint remains serializable at full size.
        checkpoint = dwell.checkpoint(dstate)
        assert TemporalCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint
        # The open-interval identity is stable across the session.
        late = dwell.open_interval(dstate)
        assert late is not None
        assert early.interval_id == late.interval_id
        assert late.last_seen == _event(5001)
        assert late.duration_seconds == pytest.approx(5000.0)


# =============================================================================
# Canonical chain + mapping completeness
# =============================================================================


class TestChainIntegration:
    """The full canonical chain: SpatialObservation -> presence -> dwell."""

    def test_chain_via_spatial_statuses_and_presence_kind(self) -> None:
        # Real SpatialObservation statuses (INSIDE/OUTSIDE) mapped through
        # presence_kind — no explicit kinds in the pipeline.
        presence, dwell = _normal_engines()
        pkey, dkey = _key(), _key(fsm_kind="dwell")
        pstate = presence.initial_state(pkey)
        dstate = dwell.initial_state(dkey)
        intervals: list[DwellInterval] = []
        for status, seconds, frame_index in (
            (SpatialStatus.OUTSIDE, 0, 0),
            (SpatialStatus.INSIDE, 1, 1),
            (SpatialStatus.INSIDE, 3, 3),
            (SpatialStatus.INSIDE, 5, 5),
            (SpatialStatus.OUTSIDE, 10, 10),
            (SpatialStatus.OUTSIDE, 12, 12),
        ):
            obs = _status_obs(
                pkey, status=status, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            presence_result = presence.apply(pstate, _input(pkey, obs))  # kind via presence_kind
            pstate = presence_result.state
            dwell_result = dwell.apply(
                dstate,
                _dwell_input(dkey, obs, dwell_event_from_presence(presence_result.transitions[0])),
            )
            dstate = dwell_result.state
            intervals.extend(dwell_result.dwell_intervals)
        (interval,) = intervals
        assert interval.dwell_start == _event(3)
        assert interval.dwell_end == _event(12)
        assert interval.duration_seconds == pytest.approx(9.0)


class TestDwellEventMapping:
    """dwell_event_from_presence covers every presence reason."""

    def _transition(self, reason: TemporalReason) -> TemporalTransition:
        key = _key()
        return TemporalTransition(
            transition_id=EventId(uuid.uuid5(TEMPORAL_ID_NAMESPACE, f"t-{reason.value}")),
            fsm_kind="presence",
            key=key,
            from_state="absent",
            to_state="present",
            event_kind="present",
            reason=reason,
            observation_frame_id=_frame(0),
            event_time=_event(0),
            processing_time=_processing(),
            configuration_version_id=key.configuration_version_id,
            fsm_version=TEMPORAL_ENGINE_VERSION,
        )

    def test_mapping_covers_all_reasons(self) -> None:
        assert dwell_event_from_presence(self._transition(TemporalReason.ENTER_CONFIRMED)) == (
            "enter_confirmed"
        )
        assert dwell_event_from_presence(self._transition(TemporalReason.EXIT_CONFIRMED)) == (
            "exit_confirmed"
        )
        assert dwell_event_from_presence(self._transition(TemporalReason.MISSING_EXPIRED)) == (
            "missing_expired"
        )
        assert dwell_event_from_presence(self._transition(TemporalReason.SESSION_CLOSED)) == (
            "session_closed"
        )
        assert dwell_event_from_presence(self._transition(TemporalReason.OBSERVED_STAY)) == "stay"
        assert dwell_event_from_presence(self._transition(TemporalReason.DEDUPLICATED)) == "stay"
        assert dwell_event_from_presence(self._transition(TemporalReason.REORDERED)) == "stay"


# =============================================================================
# §23. Pure core
# =============================================================================


class TestDwellPurity:
    """The dwell core performs no I/O and reads no current time."""

    def test_dwell_core_is_pure(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
        text = (package_dir / "dwell.py").read_text()
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
                f"I/O/stateful module {module!r} leaked into dwell.py"
            )
        assert "now(" not in text
        assert "utc_now" not in text
        assert "print(" not in text
