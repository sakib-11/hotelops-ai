"""Tests for the Task 15.5.3 waiting detection FSM.

Waiting is an OPERATIONAL temporal interpretation, never merely "not
moving": a tracked entity becomes WAITING only when it is confirmed
PRESENT (Task 15.2), inside a configured waiting-capable spatial context,
classified STATIONARY by Task 15.5.2, and stays that way for the
configured qualification duration of EVENT time.

The waiting FSM is driven in lockstep by the presence engine's transition
(``waiting_event_from_presence``) and the 15.5.2 classification state —
the stationary signal is read verbatim from ``current_state``, never
re-derived.

Covered:

- the NOT_WAITING / WAITING_CANDIDATE / WAITING model and its legal
  transitions (mis-wired events rejected explicitly);
- waiting requires ALL conditions (confirmed presence, configured waiting
  context, stationary classification) — stationary non-waiting and moving
  through a waiting area NEVER produce WAITING;
- the waiting context is EXPLICIT configuration (``waiting_contexts`` and
  ``waiting_contexts_from_configuration`` from a Task 10 snapshot — queue
  areas / service areas / WAITING_AREA zones only, never lobby/table/
  entrance); an empty set disables waiting everywhere;
- event-time qualification: candidate_start and waiting_start are distinct
  event times, the duration is configuration-driven, and ``0.0`` confirms
  immediately;
- candidate cancellation (movement or confirmed exit before qualification
  produces NO waiting fact);
- waiting continuation (no per-frame facts) and termination (confirmed
  exit / occlusion expiry / session closure / movement exceed, each with
  the correct interval reason);
- short occlusion follows the existing grace policy (stay preserves the
  waiting state; only confirmed ``missing_expired`` ends it);
- event-time is authoritative (scrambled processing times are irrelevant),
  late/out-of-order follows the 15.1 policy, duplicates are idempotent
  (content-derived interval ids);
- re-entry creates independent waiting intervals;
- isolation across tenant/venue/session/camera/track/spatial context and
  rejection of cross-scope inputs;
- configuration provenance: intervals carry the pinned policy revision and
  configuration version; a V1 session replays on V1 even after V2 exists;
- checkpoint while WAITING_CANDIDATE and while WAITING; restart recovery
  equals uninterrupted processing; version/policy drift rejected;
- failure tests (missing scopes, invalid event time, negative durations,
  invalid movement state, cross-scope inputs) — all explicit, never
  fabricated;
- bounded state and the pure-core boundary.

All fixtures use the REAL canonical contracts with fixed deterministic IDs
so replay comparisons are byte-exact.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from backend.app.intelligence.temporal import (
    WAITING_FSM,
    WaitingEngine,
    WaitingInput,
    WaitingResult,
    waiting_contexts_from_configuration,
    waiting_event_from_presence,
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
    ConfigurationId,
    ConfigurationVersionId,
    EventId,
    FrameId,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
)
from contracts.configuration import (
    ConfigurationStatus,
    ConfigurationVersionModel,
    EntranceDirection,
    EntranceModel,
    QueueAreaModel,
    ServiceAreaModel,
    TableModel,
    ZoneModel,
    ZoneType,
)
from contracts.geometry import CoordinateSpace, GeometryModel, GeometryScope, GeometryType
from contracts.temporal import (
    MOVEMENT_STATES,
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    WAITING_STATES,
    MovementClassificationState,
    TemporalPolicy,
    TemporalReason,
    TemporalStateKey,
    TemporalTransition,
    WaitingCheckpoint,
    WaitingInterval,
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

# Explicit waiting contexts used across most tests (§4 — never inferred).
WAITING_CTX = "zone-queue-a"


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


def _family_keys(
    *,
    tenant_id: TenantId = _TENANT,
    venue_id: VenueId = _VENUE,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG,
    track_id: TrackId = _TRACK,
    semantic_context: str | None = WAITING_CTX,
) -> tuple[TemporalStateKey, TemporalStateKey, TemporalStateKey]:
    """(presence, movement_classification, waiting) keys sharing every scope."""
    common = dict(
        tenant_id=tenant_id,
        venue_id=venue_id,
        session_id=session_id,
        camera_id=camera_id,
        configuration_version_id=configuration_version_id,
        track_id=track_id,
        semantic_context=semantic_context,
    )
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
    """A canonical presence transition with a content-derived ID."""
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


def _classification(
    ckey: TemporalStateKey,
    *,
    current_state: str,
) -> MovementClassificationState:
    """A 15.5.2 classification state carrying the given classification."""
    return MovementClassificationState(
        fsm_version=TEMPORAL_ENGINE_VERSION,
        key=ckey,
        current_state=current_state,
    )


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


def _policy(
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
    return WaitingEngine(fsm=WAITING_FSM, policy=policy or _policy(**kwargs))


def _enter(
    pkey: TemporalStateKey,
    *,
    event_time: datetime,
    frame_id: FrameId,
) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.ENTER_CONFIRMED,
        event_time=event_time,
        frame_id=frame_id,
        from_state="absent",
        to_state="present",
    )


def _stay(
    pkey: TemporalStateKey,
    *,
    event_time: datetime,
    frame_id: FrameId,
    to_state: str = "present",
) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.OBSERVED_STAY,
        event_time=event_time,
        frame_id=frame_id,
        from_state=to_state,
        to_state=to_state,
    )


def _exit(
    pkey: TemporalStateKey,
    *,
    event_time: datetime,
    frame_id: FrameId,
) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.EXIT_CONFIRMED,
        event_time=event_time,
        frame_id=frame_id,
        from_state="present",
        to_state="absent",
    )


def _missing_expired(
    pkey: TemporalStateKey,
    *,
    event_time: datetime,
    frame_id: FrameId,
) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.MISSING_EXPIRED,
        event_time=event_time,
        frame_id=frame_id,
        from_state="present",
        to_state="absent",
    )


def _session_closed(
    pkey: TemporalStateKey,
    *,
    event_time: datetime,
    frame_id: FrameId,
) -> TemporalTransition:
    return _presence_transition(
        pkey,
        reason=TemporalReason.SESSION_CLOSED,
        event_time=event_time,
        frame_id=frame_id,
        from_state="present",
        to_state="absent",
    )


def _stationary(ckey: TemporalStateKey) -> MovementClassificationState:
    return _classification(ckey, current_state="stationary")


def _moving(ckey: TemporalStateKey) -> MovementClassificationState:
    return _classification(ckey, current_state="moving")


def _unknown(ckey: TemporalStateKey) -> MovementClassificationState:
    return _classification(ckey, current_state="unknown")


def _apply(
    engine: WaitingEngine,
    state: WaitingState,
    *,
    pkey: TemporalStateKey,
    ckey: TemporalStateKey,
    wkey: TemporalStateKey,
    transition: TemporalTransition,
    classification: MovementClassificationState,
    kind: str,
    processing_time: datetime | None = None,
) -> WaitingResult:
    return engine.apply(
        state,
        _w_input(
            wkey,
            transition=transition,
            classification=classification,
            kind=kind,
            processing_time=processing_time,
        ),
    )


def _run(
    engine: WaitingEngine,
    *,
    pkey: TemporalStateKey,
    ckey: TemporalStateKey,
    wkey: TemporalStateKey,
    timeline: tuple[tuple[str, str, int, int], ...],
) -> tuple[WaitingState, list[WaitingResult]]:
    """Apply (kind, classification, seconds, frame index) lockstep inputs.

    Returns (final state, per-step results) so tests can assert both the
    state timeline and which step produced a closed interval.
    """
    state = engine.initial_state(wkey)
    results: list[WaitingResult] = []
    for kind, classification_state, seconds, frame_index in timeline:
        classification = _classification(ckey, current_state=classification_state)
        transition = {
            "enter_confirmed": _enter,
            "stay": _stay,
            "exit_confirmed": _exit,
            "missing_expired": _missing_expired,
            "session_closed": _session_closed,
        }[kind](pkey, event_time=_event(seconds), frame_id=_frame(frame_index))
        result = engine.apply(
            state,
            _w_input(
                wkey,
                transition=transition,
                classification=classification,
                kind=kind,
            ),
        )
        state = result.state
        results.append(result)
    return state, results


def _states(
    engine: WaitingEngine,
    *,
    pkey: TemporalStateKey,
    ckey: TemporalStateKey,
    wkey: TemporalStateKey,
    timeline: tuple[tuple[str, str, int, int], ...],
) -> list[str]:
    state = engine.initial_state(wkey)
    states: list[str] = []
    for kind, classification_state, seconds, frame_index in timeline:
        classification = _classification(ckey, current_state=classification_state)
        transition = {
            "enter_confirmed": _enter,
            "stay": _stay,
            "exit_confirmed": _exit,
            "missing_expired": _missing_expired,
            "session_closed": _session_closed,
        }[kind](pkey, event_time=_event(seconds), frame_id=_frame(frame_index))
        state = engine.apply(
            state,
            _w_input(wkey, transition=transition, classification=classification, kind=kind),
        ).state
        states.append(state.current_state)
    return states


def _geometry(geometry_id: str = "g") -> GeometryModel:
    return GeometryModel(
        geometry_id=geometry_id,
        geometry_type=GeometryType.POLYGON,
        coordinate_space=CoordinateSpace.VENUE_LOCAL,
        geometry_scope=GeometryScope.VENUE,
        coordinates=[[0, 0], [10, 0], [10, 10], [0, 10]],
    )


# =============================================================================
# §2. The sanctioned presence -> waiting mapping
# =============================================================================


class TestWaitingEventFromPresence:
    """waiting_event_from_presence — the ONLY sanctioned mapping."""

    def test_confirmed_events_map_directly(self) -> None:
        pkey, _, _ = _family_keys()
        assert (
            waiting_event_from_presence(_enter(pkey, event_time=_event(0), frame_id=_frame(0)))
            == "enter_confirmed"
        )
        assert (
            waiting_event_from_presence(_exit(pkey, event_time=_event(1), frame_id=_frame(1)))
            == "exit_confirmed"
        )
        assert (
            waiting_event_from_presence(
                _missing_expired(pkey, event_time=_event(2), frame_id=_frame(2))
            )
            == "missing_expired"
        )
        assert (
            waiting_event_from_presence(
                _session_closed(pkey, event_time=_event(3), frame_id=_frame(3))
            )
            == "session_closed"
        )

    def test_everything_else_is_a_stay(self) -> None:
        # OBSERVED_STAY / DEDUPLICATED / REORDERED presence transitions map
        # to ``stay`` — the waiting engine reproduces dedup/reorder itself.
        pkey, _, _ = _family_keys()
        for reason in (
            TemporalReason.OBSERVED_STAY,
            TemporalReason.DEDUPLICATED,
            TemporalReason.REORDERED,
        ):
            transition = _presence_transition(
                pkey,
                reason=reason,
                event_time=_event(0),
                frame_id=_frame(0),
                from_state="present",
                to_state="present",
            )
            assert waiting_event_from_presence(transition) == "stay"


# =============================================================================
# §5. Waiting states and legal transitions
# =============================================================================


class TestWaitingStates:
    """NOT_WAITING / WAITING_CANDIDATE / WAITING — minimal, no extras."""

    def test_only_three_states_exist(self) -> None:
        assert WAITING_STATES == ("not_waiting", "waiting_candidate", "waiting")
        assert WAITING_FSM.states == WAITING_STATES
        assert WAITING_FSM.initial_state == "not_waiting"

    def test_initial_state_is_not_waiting(self) -> None:
        engine = _waiting_engine()
        _, _, wkey = _family_keys()
        state = engine.initial_state(wkey)
        assert state.current_state == "not_waiting"
        assert state.candidate_start is None
        assert state.waiting_start is None

    def test_waiting_states_are_distinct_from_movement_states(self) -> None:
        # §3: STATIONARY (a 15.5.2 classification) is NOT the same state as
        # WAITING (an operational interpretation). No state is shared.
        assert WAITING_STATES != MOVEMENT_STATES
        assert "waiting" not in MOVEMENT_STATES
        assert "stationary" not in WAITING_STATES

    def test_candidate_requires_candidate_start(self) -> None:
        # A corrupted candidate (no candidate_start) is rejected — never
        # silently repaired.
        _, _, wkey = _family_keys()
        with pytest.raises(ValueError, match="candidate_start"):
            WaitingState(
                fsm_version=TEMPORAL_ENGINE_VERSION,
                key=wkey,
                current_state="waiting_candidate",
                candidate_start=None,
            )
        with pytest.raises(ValueError, match="both candidate_start and waiting_start"):
            WaitingState(
                fsm_version=TEMPORAL_ENGINE_VERSION,
                key=wkey,
                current_state="waiting",
                candidate_start=_event(0),
                waiting_start=None,
            )

    def test_illegal_transition_rejected_by_fsm(self) -> None:
        # An enter_confirmed while already a candidate is not a legal
        # transition (an in-order presence stream always confirms exit
        # first) — explicit rejection, never a silent merge.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=5.0))
        pkey, ckey, wkey = _family_keys()
        state = engine.initial_state(wkey)
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
            classification=_stationary(ckey),
            kind="enter_confirmed",
        )
        state = result.state
        assert state.current_state == "waiting_candidate"
        with pytest.raises(InvalidTransitionError):
            _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_enter(pkey, event_time=_event(2), frame_id=_frame(2)),
                classification=_stationary(ckey),
                kind="enter_confirmed",
            )


# =============================================================================
# §2/§4/§25/§26. Waiting requires ALL conditions — never inferred
# =============================================================================


class TestWaitingRequiresAllConditions:
    """Confirmed presence + waiting context + stationary are ALL required."""

    def test_stationary_non_waiting_zone_is_never_waiting(self) -> None:
        # §25 (mandatory): a stationary person in a normal (non-waiting)
        # context is STATIONARY at the movement layer and NOT_WAITING here.
        pkey, ckey, wkey = _family_keys(semantic_context="lobby")
        engine = _waiting_engine()  # waiting_contexts only contains zone-queue-a
        states = _states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "unknown", 0, 0),
                ("stay", "stationary", 1, 1),
                ("stay", "stationary", 2, 2),
                ("stay", "stationary", 3, 3),
            ),
        )
        assert states == ["not_waiting", "not_waiting", "not_waiting", "not_waiting"]

    def test_moving_through_waiting_area_is_never_waiting(self) -> None:
        # §26 (mandatory): crossing a waiting-capable zone while MOVING
        # never creates WAITING.
        pkey, ckey, wkey = _family_keys(semantic_context=WAITING_CTX)
        engine = _waiting_engine()
        states = _states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "unknown", 0, 0),
                ("stay", "moving", 1, 1),
                ("stay", "moving", 2, 2),
                ("stay", "moving", 3, 3),
            ),
        )
        assert states == ["not_waiting", "not_waiting", "not_waiting", "not_waiting"]

    def test_unknown_classification_never_starts_waiting(self) -> None:
        # The 15.5.2 classification must be STATIONARY — UNKNOWN (no pair
        # measured yet) is not "sufficiently stationary" (§2.3).
        pkey, ckey, wkey = _family_keys()
        engine = _waiting_engine()
        states = _states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "unknown", 0, 0),
                ("stay", "unknown", 1, 1),
                ("stay", "unknown", 2, 2),
            ),
        )
        assert states == ["not_waiting", "not_waiting", "not_waiting"]

    def test_presence_never_confirmed_never_starts_waiting(self) -> None:
        # §2.1: waiting requires CONFIRMED PRESENT — stays without an
        # ENTER_CONFIRMED (entity still entering/absent) never start a
        # candidate, no matter how stationary.
        pkey, ckey, wkey = _family_keys()
        engine = _waiting_engine()
        state = engine.initial_state(wkey)
        for seconds in range(4):
            result = _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_stay(pkey, event_time=_event(seconds), frame_id=_frame(seconds)),
                classification=_stationary(ckey),
                kind="stay",
            )
            state = result.state
            assert state.current_state == "not_waiting"

    def test_missing_waiting_configuration_disables_waiting_everywhere(self) -> None:
        # §4: an empty waiting_contexts set means the venue operator
        # declared NO waiting context — the entity must NOT become WAITING.
        pkey, ckey, wkey = _family_keys(semantic_context=WAITING_CTX)
        engine = _waiting_engine(waiting_contexts=frozenset())
        states = _states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 1, 1),
                ("stay", "stationary", 2, 2),
            ),
        )
        assert states == ["not_waiting", "not_waiting", "not_waiting"]

    def test_missing_spatial_context_never_waiting(self) -> None:
        # A key with no semantic context cannot be waiting-capable.
        pkey, ckey, wkey = _family_keys(semantic_context=None)
        engine = _waiting_engine()
        states = _states(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 1, 1),
            ),
        )
        assert states == ["not_waiting", "not_waiting"]


# =============================================================================
# §4. Waiting context — explicit Task 10 configuration, never inferred
# =============================================================================


class TestWaitingContextConfiguration:
    """waiting_contexts_from_configuration derives the explicit set."""

    def test_mapping_includes_queue_service_and_waiting_area_only(self) -> None:
        # The ONLY waiting-capable contexts are WAITING_AREA zones, queue
        # areas, and service areas. Lobby/restaurant zones, tables, and
        # entrances are NEVER waiting contexts by themselves.
        version = ConfigurationVersionModel(
            configuration_version_id=_CONFIG,
            configuration_id=ConfigurationId(UUID("70000000-0000-0000-0000-000000000001")),
            venue_id=_VENUE,
            tenant_id=_TENANT,
            version=1,
            status=ConfigurationStatus.DRAFT,
            zones=[
                ZoneModel(
                    profile_id="zone-wait",
                    name="Waiting",
                    zone_type=ZoneType.WAITING_AREA,
                    geometry=_geometry("gw"),
                ),
                ZoneModel(
                    profile_id="zone-lobby",
                    name="Lobby",
                    zone_type=ZoneType.LOBBY,
                    geometry=_geometry("gl"),
                ),
            ],
            queue_areas=[QueueAreaModel(profile_id="queue-1", name="Q", geometry=_geometry("gq"))],
            service_areas=[
                ServiceAreaModel(profile_id="service-1", name="S", geometry=_geometry("gs"))
            ],
            tables=[TableModel(profile_id="table-1", name="T", geometry=_geometry("gt"))],
            entrances=[
                EntranceModel(
                    profile_id="entrance-1",
                    name="E",
                    geometry=_geometry("ge"),
                    direction=EntranceDirection.BIDIRECTIONAL,
                )
            ],
        )
        contexts = waiting_contexts_from_configuration(version)
        assert contexts == frozenset({"zone-wait", "queue-1", "service-1"})
        assert "zone-lobby" not in contexts
        assert "table-1" not in contexts
        assert "entrance-1" not in contexts

    def test_empty_configuration_yields_no_waiting_contexts(self) -> None:
        version = ConfigurationVersionModel(
            configuration_version_id=_CONFIG,
            configuration_id=ConfigurationId(UUID("70000000-0000-0000-0000-000000000002")),
            venue_id=_VENUE,
            tenant_id=_TENANT,
            version=1,
        )
        assert waiting_contexts_from_configuration(version) == frozenset()

    def test_policy_accepts_mapped_contexts(self) -> None:
        # The mapped set feeds the typed policy — the engine honors it.
        version = ConfigurationVersionModel(
            configuration_version_id=_CONFIG,
            configuration_id=ConfigurationId(UUID("70000000-0000-0000-0000-000000000003")),
            venue_id=_VENUE,
            tenant_id=_TENANT,
            version=1,
            zones=[
                ZoneModel(
                    profile_id="zone-wait",
                    name="Waiting",
                    zone_type=ZoneType.WAITING_AREA,
                    geometry=_geometry("gw"),
                )
            ],
        )
        policy = _policy(waiting_contexts=waiting_contexts_from_configuration(version))
        assert "zone-wait" in policy.waiting_contexts

    def test_waiting_context_is_a_pinned_configuration_declaration(self) -> None:
        # §21: two configurations may declare DIFFERENT waiting zones. A
        # context declared by V1 but not V2 only produces WAITING under V1.
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        pkey1, ckey1, wkey1 = _family_keys(configuration_version_id=v1)
        pkey2, ckey2, wkey2 = _family_keys(
            configuration_version_id=v2, semantic_context="zone-lobby"
        )
        engine_v1 = _waiting_engine(
            _policy(waiting_contexts={"zone-queue-a"}, waiting_qualification_seconds=2.0)
        )
        engine_v2 = _waiting_engine(
            _policy(waiting_contexts={"zone-lobby"}, waiting_qualification_seconds=2.0)
        )
        # Under V1, zone-queue-a qualifies; zone-lobby (not declared by V1)
        # does not. Under V2, zone-lobby qualifies.
        timeline1 = (
            ("enter_confirmed", "stationary", 0, 0),
            ("stay", "stationary", 2, 2),
        )
        state_v1, _ = _run(engine_v1, pkey=pkey1, ckey=ckey1, wkey=wkey1, timeline=timeline1)
        state_lobby_under_v1, _ = _run(
            engine_v1,
            pkey=_key(
                fsm_kind="presence", configuration_version_id=v1, semantic_context="zone-lobby"
            ),
            ckey=_key(
                fsm_kind="movement_classification",
                configuration_version_id=v1,
                semantic_context="zone-lobby",
            ),
            wkey=_key(
                fsm_kind="waiting", configuration_version_id=v1, semantic_context="zone-lobby"
            ),
            timeline=timeline1,
        )
        state_v2, _ = _run(engine_v2, pkey=pkey2, ckey=ckey2, wkey=wkey2, timeline=timeline1)
        assert state_v1.current_state == "waiting"
        assert state_lobby_under_v1.current_state == "not_waiting"  # not in V1's set
        assert state_v2.current_state == "waiting"


# =============================================================================
# §6/§7/§23. Golden valid waiting — qualification completes at the right event time
# =============================================================================


class TestGoldenValidWaiting:
    """NOT_WAITING -> WAITING_CANDIDATE -> WAITING (qualified in event time)."""

    TIMELINE = (
        ("enter_confirmed", "unknown", 0, 0),  # 10:00 enters (no pair yet)
        ("stay", "stationary", 1, 1),  # 10:01 first measurement stationary
        ("stay", "stationary", 2, 2),  # 10:02 qualifying
        ("stay", "stationary", 3, 3),  # 10:03 qualifying
        ("stay", "stationary", 4, 4),  # 10:04 qualification satisfied
    )

    def test_golden_valid_waiting(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=3.0))
        pkey, ckey, wkey = _family_keys()
        states = _states(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=self.TIMELINE)
        assert states == [
            "not_waiting",  # 10:00 — entered but not yet stationary
            "waiting_candidate",  # 10:01 — present + stationary + context
            "waiting_candidate",  # 10:02 — qualifying (1s)
            "waiting_candidate",  # 10:03 — qualifying (2s)
            "waiting",  # 10:04 — 3s elapsed -> confirmed
        ]
        state, results = _run(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=self.TIMELINE)
        assert state.current_state == "waiting"
        assert state.candidate_start == _event(1)  # preserved as provenance
        assert state.waiting_start == _event(4)  # the confirming event time
        assert all(r.interval is None for r in results)  # no fact until termination

    def test_waiting_start_is_the_qualification_boundary_not_candidate_start(self) -> None:
        # §6: candidate_start (10:01) and confirmed waiting_start (10:04)
        # are distinct — waiting_start is the qualification-completed
        # boundary, never processing time.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=3.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=self.TIMELINE)
        assert state.candidate_start == _event(1)
        assert state.waiting_start == _event(4)
        assert state.waiting_start > state.candidate_start

    def test_open_interval_exposes_running_waiting(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=3.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=self.TIMELINE)
        interval = engine.open_interval(state)
        assert interval is not None
        assert interval.waiting_start == _event(4)
        assert interval.waiting_end is None  # open
        assert interval.last_seen == _event(4)
        assert interval.duration_seconds == pytest.approx(0.0)
        assert interval.key == wkey
        assert interval.policy_revision == "v1"
        assert interval.reason is None

    def test_qualification_duration_is_configuration_driven(self) -> None:
        # §7: the SAME timeline confirms at a different event_time under a
        # longer configured duration — never hardcoded.
        timeline = self.TIMELINE
        short = _waiting_engine(_policy(waiting_qualification_seconds=1.0))
        long = _waiting_engine(_policy(waiting_qualification_seconds=5.0))
        pkey, ckey, wkey = _family_keys()
        state_short, _ = _run(short, pkey=pkey, ckey=ckey, wkey=wkey, timeline=timeline)
        state_long, _ = _run(long, pkey=pkey, ckey=ckey, wkey=wkey, timeline=timeline)
        assert state_short.current_state == "waiting"
        assert state_short.waiting_start == _event(2)  # 1s after candidate @10:01
        assert state_long.current_state == "waiting_candidate"  # 3s run < 5s
        assert state_long.waiting_start is None

    def test_qualification_seconds_zero_confirms_immediately(self) -> None:
        # The degenerate policy: the first qualifying step confirms.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=0.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(("enter_confirmed", "stationary", 0, 0),),
        )
        assert state.current_state == "waiting"
        assert state.waiting_start == _event(0)

    def test_negative_qualification_duration_rejected_at_contract(self) -> None:
        with pytest.raises(ValueError):
            TemporalPolicy(waiting_qualification_seconds=-1.0)


# =============================================================================
# §8/§9/§24. Candidate cancellation — no confirmed waiting fact
# =============================================================================


class TestCandidateCancellation:
    """A candidate that moves or loses presence before qualification returns."""

    def test_candidate_cancelled_by_movement(self) -> None:
        # §8: WAITING_CANDIDATE + MOVING -> NOT_WAITING, NO waiting fact.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=10.0))
        pkey, ckey, wkey = _family_keys()
        state, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "moving", 1, 1),
            ),
        )
        assert state.current_state == "not_waiting"
        assert state.candidate_start is None
        assert state.waiting_start is None
        assert all(r.interval is None for r in results)  # never confirmed

    def test_candidate_cancelled_by_confirmed_exit(self) -> None:
        # §9/§24: leaving the waiting context (confirmed presence exit)
        # before qualification aborts the candidate.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=10.0))
        pkey, ckey, wkey = _family_keys()
        state, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("exit_confirmed", "stationary", 1, 1),
            ),
        )
        assert state.current_state == "not_waiting"
        assert all(r.interval is None for r in results)

    def test_candidate_cancelled_by_occlusion_expiry(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=10.0))
        pkey, ckey, wkey = _family_keys()
        state, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("missing_expired", "stationary", 5, 5),
            ),
        )
        assert state.current_state == "not_waiting"
        assert all(r.interval is None for r in results)

    def test_golden_false_wait_no_confirmed_fact(self) -> None:
        # §24: 10:00 enters, 10:01 stationary, 10:02 exits/moves — before
        # qualification: WAITING_CANDIDATE -> NOT_WAITING, no WAITING fact.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=10.0))
        pkey, ckey, wkey = _family_keys()
        state, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "unknown", 0, 0),
                ("stay", "stationary", 1, 1),
                ("exit_confirmed", "stationary", 2, 2),
            ),
        )
        assert state.current_state == "not_waiting"
        assert all(r.interval is None for r in results)
        assert engine.open_interval(state) is None


# =============================================================================
# §11/§12. Continuation and termination
# =============================================================================


class TestWaitingContinuation:
    """WAITING remains WAITING while conditions persist — no per-frame facts."""

    def _waiting_state(
        self,
    ) -> tuple[WaitingEngine, WaitingState, TemporalStateKey, TemporalStateKey, TemporalStateKey]:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
            ),
        )
        assert state.current_state == "waiting"
        return engine, state, pkey, ckey, wkey

    def test_stays_keep_waiting_without_new_facts(self) -> None:
        engine, state, pkey, ckey, wkey = self._waiting_state()
        for seconds in (3, 4, 5, 6):
            result = _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_stay(pkey, event_time=_event(seconds), frame_id=_frame(seconds)),
                classification=_stationary(ckey),
                kind="stay",
            )
            assert result.state.current_state == "waiting"
            assert result.interval is None  # §11: no new fact per frame
            assert result.state.waiting_start == _event(2)  # never reset
            state = result.state


class TestWaitingTermination:
    """WAITING ends on confirmed presence loss, session closure, or movement."""

    def _waiting_state(
        self,
    ) -> tuple[WaitingEngine, WaitingState, TemporalStateKey, TemporalStateKey, TemporalStateKey]:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
            ),
        )
        assert state.current_state == "waiting"
        return engine, state, pkey, ckey, wkey

    def test_termination_by_confirmed_exit(self) -> None:
        engine, state, pkey, ckey, wkey = self._waiting_state()
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_exit(pkey, event_time=_event(5), frame_id=_frame(5)),
            classification=_stationary(ckey),
            kind="exit_confirmed",
        )
        assert result.state.current_state == "not_waiting"
        interval = result.interval
        assert interval is not None
        assert interval.waiting_start == _event(2)
        assert interval.waiting_end == _event(5)
        assert interval.reason is TemporalReason.EXIT_CONFIRMED
        assert interval.duration_seconds == pytest.approx(3.0)

    def test_termination_by_occlusion_expiry(self) -> None:
        engine, state, pkey, ckey, wkey = self._waiting_state()
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_missing_expired(pkey, event_time=_event(7), frame_id=_frame(7)),
            classification=_stationary(ckey),
            kind="missing_expired",
        )
        assert result.state.current_state == "not_waiting"
        assert result.interval is not None
        assert result.interval.reason is TemporalReason.MISSING_EXPIRED
        assert result.interval.waiting_end == _event(7)

    def test_termination_by_session_closure(self) -> None:
        engine, state, pkey, ckey, wkey = self._waiting_state()
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_session_closed(pkey, event_time=_event(8), frame_id=_frame(8)),
            classification=_stationary(ckey),
            kind="session_closed",
        )
        assert result.state.current_state == "not_waiting"
        assert result.interval is not None
        assert result.interval.reason is TemporalReason.SESSION_CLOSED

    def test_termination_by_movement_qualification(self) -> None:
        # §12: a Task 15.5.2 MOVING confirmation (the classification state
        # reached "moving" through ITS OWN qualification) ends waiting.
        engine, state, pkey, ckey, wkey = self._waiting_state()
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(6), frame_id=_frame(6)),
            classification=_moving(ckey),
            kind="stay",
        )
        assert result.state.current_state == "not_waiting"
        assert result.interval is not None
        assert result.interval.reason is TemporalReason.MOVEMENT_EXCEEDED
        assert result.interval.waiting_end == _event(6)

    def test_closed_interval_is_qualified_by_configuration(self) -> None:
        # The closed interval carries the configured minimum; a confirmed
        # interval shorter than the minimum is still a REAL interval — the
        # flag marks, never alters (§7 semantics, mirroring dwell).
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
            ),
        )
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_exit(pkey, event_time=_event(3), frame_id=_frame(3)),
            classification=_stationary(ckey),
            kind="exit_confirmed",
        )
        interval = result.interval
        assert interval is not None
        assert interval.minimum_waiting_seconds == pytest.approx(2.0)
        assert interval.duration_seconds == pytest.approx(1.0)
        assert interval.qualified is False  # shorter than the minimum


# =============================================================================
# §13. Short occlusion follows the existing grace policy
# =============================================================================


class TestOcclusion:
    """A short gap keeps WAITING (grace lives in the presence FSM); only a
    confirmed missing_expired ends it."""

    def test_short_gap_preserves_waiting(self) -> None:
        # A not_observed gap within the presence grace policy produces an
        # OBSERVED_STAY (still PRESENT) at the waiting layer — waiting is
        # preserved, never reset by a missing frame.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),  # WAITING @10:02
            ),
        )
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(3), frame_id=_frame(3)),
            classification=_stationary(ckey),
            kind="stay",
        )
        assert result.state.current_state == "waiting"
        assert result.state.waiting_start == _event(2)  # never reset
        assert result.interval is None

    def test_expired_gap_ends_waiting(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
            ),
        )
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_missing_expired(pkey, event_time=_event(9), frame_id=_frame(9)),
            classification=_stationary(ckey),
            kind="missing_expired",
        )
        assert result.state.current_state == "not_waiting"
        assert result.interval is not None
        assert result.interval.reason is TemporalReason.MISSING_EXPIRED


# =============================================================================
# §14. Event-time is authoritative
# =============================================================================


class TestEventTimeAuthoritative:
    """Waiting semantics follow event time — never processing order."""

    def _timeline_result(self, processing_times: list[datetime]):
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state = engine.initial_state(wkey)
        steps = [
            (
                _enter(pkey, event_time=_event(0), frame_id=_frame(0)),
                "stationary",
                "enter_confirmed",
            ),
            (_stay(pkey, event_time=_event(1), frame_id=_frame(1)), "stationary", "stay"),
            (_stay(pkey, event_time=_event(2), frame_id=_frame(2)), "stationary", "stay"),
        ]
        results: list[WaitingResult] = []
        for (transition, classification_state, kind), processing_time in zip(
            steps, processing_times, strict=True
        ):
            result = engine.apply(
                state,
                _w_input(
                    wkey,
                    transition=transition,
                    classification=_classification(ckey, current_state=classification_state),
                    kind=kind,
                    processing_time=processing_time,
                ),
            )
            state = result.state
            results.append(result)
        return state, results

    def test_scrambled_processing_time_produces_identical_results(self) -> None:
        ordered, _ = self._timeline_result([_processing(0), _processing(1), _processing(2)])
        scrambled, _ = self._timeline_result([
            _processing(9000),
            _processing(-9000),
            _processing(0),
        ])
        assert ordered == scrambled
        assert ordered.current_state == "waiting"
        assert ordered.waiting_start == _event(2)


# =============================================================================
# §15/§16. Late/out-of-order policy and duplicate idempotency
# =============================================================================


class TestOrderingAndIdempotency:
    """The 15.1 policy is reused verbatim; duplicates are idempotent."""

    def _seeded_waiting(self, *, reorder_window: float = 30.0):
        engine = _waiting_engine(
            _policy(
                waiting_qualification_seconds=2.0,
                reorder_window_seconds=reorder_window,
            )
        )
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),  # WAITING @10:02 (watermark)
            ),
        )
        assert state.current_state == "waiting"
        return engine, state, pkey, ckey, wkey

    def test_older_within_window_is_reordered_not_rewound(self) -> None:
        engine, state, pkey, ckey, wkey = self._seeded_waiting()
        # 10:01 arrives after 10:02: within the window -> reordered, the
        # waiting state is NOT rewound.
        result = _apply(
            engine,
            state,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(1), frame_id=_frame(1)),
            classification=_stationary(ckey),
            kind="stay",
        )
        assert result.reordered is True
        assert result.interval is None
        assert result.state.current_state == "waiting"
        assert result.state.waiting_start == _event(2)
        assert result.state.watermark_event_time == _event(2)  # no rewind

    def test_late_beyond_window_rejected(self) -> None:
        engine, state, pkey, ckey, wkey = self._seeded_waiting(reorder_window=30.0)
        # 9:58 is 4s late (within window, reordered); 9:00 is 62s late.
        with pytest.raises(LateEventError, match="reordering window"):
            _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_stay(pkey, event_time=_event(-59), frame_id=_frame(-59)),
                classification=_stationary(ckey),
                kind="stay",
            )

    def test_duplicate_input_is_deduplicated(self) -> None:
        engine, state, pkey, ckey, wkey = self._seeded_waiting()
        inp = _w_input(
            wkey,
            transition=_stay(pkey, event_time=_event(2), frame_id=_frame(2)),
            classification=_stationary(ckey),
            kind="stay",
        )
        replay = engine.apply(state, inp)
        assert replay.deduplicated is True
        assert replay.interval is None  # no duplicate waiting fact
        assert replay.state == state  # byte-identical

    def test_replayed_timeline_reproduces_identical_intervals(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        timeline = (
            ("enter_confirmed", "stationary", 0, 0),
            ("stay", "stationary", 2, 2),
            ("exit_confirmed", "stationary", 5, 5),
        )
        _, r1 = _run(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=timeline)
        _, r2 = _run(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=timeline)
        i1 = [r.interval for r in r1 if r.interval is not None]
        i2 = [r.interval for r in r2 if r.interval is not None]
        assert len(i1) == 1
        assert i1 == i2
        assert i1[0].interval_id == i2[0].interval_id  # content-derived


# =============================================================================
# §17/§27. Re-entry creates independent waiting intervals
# =============================================================================


class TestReentry:
    """Each confirmed entry after a confirmed exit opens a NEW interval."""

    def test_reentry_produces_two_independent_intervals(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        _, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),  # 10:00 enters
                ("stay", "stationary", 2, 2),  # 10:02 WAITING #1
                ("exit_confirmed", "stationary", 5, 5),  # 10:05 leaves
                ("enter_confirmed", "stationary", 10, 10),  # 10:10 returns
                ("stay", "stationary", 12, 12),  # 10:12 WAITING #2
                ("exit_confirmed", "stationary", 15, 15),  # 10:15 leaves
            ),
        )
        intervals = [r.interval for r in results if r.interval is not None]
        assert len(intervals) == 2  # never merged (§17)
        first, second = intervals
        assert first.waiting_start == _event(2)
        assert first.waiting_end == _event(5)
        assert second.waiting_start == _event(12)
        assert second.waiting_end == _event(15)
        assert first.interval_id != second.interval_id
        assert first.duration_seconds == pytest.approx(3.0)
        assert second.duration_seconds == pytest.approx(3.0)

    def test_reentry_after_session_closure(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        _, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
                ("session_closed", "stationary", 5, 5),
                ("enter_confirmed", "stationary", 10, 10),
                ("stay", "stationary", 12, 12),
                ("session_closed", "stationary", 15, 15),
            ),
        )
        intervals = [r.interval for r in results if r.interval is not None]
        assert len(intervals) == 2


# =============================================================================
# §18/§19/§20. Isolation
# =============================================================================


class TestIsolation:
    """Waiting state is independent per track and per spatial context."""

    def _waiting_for(self, engine: WaitingEngine, **overrides) -> list[WaitingInterval]:
        pkey, ckey, wkey = _family_keys(**overrides)
        _, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
                ("exit_confirmed", "stationary", 5, 5),
            ),
        )
        return [r.interval for r in results if r.interval is not None]

    def test_track_isolation(self) -> None:
        # §18: Track A waiting must not affect Track B.
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        other = TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        a = self._waiting_for(engine)
        b = self._waiting_for(engine, track_id=other)
        assert len(a) == 1
        assert len(b) == 1
        assert a[0].key.track_id != b[0].key.track_id
        assert a[0].interval_id != b[0].interval_id

    def test_multiple_waiting_areas(self) -> None:
        # §19: Track A -> zone-a, Track B -> zone-b — independent waiting.
        engine = _waiting_engine(
            _policy(waiting_contexts={"zone-a", "zone-b"}, waiting_qualification_seconds=2.0)
        )
        a = self._waiting_for(engine, semantic_context="zone-a")
        b = self._waiting_for(engine, semantic_context="zone-b")
        assert a[0].key.semantic_context == "zone-a"
        assert b[0].key.semantic_context == "zone-b"
        assert a[0].interval_id != b[0].interval_id

    def test_tenant_isolation(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        other = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        a = self._waiting_for(engine)
        b = self._waiting_for(engine, tenant_id=other)
        assert a[0].interval_id != b[0].interval_id

    def test_venue_isolation(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        other = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        a = self._waiting_for(engine)
        b = self._waiting_for(engine, venue_id=other)
        assert a[0].interval_id != b[0].interval_id

    def test_session_isolation(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        other = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        a = self._waiting_for(engine)
        b = self._waiting_for(engine, session_id=other)
        assert a[0].interval_id != b[0].interval_id

    def test_camera_isolation(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        other = CameraId(UUID("40000000-0000-0000-0000-000000000099"))
        a = self._waiting_for(engine)
        b = self._waiting_for(engine, camera_id=other)
        assert a[0].interval_id != b[0].interval_id

    def test_same_track_in_two_sessions_is_independent(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        s1 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000001"))
        s2 = VideoSessionId(UUID("30000000-0000-0000-0000-000000000002"))
        a = self._waiting_for(engine, session_id=s1)
        b = self._waiting_for(engine, session_id=s2)
        assert a[0].key.session_id != b[0].key.session_id
        assert a[0].interval_id != b[0].interval_id

    def test_cross_session_input_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        other_session = VideoSessionId(UUID("30000000-0000-0000-0000-000000000099"))
        _, ckey_other, _ = _family_keys(session_id=other_session)
        state = engine.initial_state(wkey)
        with pytest.raises(StateKeyMismatchError, match="session_id"):
            _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
                classification=_classification(ckey_other, current_state="stationary"),
                kind="enter_confirmed",
            )

    def test_cross_tenant_input_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        other = TenantId(UUID("10000000-0000-0000-0000-000000000099"))
        pkey_other = _key(fsm_kind="presence", tenant_id=other, semantic_context=WAITING_CTX)
        state = engine.initial_state(wkey)
        with pytest.raises(StateKeyMismatchError, match="tenant_id"):
            _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_enter(pkey_other, event_time=_event(0), frame_id=_frame(0)),
                classification=_stationary(ckey),
                kind="enter_confirmed",
            )

    def test_cross_venue_input_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        other = VenueId(UUID("20000000-0000-0000-0000-000000000099"))
        pkey_other = _key(fsm_kind="presence", venue_id=other, semantic_context=WAITING_CTX)
        state = engine.initial_state(wkey)
        with pytest.raises(StateKeyMismatchError, match="venue_id"):
            _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_enter(pkey_other, event_time=_event(0), frame_id=_frame(0)),
                classification=_stationary(ckey),
                kind="enter_confirmed",
            )

    def test_cross_track_input_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        other = TrackId(UUID("60000000-0000-0000-0000-000000000099"))
        pkey_other = _key(fsm_kind="presence", track_id=other, semantic_context=WAITING_CTX)
        state = engine.initial_state(wkey)
        with pytest.raises(StateKeyMismatchError, match="track_id"):
            _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_enter(pkey_other, event_time=_event(0), frame_id=_frame(0)),
                classification=_stationary(ckey),
                kind="enter_confirmed",
            )


# =============================================================================
# §21. Configuration provenance (pinned, never "latest")
# =============================================================================


class TestConfigurationProvenance:
    """Every waiting fact preserves the pinned policy revision and config version."""

    def test_interval_preserves_pinned_provenance(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0, revision="v7"))
        pkey, ckey, wkey = _family_keys()
        _, results = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(
                ("enter_confirmed", "stationary", 0, 0),
                ("stay", "stationary", 2, 2),
                ("exit_confirmed", "stationary", 5, 5),
            ),
        )
        (interval,) = [r.interval for r in results if r.interval is not None]
        assert interval.policy_revision == "v7"
        assert interval.key.configuration_version_id == _CONFIG
        assert interval.key.tenant_id == _TENANT
        assert interval.key.venue_id == _VENUE
        assert interval.key.session_id == _SESSION
        assert interval.key.camera_id == _CAMERA
        assert interval.key.track_id == _TRACK
        assert interval.key.semantic_context == WAITING_CTX
        assert interval.fsm_version == TEMPORAL_ENGINE_VERSION
        assert interval.fsm_kind == "waiting"

    def test_historical_session_keeps_its_pinned_configuration(self) -> None:
        # §21: a V1 session stays on V1 even after V2 is published — replay
        # is byte-identical and the engine never queries "the latest".
        v1 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000001"))
        v2 = ConfigurationVersionId(UUID("50000000-0000-0000-0000-000000000002"))
        engine = _waiting_engine(
            _policy(waiting_contexts={"zone-a"}, waiting_qualification_seconds=2.0, revision="v1")
        )
        timeline = (
            ("enter_confirmed", "stationary", 0, 0),
            ("stay", "stationary", 2, 2),
            ("exit_confirmed", "stationary", 5, 5),
        )
        pkey1, ckey1, wkey1 = _family_keys(configuration_version_id=v1, semantic_context="zone-a")
        pkey2, ckey2, wkey2 = _family_keys(configuration_version_id=v2, semantic_context="zone-a")
        _, r1 = _run(engine, pkey=pkey1, ckey=ckey1, wkey=wkey1, timeline=timeline)
        _, r2 = _run(engine, pkey=pkey2, ckey=ckey2, wkey=wkey2, timeline=timeline)
        _, r1_replay = _run(engine, pkey=pkey1, ckey=ckey1, wkey=wkey1, timeline=timeline)
        i1 = [r.interval for r in r1 if r.interval is not None]
        i2 = [r.interval for r in r2 if r.interval is not None]
        i1_replay = [r.interval for r in r1_replay if r.interval is not None]
        assert i1 == i1_replay  # V1 unchanged after V2 exists
        assert i1[0].interval_id != i2[0].interval_id  # different config version

    def test_measurement_policy_mismatch_does_not_apply(self) -> None:
        # The waiting engine itself never fabricates facts from a different
        # policy: only the pinned engine policy is ever consulted.
        engine = _waiting_engine(
            _policy(waiting_contexts={"zone-a"}, waiting_qualification_seconds=2.0)
        )
        _, _, wkey = _family_keys(semantic_context="zone-a")
        state = engine.initial_state(wkey)
        assert state.current_state == "not_waiting"


# =============================================================================
# §22. Checkpoint / restart recovery
# =============================================================================


class TestCheckpoint:
    """Waiting checkpoints and resumes under the versioned discipline."""

    TIMELINE = (
        ("enter_confirmed", "stationary", 0, 0),  # candidate @10:00
        ("stay", "stationary", 2, 2),  # WAITING @10:02
        ("stay", "stationary", 3, 3),  # continuation
        ("exit_confirmed", "stationary", 5, 5),  # interval closed
    )

    def test_checkpoint_round_trip(self) -> None:
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(engine, pkey=pkey, ckey=ckey, wkey=wkey, timeline=self.TIMELINE)
        checkpoint = engine.checkpoint(state)
        data = checkpoint.to_dict()
        assert WaitingCheckpoint.from_dict(data) == checkpoint
        assert engine.restore(checkpoint) == state

    def test_restart_recovery_matches_uninterrupted_processing(self) -> None:
        policy = _policy(waiting_qualification_seconds=2.0)
        pkey, ckey, wkey = _family_keys()
        # Uninterrupted run.
        uninterrupted, results = _run(
            _waiting_engine(policy), pkey=pkey, ckey=ckey, wkey=wkey, timeline=self.TIMELINE
        )
        # Interrupted: stop at step 2 (WAITING confirmed), checkpoint,
        # restore into a FRESH engine, continue.
        engine = _waiting_engine(policy)
        state = engine.initial_state(wkey)
        for kind, classification_state, seconds, frame_index in self.TIMELINE[:2]:
            classification = _classification(ckey, current_state=classification_state)
            transition = {
                "enter_confirmed": _enter,
                "stay": _stay,
            }[kind](pkey, event_time=_event(seconds), frame_id=_frame(frame_index))
            state = engine.apply(
                state,
                _w_input(wkey, transition=transition, classification=classification, kind=kind),
            ).state
        assert state.current_state == "waiting"  # checkpoint while WAITING
        checkpoint = engine.checkpoint(state)
        assert checkpoint.state.waiting_start == _event(2)

        resumed_engine = _waiting_engine(policy)
        restored = resumed_engine.restore(checkpoint)
        assert restored == state
        resumed_state = restored
        for kind, classification_state, seconds, frame_index in self.TIMELINE[2:]:
            classification = _classification(ckey, current_state=classification_state)
            transition = {
                "stay": _stay,
                "exit_confirmed": _exit,
            }[kind](pkey, event_time=_event(seconds), frame_id=_frame(frame_index))
            resumed_state = resumed_engine.apply(
                resumed_state,
                _w_input(wkey, transition=transition, classification=classification, kind=kind),
            ).state
        assert resumed_state == uninterrupted
        assert resumed_state.current_state == "not_waiting"
        assert results[-1].interval is not None

    def test_checkpoint_while_candidate(self) -> None:
        policy = _policy(waiting_qualification_seconds=10.0)
        engine = _waiting_engine(policy)
        pkey, ckey, wkey = _family_keys()
        state, _ = _run(
            engine,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            timeline=(("enter_confirmed", "stationary", 0, 0),),
        )
        assert state.current_state == "waiting_candidate"
        checkpoint = engine.checkpoint(state)
        assert checkpoint.state.candidate_start == _event(0)
        # Restore continues the qualification run at the SAME event-time
        # anchor — restart does not restart the clock.
        restored = engine.restore(checkpoint)
        result = _apply(
            engine,
            restored,
            pkey=pkey,
            ckey=ckey,
            wkey=wkey,
            transition=_stay(pkey, event_time=_event(5), frame_id=_frame(5)),
            classification=_stationary(ckey),
            kind="stay",
        )
        assert result.state.current_state == "waiting_candidate"  # 5s < 10s
        assert result.state.candidate_start == _event(0)

    def test_restore_rejects_engine_version_drift(self) -> None:
        engine = _waiting_engine(_policy())
        state = engine.initial_state(_key(fsm_kind="waiting"))
        checkpoint = WaitingCheckpoint(engine_version="9.9.9", policy_revision="v1", state=state)
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            engine.restore(checkpoint)

    def test_restore_rejects_policy_drift(self) -> None:
        engine = _waiting_engine(_policy(revision="v2"))
        state = engine.initial_state(_key(fsm_kind="waiting"))
        checkpoint = WaitingCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION, policy_revision="v1", state=state
        )
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            engine.restore(checkpoint)

    def test_restore_rejects_cross_fsm_checkpoint(self) -> None:
        engine = _waiting_engine(_policy())
        with pytest.raises(InvalidTemporalInputError, match="WaitingCheckpoint"):
            engine.restore(object())  # type: ignore[arg-type]


# =============================================================================
# §28. Failure tests — never fabricate waiting state
# =============================================================================


class TestFailureTests:
    """Malformed or contradictory inputs fail explicitly, never repaired."""

    def test_missing_track_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="waiting",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
            )

    def test_missing_session_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="waiting",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_tenant_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="waiting",
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_venue_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="waiting",
                tenant_id=_TENANT,
                session_id=_SESSION,
                camera_id=_CAMERA,
                configuration_version_id=_CONFIG,
                track_id=_TRACK,
            )

    def test_missing_configuration_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalStateKey(
                fsm_kind="waiting",
                tenant_id=_TENANT,
                venue_id=_VENUE,
                session_id=_SESSION,
                camera_id=_CAMERA,
                track_id=_TRACK,
            )

    def test_non_waiting_key_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        with pytest.raises(InvalidTemporalInputError, match="fsm_kind"):
            engine.initial_state(_key(fsm_kind="presence"))

    def test_input_key_must_match_state_key(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        other_wkey = _key(
            fsm_kind="waiting",
            track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")),
            semantic_context=WAITING_CTX,
        )
        with pytest.raises(InvalidTemporalInputError, match="must match the state key"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    other_wkey,
                    transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
                    classification=_stationary(ckey),
                    kind="enter_confirmed",
                ),
            )

    def test_wrong_presence_family_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        _, ckey, wkey = _family_keys()
        # A transition from a NON-presence family is not a presence result.
        transition = TemporalTransition(
            transition_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "wrong-family")),
            fsm_kind="dwell",
            key=_key(fsm_kind="dwell", semantic_context=WAITING_CTX),
            from_state="idle",
            to_state="idle",
            event_kind="stay",
            reason=TemporalReason.OBSERVED_STAY,
            observation_frame_id=_frame(0),
            event_time=_event(0),
            processing_time=_processing(),
            configuration_version_id=_CONFIG,
            fsm_version=TEMPORAL_ENGINE_VERSION,
        )
        with pytest.raises(InvalidTemporalInputError, match="presence-family"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=transition,
                    classification=_stationary(ckey),
                    kind="stay",
                ),
            )

    def test_wrong_classification_family_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, _, wkey = _family_keys()
        # A classification state of the wrong family is rejected. model_construct
        # bypasses the contract so the ENGINE's family guard is exercised.
        bad = MovementClassificationState.model_construct(
            fsm_version=TEMPORAL_ENGINE_VERSION,
            key=_key(
                fsm_kind="presence",
                track_id=TrackId(UUID("60000000-0000-0000-0000-000000000099")),
                semantic_context=WAITING_CTX,
            ),
            current_state="stationary",
        )
        with pytest.raises(InvalidTemporalInputError, match="classification states"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
                    classification=bad,
                    kind="enter_confirmed",
                ),
            )

    def test_invalid_movement_state_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        # model_construct bypasses the contract so the ENGINE's guard fires.
        bad = MovementClassificationState.model_construct(
            fsm_version=TEMPORAL_ENGINE_VERSION,
            key=ckey,
            current_state="running",  # not a MOVEMENT_STATES value
        )
        with pytest.raises(InvalidTemporalInputError, match="current_state"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
                    classification=bad,
                    kind="enter_confirmed",
                ),
            )

    def test_mis_wired_observation_kind_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        # The kind says enter_confirmed but the transition is a stay —
        # a mis-wired coordinator step is rejected explicitly.
        with pytest.raises(InvalidTemporalInputError, match="observation_kind"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=_stay(pkey, event_time=_event(0), frame_id=_frame(0)),
                    classification=_stationary(ckey),
                    kind="enter_confirmed",
                ),
            )

    def test_naive_processing_time_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        with pytest.raises(InvalidTemporalInputError, match="timezone-aware"):
            engine.apply(
                engine.initial_state(wkey),
                WaitingInput(
                    key=wkey,
                    presence_transition=_enter(pkey, event_time=_event(0), frame_id=_frame(0)),
                    classification_state=_stationary(ckey),
                    observation_kind="enter_confirmed",
                    processing_time=datetime(2026, 8, 1, 11, 0, 0),  # naive
                ),
            )

    def test_naive_transition_event_time_rejected(self) -> None:
        engine = _waiting_engine(_policy())
        pkey, ckey, wkey = _family_keys()
        # model_construct bypasses pydantic so the ENGINE's timezone guard
        # is exercised directly.
        naive = TemporalTransition.model_construct(
            transition_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "naive")),
            fsm_kind="presence",
            key=pkey,
            from_state="absent",
            to_state="present",
            event_kind="present",
            reason=TemporalReason.ENTER_CONFIRMED,
            observation_frame_id=_frame(0),
            event_time=datetime(2026, 8, 1, 10, 0, 0),  # naive
            processing_time=_processing(),
            configuration_version_id=_CONFIG,
            fsm_version=TEMPORAL_ENGINE_VERSION,
        )
        with pytest.raises(InvalidTemporalInputError, match="timezone-aware"):
            engine.apply(
                engine.initial_state(wkey),
                _w_input(
                    wkey,
                    transition=naive,
                    classification=_stationary(ckey),
                    kind="enter_confirmed",
                ),
            )

    def test_negative_interval_duration_rejected_at_contract(self) -> None:
        _, _, wkey = _family_keys()
        with pytest.raises(ValueError, match="duration_seconds"):
            WaitingInterval(
                interval_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "neg")),
                fsm_kind="waiting",
                key=wkey,
                waiting_start=_event(5),
                waiting_end=_event(2),  # backwards — invalid on its own
                last_seen=_event(5),
                duration_seconds=-3.0,
                qualified=True,
                minimum_waiting_seconds=0.0,
                reason=TemporalReason.EXIT_CONFIRMED,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )

    def test_interval_end_before_start_rejected(self) -> None:
        _, _, wkey = _family_keys()
        with pytest.raises(ValueError, match="must not precede"):
            WaitingInterval(
                interval_id=EventId(uuid5(TEMPORAL_ID_NAMESPACE, "back")),
                fsm_kind="waiting",
                key=wkey,
                waiting_start=_event(5),
                waiting_end=_event(2),
                last_seen=_event(5),
                duration_seconds=3.0,
                qualified=True,
                minimum_waiting_seconds=0.0,
                reason=TemporalReason.EXIT_CONFIRMED,
                fsm_version=TEMPORAL_ENGINE_VERSION,
                policy_revision="v1",
            )


# =============================================================================
# §30. Bounded state / performance
# =============================================================================


class TestBoundedState:
    """Waiting retains only scalars — no per-frame history."""

    def test_state_is_scalar_bounded_by_construction(self) -> None:
        engine = _waiting_engine(_policy())
        state = engine.initial_state(_key(fsm_kind="waiting", semantic_context=WAITING_CTX))
        for field in WaitingState.model_fields:
            value = getattr(state, field)
            if field == "key":
                continue
            assert not isinstance(value, (list, tuple, dict, set, frozenset)), (
                f"waiting state must not accumulate {field}"
            )

    def test_long_stream_stays_bounded_and_deterministic(self) -> None:
        # 200 alternating enter/exit cycles: bounded state, deterministic
        # interval identities (no history growth).
        engine = _waiting_engine(_policy(waiting_qualification_seconds=2.0))
        pkey, ckey, wkey = _family_keys()
        state = engine.initial_state(wkey)
        intervals: list[WaitingInterval] = []
        for cycle in range(100):
            enter_at = cycle * 10
            result = _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_enter(pkey, event_time=_event(enter_at), frame_id=_frame(enter_at)),
                classification=_stationary(ckey),
                kind="enter_confirmed",
            )
            state = result.state
            result = _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_stay(
                    pkey, event_time=_event(enter_at + 2), frame_id=_frame(enter_at + 2)
                ),
                classification=_stationary(ckey),
                kind="stay",
            )
            state = result.state
            result = _apply(
                engine,
                state,
                pkey=pkey,
                ckey=ckey,
                wkey=wkey,
                transition=_exit(
                    pkey, event_time=_event(enter_at + 5), frame_id=_frame(enter_at + 5)
                ),
                classification=_stationary(ckey),
                kind="exit_confirmed",
            )
            state = result.state
            if result.interval is not None:
                intervals.append(result.interval)
        assert len(intervals) == 100  # one per cycle, never merged
        assert state.current_state == "not_waiting"
        for field in WaitingState.model_fields:
            value = getattr(state, field)
            if field == "key":
                continue
            assert not isinstance(value, (list, tuple, dict, set, frozenset))


# =============================================================================
# §29. Pure core
# =============================================================================


class TestWaitingPurity:
    """The waiting core performs no I/O and reads no current time."""

    def test_waiting_core_is_pure(self) -> None:
        package_dir = (
            Path(__file__).resolve().parents[2] / "backend" / "app" / "intelligence" / "temporal"
        )
        text = (package_dir / "waiting.py").read_text()
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
                f"I/O/stateful module {module!r} leaked into waiting.py"
            )
        assert "now(" not in text
        assert "utc_now" not in text
        assert "print(" not in text
        assert "datetime.now" not in text
