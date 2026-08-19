"""Tests for the Task 15.4 occupancy FSM (deterministic occupancy intelligence).

Occupancy answers \\"how many UNIQUE entities are confirmed PRESENT in a
defined spatial context at event-time?\\". It is an AGGREGATE across
tracks built on the Task 15.2 Enter/Exit FSM: the occupancy engine
consumes the presence engine's confirmed transitions
(``occupancy_event_from_presence``) and maintains one entity set per
occupancy scope (tenant + venue + session + camera + configuration
version + spatial context).

Covered:

- the per-entity idle/occupied legal transitions (enter counts once,
  confirmed exit removes once, session closure finalizes, stays never
  change the count) and explicit rejection of mis-wired events;
- occupancy is based on CONFIRMED presence only: ENTERING never counts,
  EXITING never decrements, short occlusion never removes;
- duplicate transitions are idempotent (per-track position dedup);
- multiple entities, re-entry, and the golden single-zone progression
  (1 -> 2 -> 1 -> 0);
- the 15.1 event-time policy: event_time is authoritative, within-window
  reorders are accepted-with-no-rewind (never change the set), beyond
  the window is a typed LateEventError;
- checkpoint/restart while entities are occupied equals uninterrupted
  processing;
- isolation across tenant/venue/session/camera/configuration/spatial
  context — a track from one session can never remove occupancy from
  another; configuration-version pinning for historical sessions;
- invariant enforcement (count >= 0, count == len(occupied set), every
  change has an explicit source transition) and invalid-domain rejection
  (exit for a never-counted entity, second entry while counted);
- a bounded deterministic sequence test checking invariants after every
  transition;
- performance at realistic scale (bounded state, O(1) set/dict
  bookkeeping) and the pure-core boundary (no I/O, no current time).

All fixtures use the REAL canonical contracts (SpatialObservation,
TemporalStateKey, TemporalPolicy, OccupancyState, OccupancySnapshot,
OccupancyCheckpoint) with fixed deterministic IDs so replay comparisons
are byte-exact.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.intelligence.temporal import (
    OCCUPANCY_FSM,
    OCCUPANCY_SCOPE_TRACK,
    PRESENCE_FSM,
    OccupancyEngine,
    OccupancyInput,
    OccupancyResult,
    PresenceTemporalEngine,
    TemporalInput,
    occupancy_event_from_presence,
    occupancy_scope_key,
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
    OccupancyCheckpoint,
    OccupancySnapshot,
    OccupancyState,
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

_TRACKS: dict[str, TrackId] = {
    "A": TrackId(UUID("60000000-0000-0000-0000-000000000001")),
    "B": TrackId(UUID("60000000-0000-0000-0000-000000000002")),
    "C": TrackId(UUID("60000000-0000-0000-0000-000000000003")),
    "D": TrackId(UUID("60000000-0000-0000-0000-000000000004")),
}

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


def _presence_engine(policy: TemporalPolicy | None = None, **kwargs) -> PresenceTemporalEngine:
    return PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy or TemporalPolicy(**kwargs))


def _occ_engine(policy: TemporalPolicy | None = None, **kwargs) -> OccupancyEngine:
    return OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy or TemporalPolicy(**kwargs))


def _transition(
    reason: TemporalReason,
    *,
    track_id: TrackId = _TRACK,
    event_time: datetime | None = None,
    frame_index: int = 0,
    tenant_id: TenantId = _TENANT,
    venue_id: VenueId = _VENUE,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG,
    semantic_context: str | None = None,
    fsm_kind: str = "presence",
) -> TemporalTransition:
    """A presence-family transition with deterministic content-derived id."""
    if event_time is None:
        event_time = _event(frame_index)
    key = _key(
        tenant_id=tenant_id,
        venue_id=venue_id,
        session_id=session_id,
        camera_id=camera_id,
        configuration_version_id=configuration_version_id,
        track_id=track_id,
        semantic_context=semantic_context,
        fsm_kind=fsm_kind,
    )
    return TemporalTransition(
        transition_id=EventId(
            uuid.uuid5(TEMPORAL_ID_NAMESPACE, f"t-{reason.value}-{track_id}-{frame_index}")
        ),
        fsm_kind=fsm_kind,
        key=key,
        from_state="absent",
        to_state="present",
        event_kind="present",
        reason=reason,
        observation_frame_id=_frame(frame_index),
        event_time=event_time,
        processing_time=_processing(),
        configuration_version_id=key.configuration_version_id,
        fsm_version=TEMPORAL_ENGINE_VERSION,
    )


def _scope_key(
    *,
    tenant_id: TenantId = _TENANT,
    venue_id: VenueId = _VENUE,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG,
    semantic_context: str | None = None,
) -> TemporalStateKey:
    return occupancy_scope_key(
        _key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            session_id=session_id,
            camera_id=camera_id,
            configuration_version_id=configuration_version_id,
            semantic_context=semantic_context,
        )
    )


def _occ_input(
    scope_key: TemporalStateKey, transition: TemporalTransition, kind: str
) -> OccupancyInput:
    return OccupancyInput(
        key=scope_key,
        transition=transition,
        observation_kind=kind,
        processing_time=_processing(),
    )


def _direct_apply(
    engine: OccupancyEngine,
    state: OccupancyState,
    scope_key: TemporalStateKey,
    transition: TemporalTransition,
) -> OccupancyResult:
    """Apply a presence transition with the sanctioned kind mapping."""
    return engine.apply(
        state,
        _occ_input(scope_key, transition, occupancy_event_from_presence(transition)),
    )


def _run_chain(
    presence_policy: TemporalPolicy,
    occ_engine: OccupancyEngine,
    *,
    timeline: tuple[tuple[str, str, int, int], ...],
    tenant_id: TenantId = _TENANT,
    venue_id: VenueId = _VENUE,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG,
    semantic_context: str | None = None,
) -> tuple[dict[str, TemporalState], OccupancyState, list[OccupancySnapshot]]:
    """Run the canonical chain (SpatialObservation -> presence ->
    occupancy) for a multi-track timeline: (track label, presence kind,
    seconds, frame index)."""
    presence = _presence_engine(policy=presence_policy)
    pstates: dict[str, TemporalState] = {}
    scope_key: TemporalStateKey | None = None
    occ_state: OccupancyState | None = None
    snapshots: list[OccupancySnapshot] = []
    for label, kind, seconds, frame_index in timeline:
        pkey = _key(
            track_id=_TRACKS[label],
            tenant_id=tenant_id,
            venue_id=venue_id,
            session_id=session_id,
            camera_id=camera_id,
            configuration_version_id=configuration_version_id,
            semantic_context=semantic_context,
        )
        if scope_key is None:
            scope_key = occupancy_scope_key(pkey)
            occ_state = occ_engine.initial_state(scope_key)
        pstate = pstates.get(label) or presence.initial_state(pkey)
        obs = _obs(pkey, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
        presence_result = presence.apply(pstate, _input(pkey, obs, kind=kind))
        pstates[label] = presence_result.state
        assert scope_key is not None and occ_state is not None
        occ_result = occ_engine.apply(
            occ_state,
            _occ_input(
                scope_key,
                presence_result.transitions[0],
                occupancy_event_from_presence(presence_result.transitions[0]),
            ),
        )
        occ_state = occ_result.state
        if occ_result.snapshot is not None:
            snapshots.append(occ_result.snapshot)
    assert scope_key is not None and occ_state is not None
    return pstates, occ_state, snapshots


def _quick_policy(**kwargs) -> TemporalPolicy:
    """Instant-confirm policy: every present enters, every absent exits."""
    base = TemporalPolicy(
        entry_confirmation=1,
        exit_confirmation=1,
        minimum_dwell_seconds=0,
        exit_grace_seconds=0,
    )
    return base.model_copy(update=kwargs)


# =============================================================================
# Isolated occupancy FSM transitions (per-entity, direct events)
# =============================================================================


class TestOccupancyTransitions:
    """The per-entity legal transitions and explicit rejections."""

    def test_enter_confirmed_counts_once(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        )
        assert result.snapshot is not None
        assert result.state.occupancy_count == 1
        assert result.state.occupied_tracks == frozenset({_TRACK})
        assert result.snapshot.previous_count == 0
        assert result.snapshot.delta == 1
        assert result.snapshot.occupancy_count == 1
        assert result.snapshot.event_time == _event(0)
        assert result.snapshot.source_transition_id is not None
        assert result.snapshot.key == scope

    def test_exit_confirmed_decrements_once(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.EXIT_CONFIRMED, frame_index=5, event_time=_event(5)),
        )
        assert result.snapshot is not None
        assert result.state.occupancy_count == 0
        assert result.snapshot.previous_count == 1
        assert result.snapshot.delta == -1
        assert result.snapshot.occupancy_count == 0
        assert result.snapshot.occupied_tracks == ()

    def test_missing_expired_removes_entity(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.MISSING_EXPIRED, frame_index=8, event_time=_event(8)),
        )
        assert result.state.occupancy_count == 0
        assert result.snapshot is not None
        assert result.snapshot.delta == -1

    def test_session_closed_removes_counted_entity(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.SESSION_CLOSED, frame_index=7, event_time=_event(7)),
        )
        assert result.state.occupancy_count == 0
        assert result.snapshot is not None
        assert result.snapshot.delta == -1

    def test_session_closed_while_not_counted_is_noop(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        result = _direct_apply(
            engine,
            engine.initial_state(scope),
            scope,
            _transition(TemporalReason.SESSION_CLOSED, frame_index=0, event_time=_event(0)),
        )
        assert result.state.occupancy_count == 0
        assert result.snapshot is None  # benign finalization, no fabricated change

    def test_stay_never_changes_the_count(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.OBSERVED_STAY, frame_index=2, event_time=_event(2)),
        )
        assert result.state.occupancy_count == 1
        assert result.snapshot is None
        assert result.deduplicated is False
        assert result.reordered is False

    def test_enter_while_already_counted_rejected(self) -> None:
        # A second CONFIRMED entry for an already-counted entity is
        # mis-wired orchestration — explicit rejection (never a +2).
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            _direct_apply(
                engine,
                state,
                scope,
                _transition(TemporalReason.ENTER_CONFIRMED, frame_index=2, event_time=_event(2)),
            )

    def test_exit_for_never_counted_entity_rejected(self) -> None:
        # Exit without a prior enter can never happen from the presence
        # FSM; the occupancy engine treats it as a domain invariant
        # failure — never clamped to zero.
        engine = _occ_engine()
        scope = _scope_key()
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            _direct_apply(
                engine,
                engine.initial_state(scope),
                scope,
                _transition(TemporalReason.EXIT_CONFIRMED, frame_index=0, event_time=_event(0)),
            )

    def test_missing_expired_for_never_counted_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            _direct_apply(
                engine,
                engine.initial_state(scope),
                scope,
                _transition(TemporalReason.MISSING_EXPIRED, frame_index=0, event_time=_event(0)),
            )

    def test_unknown_kind_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        transition = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0)
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(
                engine.initial_state(scope),
                _occ_input(scope, transition, "warp"),
            )

    def test_kind_reason_mismatch_rejected(self) -> None:
        # The kind must agree with the presence transition reason — a
        # mis-wired mapping is rejected explicitly.
        engine = _occ_engine()
        scope = _scope_key()
        transition = _transition(TemporalReason.EXIT_CONFIRMED, frame_index=0)
        with pytest.raises(InvalidTemporalInputError, match="reason"):
            engine.apply(
                engine.initial_state(scope),
                _occ_input(scope, transition, "enter_confirmed"),
            )

    def test_non_presence_transition_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        transition = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, fsm_kind="dwell")
        with pytest.raises(InvalidTemporalInputError, match="presence"):
            _direct_apply(engine, engine.initial_state(scope), scope, transition)


# =============================================================================
# §2. Occupancy scope key derivation
# =============================================================================


class TestOccupancyScopeKey:
    """The aggregate scope key: canonical, deterministic, isolated."""

    def test_scope_key_derivation_preserves_scope_components(self) -> None:
        pkey = _key(semantic_context="z-lobby")
        scope = occupancy_scope_key(pkey)
        assert scope.fsm_kind == "occupancy"
        assert scope.tenant_id == pkey.tenant_id
        assert scope.venue_id == pkey.venue_id
        assert scope.session_id == pkey.session_id
        assert scope.camera_id == pkey.camera_id
        assert scope.configuration_version_id == pkey.configuration_version_id
        assert scope.semantic_context == "z-lobby"
        assert scope.track_id == OCCUPANCY_SCOPE_TRACK
        assert scope.track_id != pkey.track_id

    def test_all_tracks_in_a_scope_share_one_key(self) -> None:
        a = occupancy_scope_key(_key(track_id=_TRACKS["A"]))
        b = occupancy_scope_key(_key(track_id=_TRACKS["B"]))
        assert a == b  # the aggregate identity never depends on the track

    def test_different_spatial_contexts_are_different_scopes(self) -> None:
        lobby = occupancy_scope_key(_key(semantic_context="z-lobby"))
        table = occupancy_scope_key(_key(semantic_context="t-12"))
        assert lobby != table
        assert lobby.canonical() != table.canonical()

    def test_scope_sentinel_is_not_a_real_track(self) -> None:
        assert OCCUPANCY_SCOPE_TRACK not in set(_TRACKS.values())

    def test_initial_state_rejects_non_occupancy_key(self) -> None:
        engine = _occ_engine()
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.initial_state(_key())  # presence-family key

    def test_initial_state_rejects_real_track_scope_key(self) -> None:
        engine = _occ_engine()
        bad = _key(fsm_kind="occupancy")  # real track in the track slot
        with pytest.raises(InvalidTemporalInputError, match="sentinel"):
            engine.initial_state(bad)


# =============================================================================
# §5/§6/§7. Occupancy reacts only to CONFIRMED presence (full chain)
# =============================================================================


class TestConfirmedPresenceOnly:
    """ENTERING never counts; EXITING never decrements; only confirmed
    transitions change the count."""

    def test_entering_state_is_not_counted(self) -> None:
        # entry_confirmation=2: the first present only starts ENTERING.
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(entry_confirmation=2),
            engine,
            timeline=(("A", "present", 0, 0),),
        )
        assert state.occupancy_count == 0
        assert snapshots == []

    def test_confirmed_present_counts_once(self) -> None:
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(entry_confirmation=2),
            engine,
            timeline=(("A", "present", 0, 0), ("A", "present", 2, 2)),
        )
        assert state.occupancy_count == 1  # confirmed once, not per frame
        assert len(snapshots) == 1
        assert snapshots[0].occupancy_count == 1

    def test_exiting_state_does_not_decrement(self) -> None:
        # exit_confirmation=2: the first qualified absent only starts
        # EXITING — occupancy must stay 1 until the exit is confirmed.
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(exit_confirmation=2),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("A", "absent", 5, 5),
            ),
        )
        assert state.occupancy_count == 1
        assert [s.occupancy_count for s in snapshots] == [1]

    def test_exit_confirmed_decrements(self) -> None:
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(exit_confirmation=2),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("A", "absent", 5, 5),
                ("A", "absent", 6, 6),
            ),
        )
        assert state.occupancy_count == 0
        assert [s.occupancy_count for s in snapshots] == [1, 0]


# =============================================================================
# §11/§22. Idempotent duplicates
# =============================================================================


class TestDuplicates:
    """Replaying the same confirmed transition advances occupancy once."""

    def test_replayed_enter_is_deduplicated(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        enter = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0))
        first = _direct_apply(engine, state, scope, enter)
        assert first.snapshot is not None
        second = _direct_apply(engine, first.state, scope, enter)
        assert second.deduplicated is True
        assert second.snapshot is None
        assert second.state.occupancy_count == 1  # never 2

    def test_replayed_exit_is_deduplicated(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        enter = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0))
        state = _direct_apply(engine, state, scope, enter).state
        exit_t = _transition(TemporalReason.EXIT_CONFIRMED, frame_index=5, event_time=_event(5))
        first = _direct_apply(engine, state, scope, exit_t)
        assert first.state.occupancy_count == 0
        second = _direct_apply(engine, first.state, scope, exit_t)
        assert second.deduplicated is True
        assert second.snapshot is None
        assert second.state.occupancy_count == 0  # never negative


# =============================================================================
# §12. Multiple entities / §8. re-entry
# =============================================================================


class TestMultipleEntities:
    """Unique entities each contribute at most one to the count."""

    def test_three_entities_then_partial_exits(self) -> None:
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("B", "present", 1, 1),
                ("C", "present", 2, 2),
                ("B", "absent", 3, 3),
                ("A", "present", 4, 4),
                ("A", "absent", 5, 5),
                ("C", "absent", 6, 6),
            ),
        )
        assert state.occupancy_count == 0
        assert [s.occupancy_count for s in snapshots] == [1, 2, 3, 2, 1, 0]

    def test_reentry_creates_a_new_count_cycle(self) -> None:
        # A enters, exits, then enters again: 1 -> 0 -> 1. Independent
        # presence sessions are never merged.
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.EXIT_CONFIRMED, frame_index=5, event_time=_event(5)),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=10, event_time=_event(10)),
        )
        assert result.state.occupancy_count == 1
        assert result.snapshot is not None
        assert result.snapshot.previous_count == 0
        assert result.snapshot.delta == 1


# =============================================================================
# §9/§10. Event-time authority and the 15.1 ordering policy
# =============================================================================


class TestEventTimeOrdering:
    """event_time is authoritative; reorders follow accept-with-no-rewind."""

    def test_processing_time_never_affects_the_result(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        transition = _transition(
            TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)
        )
        kind = occupancy_event_from_presence(transition)
        state = engine.initial_state(scope)
        early = engine.apply(
            state,
            OccupancyInput(
                key=scope,
                transition=transition,
                observation_kind=kind,
                processing_time=_processing(-9000),
            ),
        )
        late = engine.apply(
            state,
            OccupancyInput(
                key=scope,
                transition=transition,
                observation_kind=kind,
                processing_time=_processing(9000),
            ),
        )
        assert early.state == late.state
        assert early.snapshot == late.snapshot  # identical occupancy facts

    def test_chronological_arrival_counts_both_entities(self) -> None:
        # 10:00 A enters, 10:01 B enters, 10:02 A stays -> occupancy 2.
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["A"],
                frame_index=0,
                event_time=_event(0),
            ),
        ).state
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["B"],
                frame_index=1,
                event_time=_event(1),
            ),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.OBSERVED_STAY,
                track_id=_TRACKS["A"],
                frame_index=2,
                event_time=_event(2),
            ),
        )
        assert result.state.occupancy_count == 2

    def test_within_window_reorder_never_rewinds_the_set(self) -> None:
        # Task 15.4 §10: A enters @10:00, A stays @10:02, THEN B enters
        # @10:01. B's enter is 1s older than the watermark — within the
        # 60s reorder window — so the documented accept-with-no-rewind
        # policy applies: the REORDERED fact is recorded and the entity
        # set is NOT changed (byte-consistent with the presence/dwell
        # engines).
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["A"],
                frame_index=0,
                event_time=_event(0),
            ),
        ).state
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.OBSERVED_STAY,
                track_id=_TRACKS["A"],
                frame_index=2,
                event_time=_event(2),
            ),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["B"],
                frame_index=1,
                event_time=_event(1),
            ),
        )
        assert result.reordered is True
        assert result.snapshot is None
        assert result.state.occupancy_count == 1  # B not counted (no rewind)

    def test_replayed_reorder_reproduces_reordered(self) -> None:
        # Because a reorder never advances the watermark or positions,
        # replaying it reproduces the same REORDERED fact (never a
        # DEDUPLICATED one) — deterministic.
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["A"],
                frame_index=0,
                event_time=_event(0),
            ),
        ).state
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.OBSERVED_STAY,
                track_id=_TRACKS["A"],
                frame_index=2,
                event_time=_event(2),
            ),
        ).state
        late_b = _transition(
            TemporalReason.ENTER_CONFIRMED,
            track_id=_TRACKS["B"],
            frame_index=1,
            event_time=_event(1),
        )
        first = _direct_apply(engine, state, scope, late_b)
        second = _direct_apply(engine, state, scope, late_b)
        assert first.reordered is True
        assert second.reordered is True
        assert second.deduplicated is False

    def test_exit_after_reordered_away_enter_is_explicit_rejection(self) -> None:
        # Integration hazard (documented in the module docstring): B's
        # ENTER at 10:01 arrives within the window AFTER A's stay at
        # 10:02, so it is reordered-away and B is never counted. B's
        # later in-order EXIT (10:03) is therefore an exit for an entity
        # that was never present — the typed InvalidTransitionError, not
        # a silent skip, is the invariant enforcement.
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["A"],
                frame_index=0,
                event_time=_event(0),
            ),
        ).state
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.OBSERVED_STAY,
                track_id=_TRACKS["A"],
                frame_index=2,
                event_time=_event(2),
            ),
        ).state
        reordered = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["B"],
                frame_index=1,
                event_time=_event(1),
            ),
        )
        assert reordered.reordered is True
        assert reordered.state.occupancy_count == 1
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            _direct_apply(
                engine,
                reordered.state,
                scope,
                _transition(
                    TemporalReason.EXIT_CONFIRMED,
                    track_id=_TRACKS["B"],
                    frame_index=3,
                    event_time=_event(3),
                ),
            )

    def test_late_beyond_window_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["A"],
                frame_index=0,
                event_time=_event(0),
            ),
        ).state
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.OBSERVED_STAY,
                track_id=_TRACKS["A"],
                frame_index=120,
                event_time=_event(120),
            ),
        ).state
        with pytest.raises(LateEventError, match="reordering window"):
            _direct_apply(
                engine,
                state,
                scope,
                _transition(
                    TemporalReason.ENTER_CONFIRMED,
                    track_id=_TRACKS["B"],
                    frame_index=30,
                    event_time=_event(30),
                ),
            )

    def test_equal_positions_across_tracks_are_distinct_facts(self) -> None:
        # Two different tracks at the same (event_time, frame) are both
        # counted — dedup is per-track, never cross-track.
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["A"],
                frame_index=0,
                event_time=_event(0),
            ),
        ).state
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                track_id=_TRACKS["B"],
                frame_index=0,
                event_time=_event(0),
            ),
        )
        assert result.state.occupancy_count == 2
        assert result.snapshot is not None
        assert result.snapshot.delta == 1


# =============================================================================
# §13/§14/§15. Isolation: session / camera / configuration / tenant / venue
# =============================================================================


class TestIsolation:
    """Occupancy state never mixes across any canonical scope."""

    def test_cross_session_exit_cannot_remove_other_session(self) -> None:
        # Same track identity in two sessions: each session has its own
        # occupancy scope; an exit in session 2 leaves session 1 intact.
        engine = _occ_engine()
        s1 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000001"))
        s2 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000002"))
        _, state1, _ = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0),),
            session_id=s1,
        )
        _, state2, _ = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0), ("A", "absent", 5, 5)),
            session_id=s2,
        )
        assert state1.occupancy_count == 1  # untouched by session 2's exit
        assert state2.occupancy_count == 0

    def test_cross_session_transition_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        wrong_session = _transition(
            TemporalReason.EXIT_CONFIRMED,
            track_id=_TRACKS["A"],
            session_id=VideoSessionId(UUID("30000000-0000-0000-0000-000000000099")),
            frame_index=5,
            event_time=_event(5),
        )
        with pytest.raises(StateKeyMismatchError, match="session_id"):
            _direct_apply(engine, engine.initial_state(scope), scope, wrong_session)

    def test_cross_tenant_transition_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        wrong = _transition(
            TemporalReason.ENTER_CONFIRMED,
            track_id=_TRACKS["A"],
            tenant_id=TenantId(UUID("10000000-0000-0000-0000-000000000099")),
            frame_index=0,
            event_time=_event(0),
        )
        with pytest.raises(StateKeyMismatchError, match="tenant_id"):
            _direct_apply(engine, engine.initial_state(scope), scope, wrong)

    def test_cross_venue_transition_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        wrong = _transition(
            TemporalReason.ENTER_CONFIRMED,
            track_id=_TRACKS["A"],
            venue_id=VenueId(UUID("20000000-0000-0000-0000-000000000099")),
            frame_index=0,
            event_time=_event(0),
        )
        with pytest.raises(StateKeyMismatchError, match="venue_id"):
            _direct_apply(engine, engine.initial_state(scope), scope, wrong)

    def test_cross_camera_transition_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        wrong = _transition(
            TemporalReason.ENTER_CONFIRMED,
            track_id=_TRACKS["A"],
            camera_id=CameraId(UUID("40000000-0000-0000-0000-000000000099")),
            frame_index=0,
            event_time=_event(0),
        )
        with pytest.raises(StateKeyMismatchError, match="camera_id"):
            _direct_apply(engine, engine.initial_state(scope), scope, wrong)

    def test_cameras_have_independent_occupancy(self) -> None:
        engine = _occ_engine()
        cam_a = CameraId(UUID("40000000-0000-0000-0000-000000000001"))
        cam_b = CameraId(UUID("40000000-0000-0000-0000-000000000002"))
        _, state_a, _ = _run_chain(
            _quick_policy(), engine, timeline=(("A", "present", 0, 0),), camera_id=cam_a
        )
        _, state_b, _ = _run_chain(
            _quick_policy(), engine, timeline=(("A", "present", 0, 0),), camera_id=cam_b
        )
        assert state_a.key != state_b.key
        assert state_a.occupancy_count == 1
        assert state_b.occupancy_count == 1

    def test_configuration_version_pinned_for_historical_sessions(self) -> None:
        # §15: a V1 session stays on V1 even after V2 is published — the
        # snapshots carry the pinned configuration version.
        engine = _occ_engine()
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        _, _, snapshots_v1 = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0),),
            configuration_version_id=v1,
        )
        _, _, snapshots_v2 = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0),),
            configuration_version_id=v2,
        )
        assert snapshots_v1[0].key.configuration_version_id == v1
        assert snapshots_v2[0].key.configuration_version_id == v2
        assert snapshots_v1[0].snapshot_id != snapshots_v2[0].snapshot_id
        # Replaying the V1 timeline reproduces the V1 result unchanged.
        _, _, replay = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0),),
            configuration_version_id=v1,
        )
        assert replay == snapshots_v1


# =============================================================================
# §16/§17. Occupancy snapshot contract
# =============================================================================


class TestSnapshotContract:
    """Every count change is explained and reproducible."""

    def test_snapshot_carries_full_provenance(self) -> None:
        engine = _occ_engine()
        _, _, snapshots = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0),),
            semantic_context="z-lobby",
        )
        (snapshot,) = snapshots
        assert snapshot.fsm_kind == "occupancy"
        assert snapshot.key.semantic_context == "z-lobby"
        assert snapshot.key.configuration_version_id == _CONFIG
        assert snapshot.event_time == _event(0)
        assert snapshot.previous_count == 0
        assert snapshot.delta == 1
        assert snapshot.occupancy_count == 1
        assert set(snapshot.occupied_tracks) == {_TRACKS["A"]}
        assert snapshot.fsm_version == TEMPORAL_ENGINE_VERSION
        assert snapshot.policy_revision == "v1"

    def test_snapshot_id_content_derived_and_deterministic(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        first = _direct_apply(
            engine,
            engine.initial_state(scope),
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        )
        second = _direct_apply(
            engine,
            engine.initial_state(scope),
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        )
        assert first.snapshot is not None and second.snapshot is not None
        assert first.snapshot.snapshot_id == second.snapshot.snapshot_id


# =============================================================================
# §18/§19. Checkpoint / restart
# =============================================================================


class TestCheckpointAndRestart:
    """Restarting with occupied entities equals uninterrupted processing."""

    def _full_timeline(self) -> list[tuple[TemporalReason, TrackId, int]]:
        return [
            (TemporalReason.ENTER_CONFIRMED, _TRACKS["A"], 0),
            (TemporalReason.ENTER_CONFIRMED, _TRACKS["B"], 1),
            (TemporalReason.ENTER_CONFIRMED, _TRACKS["C"], 2),
            (TemporalReason.EXIT_CONFIRMED, _TRACKS["B"], 3),
        ]

    def test_checkpoint_restart_equals_uninterrupted(self) -> None:
        scope = _scope_key()
        # Uninterrupted: A enter, B enter, C enter, B exit.
        engine_full = _occ_engine()
        state_full = engine_full.initial_state(scope)
        for reason, track, seconds in self._full_timeline():
            state_full = _direct_apply(
                engine_full,
                state_full,
                scope,
                _transition(
                    reason, track_id=track, frame_index=seconds, event_time=_event(seconds)
                ),
            ).state

        # Restarted: A + B enter -> CHECKPOINT -> new engine -> C enter + B exit.
        engine_a = _occ_engine()
        state_a = engine_a.initial_state(scope)
        for reason, track, seconds in self._full_timeline()[:2]:
            state_a = _direct_apply(
                engine_a,
                state_a,
                scope,
                _transition(
                    reason, track_id=track, frame_index=seconds, event_time=_event(seconds)
                ),
            ).state
        checkpoint = engine_a.checkpoint(state_a)
        data = checkpoint.to_dict()
        assert OccupancyCheckpoint.from_dict(data) == checkpoint

        engine_b = _occ_engine()
        state_b = engine_b.restore(checkpoint)
        for reason, track, seconds in self._full_timeline()[2:]:
            state_b = _direct_apply(
                engine_b,
                state_b,
                scope,
                _transition(
                    reason, track_id=track, frame_index=seconds, event_time=_event(seconds)
                ),
            ).state

        assert state_b == state_full  # identical final occupancy state
        assert state_b.occupancy_count == 2  # A and C remain

    def test_restore_rejects_per_track_scope_key(self) -> None:
        # A checkpoint whose state key uses a real track (not the
        # canonical sentinel) is rejected — occupancy scopes are never
        # per-track, and apply() would reject it anyway.
        engine = _occ_engine()
        bad_key = _key(fsm_kind="occupancy")  # real track in the slot
        checkpoint = OccupancyCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
            state=OccupancyState(fsm_version=TEMPORAL_ENGINE_VERSION, key=bad_key),
        )
        with pytest.raises(InvalidTemporalInputError, match="sentinel"):
            engine.restore(checkpoint)

    def test_restore_rejects_engine_version_drift(self) -> None:
        engine = _occ_engine()
        state = engine.initial_state(_scope_key())
        checkpoint = OccupancyCheckpoint(
            engine_version="9.9.9",
            policy_revision="v1",
            state=state,
        )
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            engine.restore(checkpoint)

    def test_restore_rejects_policy_drift(self) -> None:
        engine = _occ_engine(policy=TemporalPolicy(revision="v2"))
        state = engine.initial_state(_scope_key())
        checkpoint = OccupancyCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",  # checkpoint made under a different policy
            state=state,
        )
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            engine.restore(checkpoint)


# =============================================================================
# §20-24. Golden scenarios
# =============================================================================


class TestGoldenSingleZone:
    """§20: occupancy progression 1 -> 2 -> 1 -> 0 at exact event times."""

    def test_golden_single_zone_progression(self) -> None:
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("A", "present", 1, 1),
                ("B", "present", 2, 2),
                ("A", "present", 3, 3),
                ("B", "present", 4, 4),
                ("A", "absent", 5, 5),
                ("B", "present", 6, 6),
                ("B", "absent", 7, 7),
            ),
        )
        assert state.occupancy_count == 0
        assert [s.occupancy_count for s in snapshots] == [1, 2, 1, 0]
        assert [s.event_time for s in snapshots] == [_event(0), _event(2), _event(5), _event(7)]
        assert [s.delta for s in snapshots] == [1, 1, -1, -1]
        assert [s.previous_count for s in snapshots] == [0, 1, 2, 1]
        # At the peak, both entities were counted.
        assert set(snapshots[1].occupied_tracks) == {_TRACKS["A"], _TRACKS["B"]}

    def test_golden_replay_is_identical(self) -> None:
        engine = _occ_engine()
        _, state1, snapshots1 = _run_chain(
            _quick_policy(),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("B", "present", 2, 2),
                ("A", "absent", 5, 5),
                ("B", "absent", 7, 7),
            ),
        )
        _, state2, snapshots2 = _run_chain(
            _quick_policy(),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("B", "present", 2, 2),
                ("A", "absent", 5, 5),
                ("B", "absent", 7, 7),
            ),
        )
        assert state2 == state1
        assert snapshots2 == snapshots1
        assert [s.snapshot_id for s in snapshots1] == [s.snapshot_id for s in snapshots2]


class TestGoldenJitter:
    """§21: short occlusion keeps occupancy at 1 — never 1 -> 0 -> 1."""

    def test_golden_jitter_keeps_occupancy(self) -> None:
        engine = _occ_engine()
        policy = TemporalPolicy(
            entry_confirmation=1,
            exit_confirmation=3,
            minimum_dwell_seconds=0,
            exit_grace_seconds=0,
            occlusion_tolerance_seconds=60,
        )
        _, state, snapshots = _run_chain(
            policy,
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("A", "not_observed", 1, 1),
                ("A", "present", 2, 2),
                ("A", "not_observed", 3, 3),
                ("A", "present", 4, 4),
            ),
        )
        assert state.occupancy_count == 1
        assert len(snapshots) == 1  # only the original enter
        assert snapshots[0].occupancy_count == 1


class TestGoldenDuplicate:
    """§22: replaying the same enter/exit transitions changes nothing twice."""

    def test_golden_duplicate_enter_and_exit(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        enter = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0))
        exit_t = _transition(TemporalReason.EXIT_CONFIRMED, frame_index=5, event_time=_event(5))

        first = _direct_apply(engine, state, scope, enter)
        assert first.state.occupancy_count == 1
        replay = _direct_apply(engine, first.state, scope, enter)
        assert replay.deduplicated and replay.state.occupancy_count == 1

        leaving = _direct_apply(engine, replay.state, scope, exit_t)
        assert leaving.state.occupancy_count == 0
        replay_exit = _direct_apply(engine, leaving.state, scope, exit_t)
        assert replay_exit.deduplicated and replay_exit.state.occupancy_count == 0


class TestGoldenTwoSpatialContexts:
    """§23: Zone A and Zone B occupancy never share state."""

    def test_golden_two_contexts(self) -> None:
        engine = _occ_engine()
        _, state_a, _ = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("A", "present", 0, 0),),
            semantic_context="z-lobby",
        )
        _, state_b, _ = _run_chain(
            _quick_policy(),
            engine,
            timeline=(("B", "present", 0, 0),),
            semantic_context="z-restaurant",
        )
        assert state_a.key != state_b.key
        assert state_a.occupancy_count == 1
        assert state_b.occupancy_count == 1
        assert set(state_a.occupied_tracks) == {_TRACKS["A"]}
        assert set(state_b.occupied_tracks) == {_TRACKS["B"]}


class TestGoldenTenantIsolation:
    """§24: Tenant A and Tenant B occupancy never share state."""

    def test_golden_tenant_isolation(self) -> None:
        engine = _occ_engine()
        tenant_a = TenantId(UUID("10000000-0000-0000-0000-000000000001"))
        tenant_b = TenantId(UUID("10000000-0000-0000-0000-000000000002"))
        _, state_a, _ = _run_chain(
            _quick_policy(), engine, timeline=(("A", "present", 0, 0),), tenant_id=tenant_a
        )
        _, state_b, _ = _run_chain(
            _quick_policy(), engine, timeline=(("B", "present", 0, 0),), tenant_id=tenant_b
        )
        assert state_a.key != state_b.key
        assert state_a.occupancy_count == 1
        assert state_b.occupancy_count == 1


class TestGoldenVenueIsolation:
    """Venue A and Venue B occupancy never share state."""

    def test_golden_venue_isolation(self) -> None:
        engine = _occ_engine()
        venue_a = VenueId(UUID("20000000-0000-0000-0000-000000000001"))
        venue_b = VenueId(UUID("20000000-0000-0000-0000-000000000002"))
        _, state_a, _ = _run_chain(
            _quick_policy(), engine, timeline=(("A", "present", 0, 0),), venue_id=venue_a
        )
        _, state_b, _ = _run_chain(
            _quick_policy(), engine, timeline=(("B", "present", 0, 0),), venue_id=venue_b
        )
        assert state_a.occupancy_count == 1
        assert state_b.occupancy_count == 1


# =============================================================================
# §25. Invalid inputs and corrupted-domain rejection
# =============================================================================


class TestInvalidStates:
    """Missing or malformed inputs fail explicitly — never repaired."""

    def test_missing_track_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="occupancy",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
            )

    def test_missing_tenant_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="occupancy",
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=OCCUPANCY_SCOPE_TRACK,
            )

    def test_missing_venue_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="occupancy",
                tenant_id=_TENANT,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=OCCUPANCY_SCOPE_TRACK,
            )

    def test_missing_session_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="occupancy",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=OCCUPANCY_SCOPE_TRACK,
            )

    def test_missing_camera_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="occupancy",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                configuration_version_id=_CONFIG,
                track_id=OCCUPANCY_SCOPE_TRACK,
            )

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="occupancy",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                track_id=OCCUPANCY_SCOPE_TRACK,
            )

    def test_missing_spatial_context_is_a_valid_scope(self) -> None:
        # semantic_context is optional by contract: context-agnostic
        # occupancy scopes are valid (still scoped by camera/session).
        engine = _occ_engine()
        scope = _scope_key(semantic_context=None)
        state = engine.initial_state(scope)
        result = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        )
        assert result.state.occupancy_count == 1
        assert scope.semantic_context is None

    def test_invalid_event_timestamp_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError):
            _transition(
                TemporalReason.ENTER_CONFIRMED,
                frame_index=0,
                event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
            )

    def test_negative_occupancy_is_unrepresentable(self) -> None:
        # The count is DERIVED from the entity set, so it can never be
        # negative; an attempted "would-be negative" exit is the explicit
        # InvalidTransitionError above (never clamped).
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        # Full cycle ends at 0, never below.
        for reason, frame in (
            (TemporalReason.ENTER_CONFIRMED, 0),
            (TemporalReason.EXIT_CONFIRMED, 5),
        ):
            state = _direct_apply(
                engine,
                state,
                scope,
                _transition(reason, frame_index=frame, event_time=_event(frame)),
            ).state
        assert state.occupancy_count == 0
        assert state.occupancy_count >= 0

    def test_occupied_without_position_rejected_at_contract(self) -> None:
        # Corrupted state (an entity counted but with no recorded
        # position) is rejected by the model invariant.
        with pytest.raises(ValueError, match="subset"):
            OccupancyState(
                fsm_version=TEMPORAL_ENGINE_VERSION,
                key=_scope_key(),
                occupied_tracks=frozenset({_TRACK}),
                entity_positions={},
            )

    def test_duplicate_entity_registration_rejected(self) -> None:
        # Covered at FSM level: a second confirmed entry while counted.
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        state = _direct_apply(
            engine,
            state,
            scope,
            _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0, event_time=_event(0)),
        ).state
        with pytest.raises(InvalidTransitionError, match="invalid transition"):
            _direct_apply(
                engine,
                state,
                scope,
                _transition(TemporalReason.ENTER_CONFIRMED, frame_index=2, event_time=_event(2)),
            )

    def test_restore_rejects_non_checkpoint_input(self) -> None:
        engine = _occ_engine()
        with pytest.raises(InvalidTemporalInputError, match="OccupancyCheckpoint"):
            engine.restore(object())  # type: ignore[arg-type]

    def test_state_fsm_version_mismatch_rejected(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        corrupt = OccupancyState(
            fsm_version="0.1.0",
            key=scope,
        )
        transition = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0)
        with pytest.raises(FsmVersionMismatchError, match="FSM version"):
            engine.apply(
                corrupt,
                _occ_input(scope, transition, "enter_confirmed"),
            )


# =============================================================================
# §26/§27. Invariant + bounded deterministic sequence tests
# =============================================================================


class TestInvariants:
    """The occupancy invariants hold after every transition."""

    def _assert_invariants(self, state: OccupancyState) -> None:
        assert state.occupancy_count >= 0  # invariant 1
        assert state.occupancy_count == len(state.occupied_tracks)  # invariant 2
        # invariant 3: every occupied entity belongs to the same scope
        # (all tracks in the state are from this session's fixed set) and
        # has a recorded position.
        assert state.occupied_tracks <= set(state.entity_positions)
        assert state.key.fsm_kind == "occupancy"
        assert state.key.track_id == OCCUPANCY_SCOPE_TRACK

    def test_deterministic_sequence_invariants_hold_after_every_step(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        # Deterministic sequence: 8 DISTINCT entities enter (t=0..7),
        # each with an interleaved stay, then all exit in reverse order.
        tracks = [TrackId(uuid.uuid5(TEMPORAL_ID_NAMESPACE, f"inv-track-{i}")) for i in range(8)]
        steps: list[tuple[TemporalReason, TrackId, int]] = []
        for i, track in enumerate(tracks):
            steps.append((TemporalReason.ENTER_CONFIRMED, track, i))
        for i, track in enumerate(tracks):
            steps.append((TemporalReason.OBSERVED_STAY, track, 8 + i))
        for i, track in enumerate(tracks):
            steps.append((TemporalReason.EXIT_CONFIRMED, track, 16 + i))
        for reason, track, seconds in steps:
            result = _direct_apply(
                engine,
                state,
                scope,
                _transition(
                    reason, track_id=track, frame_index=seconds, event_time=_event(seconds)
                ),
            )
            state = result.state
            self._assert_invariants(state)
            if result.snapshot is not None:
                snapshot = result.snapshot
                assert snapshot.previous_count + snapshot.delta == snapshot.occupancy_count
                assert snapshot.occupancy_count == state.occupancy_count
                # invariant 5: a confirmed exit removes exactly one entity.
                if snapshot.delta == -1:
                    assert snapshot.previous_count == snapshot.occupancy_count + 1
        assert state.occupancy_count == 0
        assert len(state.entity_positions) == 8  # positions retained for dedup

    def test_occupancy_cannot_change_without_a_valid_transition(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        # An unknown kind and a mis-wired kind are both rejected, so a
        # count change is impossible without a sanctioned transition.
        transition = _transition(TemporalReason.ENTER_CONFIRMED, frame_index=0)
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(state, _occ_input(scope, transition, "warp"))
        with pytest.raises(InvalidTemporalInputError, match="reason"):
            engine.apply(state, _occ_input(scope, transition, "exit_confirmed"))


# =============================================================================
# §28. Performance at realistic scale
# =============================================================================


class TestPerformance:
    """Simultaneous tracks stay bounded; bookkeeping is keyed, not scanned."""

    def test_many_simultaneous_entities_stay_bounded(self) -> None:
        engine = _occ_engine()
        scope = _scope_key()
        state = engine.initial_state(scope)
        count = 2000
        start = time.perf_counter()
        for i in range(count):
            track = TrackId(uuid.uuid5(TEMPORAL_ID_NAMESPACE, f"perf-track-{i}"))
            state = _direct_apply(
                engine,
                state,
                scope,
                _transition(
                    TemporalReason.ENTER_CONFIRMED,
                    track_id=track,
                    frame_index=i,
                    event_time=_event(i),
                ),
            ).state
        enter_elapsed = time.perf_counter() - start

        assert state.occupancy_count == count
        assert len(state.entity_positions) == count  # O(1) dict bookkeeping
        assert len(state.occupied_tracks) == count

        # Checkpoint remains serializable and round-trips exactly.
        checkpoint = engine.checkpoint(state)
        data = checkpoint.to_dict()
        assert len(data["state"]["occupied_tracks"]) == count
        assert OccupancyCheckpoint.from_dict(data) == checkpoint

        # All entities exit: count returns to zero without going negative.
        for i in range(count):
            track = TrackId(uuid.uuid5(TEMPORAL_ID_NAMESPACE, f"perf-track-{i}"))
            state = _direct_apply(
                engine,
                state,
                scope,
                _transition(
                    TemporalReason.EXIT_CONFIRMED,
                    track_id=track,
                    frame_index=count + i,
                    event_time=_event(count + i),
                ),
            ).state
            assert state.occupancy_count >= 0
        assert state.occupancy_count == 0

        # Generous bound: 4000 pure operations must not be pathologically
        # slow (a linear-scan implementation would be visibly quadratic).
        assert enter_elapsed < 30.0


# =============================================================================
# Full canonical chain + mapping completeness
# =============================================================================


class TestChainIntegration:
    """SpatialObservation -> presence_kind -> presence -> occupancy."""

    def test_chain_via_spatial_statuses_without_explicit_kinds(self) -> None:
        engine = _occ_engine()
        presence = _presence_engine(policy=_quick_policy())
        scope_key: TemporalStateKey | None = None
        occ_state: OccupancyState | None = None
        snapshots: list[OccupancySnapshot] = []
        pkey_a = _key(track_id=_TRACKS["A"])
        pkey_b = _key(track_id=_TRACKS["B"])
        pstate_a = presence.initial_state(pkey_a)
        pstate_b = presence.initial_state(pkey_b)
        for pkey, pstate_name, status, seconds, frame_index in (
            (pkey_a, "a", SpatialStatus.INSIDE, 0, 0),
            (pkey_b, "b", SpatialStatus.INSIDE, 1, 1),
            (pkey_a, "a", SpatialStatus.OUTSIDE, 5, 5),
            (pkey_b, "b", SpatialStatus.OUTSIDE, 7, 7),
        ):
            obs = _status_obs(
                pkey, status=status, event_time=_event(seconds), frame_id=_frame(frame_index)
            )
            if pstate_name == "a":
                presence_result = presence.apply(pstate_a, _input(pkey, obs))
                pstate_a = presence_result.state
            else:
                presence_result = presence.apply(pstate_b, _input(pkey, obs))
                pstate_b = presence_result.state
            if scope_key is None:
                scope_key = occupancy_scope_key(pkey)
                occ_state = engine.initial_state(scope_key)
            assert scope_key is not None and occ_state is not None
            occ_result = engine.apply(
                occ_state,
                _occ_input(
                    scope_key,
                    presence_result.transitions[0],
                    occupancy_event_from_presence(presence_result.transitions[0]),
                ),
            )
            occ_state = occ_result.state
            if occ_result.snapshot is not None:
                snapshots.append(occ_result.snapshot)
        assert occ_state is not None
        assert occ_state.occupancy_count == 0
        assert [s.occupancy_count for s in snapshots] == [1, 2, 1, 0]


class TestOccupancyEventMapping:
    """occupancy_event_from_presence covers every presence reason."""

    def _transition(self, reason: TemporalReason) -> TemporalTransition:
        return _transition(reason, frame_index=0)

    def test_mapping_covers_all_reasons(self) -> None:
        assert occupancy_event_from_presence(self._transition(TemporalReason.ENTER_CONFIRMED)) == (
            "enter_confirmed"
        )
        assert occupancy_event_from_presence(self._transition(TemporalReason.EXIT_CONFIRMED)) == (
            "exit_confirmed"
        )
        assert occupancy_event_from_presence(self._transition(TemporalReason.MISSING_EXPIRED)) == (
            "missing_expired"
        )
        assert occupancy_event_from_presence(self._transition(TemporalReason.SESSION_CLOSED)) == (
            "session_closed"
        )
        assert occupancy_event_from_presence(self._transition(TemporalReason.OBSERVED_STAY)) == (
            "stay"
        )
        assert occupancy_event_from_presence(self._transition(TemporalReason.DEDUPLICATED)) == (
            "stay"
        )
        assert occupancy_event_from_presence(self._transition(TemporalReason.REORDERED)) == "stay"


# =============================================================================
# §29. Pure core
# =============================================================================


class TestOccupancyPurity:
    """The occupancy core performs no I/O and reads no current time."""

    def test_occupancy_core_is_pure(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
        text = (package_dir / "occupancy.py").read_text()
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
                f"I/O/stateful module {module!r} leaked into occupancy.py"
            )
        assert "now(" not in text
        assert "utc_now" not in text
        assert "print(" not in text
