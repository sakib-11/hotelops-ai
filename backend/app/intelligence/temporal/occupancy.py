"""Occupancy FSM — deterministic occupancy intelligence (Task 15.4).

Answers \\"how many UNIQUE entities are confirmed PRESENT in a defined
spatial context at event-time?\\" built entirely on the confirmed
transitions of the Task 15.2 Enter/Exit FSM. Architecture (Task 15.4):

    SpatialObservation
        ↓ Task 15.1 Temporal Foundation — event-time ordering, watermark,
          late/out-of-order policy, idempotent dedup, checkpoint/restore
    Enter/Exit FSM (Task 15.2, ``PresenceTemporalEngine``)
        ↓ presence TemporalTransition
    OCCUPANCY FSM — this module (aggregates across entities)
        ↓
    OccupancySnapshot (fact)

Occupancy is an AGGREGATE: it counts unique entities, so its state is
scoped by the occupancy key (tenant + venue + session + camera +
configuration version + semantic context) rather than per-track. The
scope key is derived from a presence key by ``occupancy_scope_key`` —
the track component is replaced by the canonical aggregate sentinel
(``OCCUPANCY_SCOPE_TRACK``), so every entity in the scope aggregates
into the SAME state and the occupancy identity never depends on which
track derived it.

Semantics (all threshold-free — confirmation is decided by the presence
FSM, never re-derived here):

  - Only CONFIRMED presence counts. ``occupancy_event_from_presence``
    maps ENTER_CONFIRMED -> entity enter, EXIT_CONFIRMED /
    MISSING_EXPIRED / session closure -> entity exit, and everything
    else (stays, deduplicated, reordered) -> ``stay`` (no effect).
    ENTERING/EXITING intermediate states never reach this engine because
    the presence FSM does not confirm them — unconfirmed entry never
    increments occupancy and unconfirmed absence never decrements it
    (§5/§6/§7).
  - An entity is counted at most once per scope: the state holds the set
    of occupied tracks; ``occupancy_count`` is DERIVED from the set, so
    it can never go negative and can never drift from the identities
    (§4/§26 invariant 2).
  - Every change has an explicit source: each emitted snapshot carries
    ``previous_count`` + ``delta`` = ``occupancy_count`` and the
    ``source_transition_id`` of the presence transition that caused it
    (§17).
  - Duplicate processing is idempotent: per-track last-applied positions
    (the same content-derived-position principle as the foundation,
    Task 7 — reused, not re-architected) deduplicate a replayed
    transition before it can change the set (§11/§22).
  - Short occlusion never corrupts occupancy: a gap covered by the
    presence FSM's grace/occlusion tolerance produces only ``stay``
    transitions here, so the entity stays counted (§8/§21).
  - Event-time is authoritative and the 15.1 policy is reused verbatim:
    an event older than the aggregate watermark within the configured
    ``reorder_window_seconds`` is accepted as a REORDERED fact and — in
    exact agreement with the foundation's documented
    \\"accept-with-no-rewind\\" policy — does NOT change the entity set
    (the watermark and positions never move backward; the same reorder
    is reproduced deterministically on replay). Events beyond the window
    raise ``LateEventError`` (§9/§10). A replay-based reorder that
    reinterprets the set is future work and is never silent.
  - Invariant enforcement: an exit for an entity that was never counted
    (or a second entry for an already-counted entity) is an explicit
    ``InvalidTransitionError`` from the per-entity ``OCCUPANCY_FSM`` —
    never clamped, never silently repaired (§6/§25). This includes an
    entity whose ENTER was reordered-away within the window (never
    counted by design): its later in-order EXIT is rejected the same
    way — the typed rejection, not a silent skip, is the invariant
    enforcement. A replay-based reorder that would reinterpret such a
    sequence is future work.
  - Track/session/camera/tenant/venue/configuration isolation: the scope
    components of the presence transition's key MUST match the occupancy
    scope key or the engine raises ``StateKeyMismatchError`` (§13/§14/
    §15). Cross-camera venue-wide identity fusion is explicitly NOT
    implemented here (Task 15.4 §14: document, never guess).

The engine is PURE and DETERMINISTIC: no PostgreSQL, Redis, S3, HTTP,
FastAPI, or LLM calls, no current-time reads, and no fallback to \\"the
latest configuration\\" — the scope key carries the pinned configuration
version. Persistence is a separate boundary over
``OccupancyCheckpoint``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid5

from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    LateEventError,
    StateKeyMismatchError,
)
from backend.app.intelligence.temporal.fsm import DeterministicFsm, FsmRule
from contracts.common import EventId, FrameId, TrackId
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TEMPORAL_ID_NAMESPACE,
    OccupancyCheckpoint,
    OccupancySnapshot,
    OccupancyState,
    TemporalPolicy,
    TemporalReason,
    TemporalStateKey,
    TemporalTransition,
)

__all__ = [
    "OCCUPANCY_FSM",
    "OCCUPANCY_SCOPE_TRACK",
    "OccupancyEngine",
    "OccupancyInput",
    "OccupancyResult",
    "occupancy_event_from_presence",
    "occupancy_scope_key",
]

# Canonical aggregate track marker for occupancy scope keys. Real track
# IDs are session-scoped UUIDs; this deterministic sentinel (derived from
# the temporal ID namespace) is the single value every occupancy scope
# key uses in the track component, so all entities in a scope aggregate
# into the SAME state.
OCCUPANCY_SCOPE_TRACK = TrackId(uuid5(TEMPORAL_ID_NAMESPACE, "occupancy-scope-aggregate"))

# Per-entity legal transitions inside the aggregate. The engine decides
# the entity's current state (idle = not counted, occupied = counted)
# from the set and asks the FSM whether the confirmed presence event is
# legal: a second entry for a counted entity, or an exit for one never
# counted, is rejected explicitly — never silently repaired.
OCCUPANCY_FSM = DeterministicFsm(
    name="occupancy",
    version=TEMPORAL_ENGINE_VERSION,
    states=("idle", "occupied"),
    initial_state="idle",
    rules=(
        FsmRule(from_state="idle", event="enter_confirmed", to_state="occupied"),
        FsmRule(from_state="occupied", event="exit_confirmed", to_state="idle"),
        FsmRule(from_state="occupied", event="missing_expired", to_state="idle"),
        FsmRule(from_state="occupied", event="session_closed", to_state="idle"),
        FsmRule(from_state="idle", event="session_closed", to_state="idle"),
        FsmRule(from_state="idle", event="stay", to_state="idle"),
        FsmRule(from_state="occupied", event="stay", to_state="occupied"),
    ),
)


def occupancy_event_from_presence(transition: TemporalTransition) -> str:
    """Map one presence transition to its occupancy event kind.

    This is the ONLY sanctioned presence -> occupancy mapping
    (deterministic):
      - ENTER_CONFIRMED   -> ``enter_confirmed``  (entity counted)
      - EXIT_CONFIRMED    -> ``exit_confirmed``   (entity removed)
      - MISSING_EXPIRED   -> ``missing_expired``  (entity removed)
      - SESSION_CLOSED    -> ``session_closed``   (entity removed if counted,
        benign no-op otherwise — the session is finalizing)
      - OBSERVED_STAY / DEDUPLICATED / REORDERED -> ``stay`` (never a
        count change — occupancy reacts only to CONFIRMED transitions).
    """
    if transition.reason is TemporalReason.ENTER_CONFIRMED:
        return "enter_confirmed"
    if transition.reason is TemporalReason.EXIT_CONFIRMED:
        return "exit_confirmed"
    if transition.reason is TemporalReason.MISSING_EXPIRED:
        return "missing_expired"
    if transition.reason is TemporalReason.SESSION_CLOSED:
        return "session_closed"
    return "stay"


def occupancy_scope_key(presence_key: TemporalStateKey) -> TemporalStateKey:
    """Derive the canonical occupancy scope key from a presence key.

    Preserves every scope component (tenant/venue/session/camera/
    configuration version/spatial context) and replaces only the track
    with ``OCCUPANCY_SCOPE_TRACK`` and the FSM family with
    ``\"occupancy\"`` — so all tracks in a scope share ONE occupancy key.
    """
    return presence_key.model_copy(
        update={"fsm_kind": "occupancy", "track_id": OCCUPANCY_SCOPE_TRACK}
    )


@dataclass(frozen=True, slots=True)
class OccupancyInput:
    """Pure-engine input: the occupancy scope key + one presence transition.

    ``observation_kind`` is the occupancy event derived via
    ``occupancy_event_from_presence`` (re-validated against the
    transition's reason, so a mis-wired kind is rejected explicitly).
    ``processing_time`` is metadata only — ordering ALWAYS uses the
    transition's event-time position.
    """

    key: TemporalStateKey
    transition: TemporalTransition
    observation_kind: str
    processing_time: datetime


@dataclass(frozen=True, slots=True)
class OccupancyResult:
    """Deterministic result of applying one presence transition."""

    state: OccupancyState
    # One OccupancySnapshot whenever the entity set changed (a confirmed
    # enter/exit was applied); None for stays, deduplicated, and
    # reordered inputs.
    snapshot: OccupancySnapshot | None = None
    deduplicated: bool = False
    reordered: bool = False


class OccupancyEngine:
    """Pure occupancy aggregator over confirmed presence transitions.

    A standalone deterministic engine (the aggregate state shape differs
    fundamentally from the per-entity ``TemporalState``, so it is not a
    ``TemporalEngine`` subclass) that REUSES the foundation's discipline
    wholesale: the same ``TemporalPolicy`` (reorder window, revision),
    the same watermark + per-track position dedup, the same typed error
    taxonomy, the same versioned ``OccupancyCheckpoint``, and the same
    content-derived fact identities.
    """

    OBSERVATION_KINDS: tuple[str, ...] = (
        "enter_confirmed",
        "exit_confirmed",
        "missing_expired",
        "stay",
        "session_closed",
    )

    def __init__(
        self,
        *,
        fsm: DeterministicFsm = OCCUPANCY_FSM,
        policy: TemporalPolicy,
    ) -> None:
        self._fsm = fsm
        self._policy = policy

    @property
    def fsm(self) -> DeterministicFsm:
        return self._fsm

    @property
    def policy(self) -> TemporalPolicy:
        return self._policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initial_state(self, key: TemporalStateKey) -> OccupancyState:
        """The pristine aggregate state for an occupancy scope key."""
        if not isinstance(key, TemporalStateKey):
            raise InvalidTemporalInputError(
                f"key must be a TemporalStateKey, got {type(key).__name__}"
            )
        if key.fsm_kind != "occupancy":
            raise InvalidTemporalInputError(
                f"occupancy state key fsm_kind must be 'occupancy', got {key.fsm_kind!r}"
            )
        if key.track_id != OCCUPANCY_SCOPE_TRACK:
            raise InvalidTemporalInputError(
                "occupancy scope keys must use the canonical aggregate track "
                "sentinel (derive the key with occupancy_scope_key)"
            )
        return OccupancyState(fsm_version=self._fsm.version, key=key)

    def apply(self, state: OccupancyState, inp: OccupancyInput) -> OccupancyResult:
        """Apply one confirmed presence transition (pure, deterministic).

        Raises the typed ``TemporalError`` taxonomy on any failure; a
        failure is never encoded as a state or a snapshot.
        """
        self._validate(state, inp)
        transition = inp.transition
        kind = inp.observation_kind
        event_time = transition.event_time
        frame_id = transition.observation_frame_id
        track = transition.key.track_id
        position = (event_time, frame_id)

        # Idempotency: the same transition for the same track applied
        # again is a duplicate — never a second count change.
        last_position = self._position_for(state, track)
        if last_position is not None and position == last_position:
            return OccupancyResult(state=state, deduplicated=True)

        # Event-time ordering: the aggregate watermark is the max applied
        # position across all tracks. Equal positions across DIFFERENT
        # tracks are distinct facts (dedup is per-track above).
        watermark = (state.watermark_event_time, state.last_applied_frame_id)
        if watermark[0] is not None and position < watermark:
            assert state.watermark_event_time is not None
            delta = (state.watermark_event_time - event_time).total_seconds()
            if delta <= self._policy.reorder_window_seconds:
                # Within the allowed reordering window: the 15.1
                # accept-with-no-rewind policy — acknowledged
                # deterministically, the entity set is NOT changed, and
                # the watermark/positions never move backward.
                return OccupancyResult(state=state, reordered=True)
            raise LateEventError(
                f"presence transition event_time {event_time.isoformat()} is "
                f"{delta:.3f}s older than the occupancy watermark "
                f"{state.watermark_event_time.isoformat()}, beyond the reordering "
                f"window of {self._policy.reorder_window_seconds}s — rejected "
                "deterministically (never silently discarded or force-ordered)"
            )

        # In-order: apply the entity-set semantics through the per-entity
        # FSM (illegal transitions — e.g. exit for a never-counted
        # entity — are rejected explicitly, never clamped).
        entity_state = "occupied" if track in state.occupied_tracks else "idle"
        next_entity_state = self._fsm.transition(entity_state, kind)
        previous_count = state.occupancy_count
        occupied = state.occupied_tracks
        if next_entity_state == "occupied":
            occupied = occupied | {track}
        elif next_entity_state == "idle" and track in occupied:
            occupied = occupied - {track}

        positions = dict(state.entity_positions)
        positions[track] = (event_time, frame_id)
        new_watermark = position if watermark[0] is None or position > watermark else watermark
        updated = state.model_copy(
            update={
                "occupied_tracks": occupied,
                "entity_positions": positions,
                "watermark_event_time": new_watermark[0],
                "last_applied_frame_id": new_watermark[1],
            }
        )

        snapshot = None
        if occupied != state.occupied_tracks:
            snapshot = self._build_snapshot(
                state=updated,
                previous_count=previous_count,
                event_time=event_time,
                source_transition=transition,
            )
        return OccupancyResult(state=updated, snapshot=snapshot)

    def checkpoint(self, state: OccupancyState) -> OccupancyCheckpoint:
        """Serialize ``state`` into a versioned, resumable checkpoint."""
        return OccupancyCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision=self._policy.revision,
            state=state,
        )

    def restore(self, checkpoint: OccupancyCheckpoint) -> OccupancyState:
        """Restore a checkpoint, rejecting version/policy drift (typed)."""
        if not isinstance(checkpoint, OccupancyCheckpoint):
            raise InvalidTemporalInputError(
                f"checkpoint must be an OccupancyCheckpoint, got {type(checkpoint).__name__}"
            )
        if checkpoint.engine_version != TEMPORAL_ENGINE_VERSION:
            raise FsmVersionMismatchError(
                f"checkpoint engine version {checkpoint.engine_version!r} does not "
                f"match the engine version {TEMPORAL_ENGINE_VERSION!r}"
            )
        if checkpoint.policy_revision != self._policy.revision:
            raise CheckpointIntegrityError(
                f"checkpoint policy revision {checkpoint.policy_revision!r} does not "
                f"match the engine policy revision {self._policy.revision!r}"
            )
        if checkpoint.state.fsm_version != self._fsm.version:
            raise FsmVersionMismatchError(
                f"checkpoint state FSM version {checkpoint.state.fsm_version!r} does "
                f"not match FSM '{self._fsm.name}' version {self._fsm.version!r}"
            )
        if checkpoint.state.key.fsm_kind != "occupancy":
            raise InvalidTemporalInputError(
                "checkpoint state key fsm_kind is not 'occupancy' — cross-FSM restore is rejected"
            )
        if checkpoint.state.key.track_id != OCCUPANCY_SCOPE_TRACK:
            raise InvalidTemporalInputError(
                "checkpoint state key must use the canonical aggregate track "
                "sentinel (occupancy scope keys are never per-track)"
            )
        return checkpoint.state

    # ------------------------------------------------------------------
    # Validation (provenance integrity)
    # ------------------------------------------------------------------

    def _validate(self, state: OccupancyState, inp: OccupancyInput) -> None:
        if not isinstance(state, OccupancyState):
            raise InvalidTemporalInputError(
                f"state must be an OccupancyState, got {type(state).__name__}"
            )
        if not isinstance(inp, OccupancyInput):
            raise InvalidTemporalInputError(
                f"input must be an OccupancyInput, got {type(inp).__name__}"
            )
        transition = inp.transition
        if not isinstance(transition, TemporalTransition):
            raise InvalidTemporalInputError(
                f"transition must be a TemporalTransition, got {type(transition).__name__}"
            )
        if state.key != inp.key:
            raise InvalidTemporalInputError(
                "occupancy input key must match the state key (cross-scope apply is rejected)"
            )
        if inp.key.fsm_kind != "occupancy":
            raise InvalidTemporalInputError(
                f"occupancy input key fsm_kind must be 'occupancy', got {inp.key.fsm_kind!r}"
            )
        if inp.key.track_id != OCCUPANCY_SCOPE_TRACK:
            raise InvalidTemporalInputError(
                "occupancy scope keys must use the canonical aggregate track "
                "sentinel (derive the key with occupancy_scope_key)"
            )
        if state.fsm_version != self._fsm.version:
            raise FsmVersionMismatchError(
                f"state FSM version {state.fsm_version!r} does not match FSM "
                f"'{self._fsm.name}' version {self._fsm.version!r}"
            )
        if transition.key.fsm_kind != "presence":
            raise InvalidTemporalInputError(
                f"occupancy consumes presence-family transitions, got transition "
                f"fsm_kind {transition.key.fsm_kind!r}"
            )
        if inp.observation_kind not in self.OBSERVATION_KINDS:
            raise InvalidTemporalInputError(
                f"unknown observation_kind {inp.observation_kind!r} for FSM "
                f"'{self._fsm.name}'; allowed: {', '.join(self.OBSERVATION_KINDS)}"
            )
        if occupancy_event_from_presence(transition) != inp.observation_kind:
            raise InvalidTemporalInputError(
                "observation_kind does not match the presence transition reason "
                "(derive the kind with occupancy_event_from_presence)"
            )
        if inp.processing_time.tzinfo is None:
            raise InvalidTemporalInputError(
                "processing_time must be timezone-aware UTC (metadata only, "
                "never used for ordering)"
            )
        if transition.event_time.tzinfo is None:
            raise InvalidTemporalInputError("transition event_time must be timezone-aware UTC")
        self._check_scope_matches_transition(inp.key, transition.key)

    def _check_scope_matches_transition(
        self, scope_key: TemporalStateKey, transition_key: TemporalStateKey
    ) -> None:
        """Provenance integrity: the aggregate scope and the entity must
        agree on every scope component (track and fsm_kind differ by
        design). Cross-session/tenant/venue/camera/configuration exits
        are impossible — explicit rejection, never a fallback."""
        mismatches: list[str] = []
        if scope_key.tenant_id != transition_key.tenant_id:
            mismatches.append("tenant_id")
        if scope_key.venue_id != transition_key.venue_id:
            mismatches.append("venue_id")
        if scope_key.session_id != transition_key.session_id:
            mismatches.append("session_id")
        if scope_key.camera_id != transition_key.camera_id:
            mismatches.append("camera_id")
        if scope_key.configuration_version_id != transition_key.configuration_version_id:
            mismatches.append("configuration_version_id")
        if scope_key.semantic_context != transition_key.semantic_context:
            mismatches.append("semantic_context")
        if mismatches:
            raise StateKeyMismatchError(
                f"occupancy scope key does not match the presence transition "
                f"provenance ({', '.join(mismatches)}); cross-scope aggregation "
                "is rejected"
            )

    # ------------------------------------------------------------------
    # State bookkeeping (pure)
    # ------------------------------------------------------------------

    @staticmethod
    def _position_for(state: OccupancyState, track: TrackId) -> tuple[datetime, FrameId] | None:
        """The last applied position for ``track`` (idempotency bookkeeping).

        O(1) dict lookup (Task 15.4 §28: no linear scans)."""
        return state.entity_positions.get(track)

    # ------------------------------------------------------------------
    # Snapshot derivation (pure)
    # ------------------------------------------------------------------

    def _build_snapshot(
        self,
        *,
        state: OccupancyState,
        previous_count: int,
        event_time: datetime,
        source_transition: TemporalTransition,
    ) -> OccupancySnapshot:
        """One deterministic occupancy fact for a changed entity set."""
        count = state.occupancy_count
        delta = count - previous_count
        snapshot_id = EventId(
            uuid5(
                TEMPORAL_ID_NAMESPACE,
                self._snapshot_identity(
                    key=state.key,
                    event_time=event_time,
                    count=count,
                    source_transition_id=source_transition.transition_id,
                ),
            )
        )
        return OccupancySnapshot(
            snapshot_id=snapshot_id,
            fsm_kind=self._fsm.name,
            key=state.key,
            event_time=event_time,
            previous_count=previous_count,
            delta=delta,
            occupancy_count=count,
            occupied_tracks=tuple(sorted(state.occupied_tracks, key=str)),
            source_transition_id=source_transition.transition_id,
            fsm_version=self._fsm.version,
            policy_revision=self._policy.revision,
        )

    def _snapshot_identity(
        self,
        *,
        key: TemporalStateKey,
        event_time: datetime,
        count: int,
        source_transition_id: EventId,
    ) -> str:
        """Content-derived identity string for a snapshot (deterministic)."""
        return "|".join([
            key.canonical(),
            event_time.isoformat(),
            str(count),
            str(source_transition_id),
            self._fsm.version,
            self._policy.revision,
        ])
