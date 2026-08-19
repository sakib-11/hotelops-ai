"""Canonical temporal intelligence contracts (Task 15 Step 1).

The deterministic temporal foundation: it converts canonical Task 14
observations (``SpatialObservation`` / ``LineCrossingObservation``) into
stable, checkpointable time-based state under an explicit event-time
discipline.

Architecture (Task 15 Step 1):

    Task 14 SpatialObservation
        ↓ temporal event-time ordering (event_time is authoritative)
    TemporalStateKey (tenant+venue+session+camera+config+track+context)
        ↓
    FSM State  (deterministic, versioned)
        ↓
    Transition Evaluation  (hysteresis / occlusion / grace, configurable)
        ↓
    TemporalState  (serializable, bounded, checkpointable)
        ↓
    Future Task 15 FSMs (enter/exit, dwell, occupancy, waiting, movement)

The presence FSM (``PRESENCE_FSM``, Task 15.2) is the full enter/exit
model — ABSENT / ENTERING / PRESENT / EXITING — and the dwell FSM
(``DWELL_FSM``, Task 15.3) derives ``DwellInterval`` facts from its
confirmed transitions. The occupancy family (Task 15.4) aggregates
confirmed presence across entities within a spatial scope into
``OccupancyState``/``OccupancySnapshot`` facts. The movement foundation
(Task 15.5.1) classifies a tracked entity as UNKNOWN / STATIONARY /
MOVING from consecutive canonical spatial observations into
``MovementState``/``MovementMeasurement`` facts; the movement
classification layer (Task 15.5.2) consumes those measurements and
applies hysteresis + event-time qualification into
``MovementClassificationState``/``MovementClassificationTransition``
facts. The waiting family (Task 15.5.3) interprets a confirmed-PRESENT,
stationary entity inside an explicitly configured waiting-capable
context (``TemporalPolicy.waiting_contexts`` — queue areas, service
areas, WAITING_AREA zones) into ``WaitingState``/``WaitingInterval``
facts after a configured event-time qualification — waiting is an
operational interpretation, never merely "not moving".

Timestamp semantics (reuse ``contracts.common.time`` — no new format):
  - ``event_time``     — when the real-world event occurred (AUTHORITATIVE
    ordering time; never substituted by processing time).
  - ``processing_time``— when the system processed the observation
    (metadata only on inputs/facts; NEVER used for ordering).
  - ``ingestion_time`` — pipeline-level (Task 7 envelope ``ingested_at``);
    the temporal engine does not need it and does not model it.

The engine is PURE and DETERMINISTIC: it performs no database, Redis,
HTTP, object-storage, FastAPI, or LLM calls, and reads no current time.
Persistence is a separate boundary — the engine exposes serializable
checkpoints that a repository/adapter layer may store.

Determinism and identity:
  - Transition IDs are content-derived (UUID5 over the canonical key +
    observation identity + outcome), so replaying the same timeline
    produces byte-identical output — this is also the idempotency marker
    (the same observation applied twice yields the same transition).
  - ``TemporalStateKey.canonical()`` is the deterministic identity used
    to keep unrelated entities' state strictly separated.

FSM versioning: every state/checkpoint carries ``fsm_version`` and the
checkpoint carries ``engine_version`` + ``policy_revision``. Restoring a
checkpoint whose versions or policy revision differ from the engine's is
rejected with a typed error — historical state is never silently
reinterpreted by changed semantics.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    EventId,
    FrameId,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)
from contracts.spatial import SpatialPointModel

# The only movement-classification states (Task 15.5.1 §6). Shared by
# the MovementState contract validator and the MOVEMENT_FSM declaration
# so the two can never drift apart. NO additional states (waiting,
# queueing, service, session_closed) are introduced here — they belong
# to later tasks.
MOVEMENT_STATES: tuple[str, ...] = ("unknown", "stationary", "moving")

# The only waiting states (Task 15.5.3 §5). NOT_WAITING is the pristine
# state; WAITING_CANDIDATE is present + stationary + waiting context
# before the configured qualification duration; WAITING is confirmed.
# Shared by the WaitingState contract validator and the WAITING_FSM
# declaration so the two can never drift apart.
WAITING_STATES: tuple[str, ...] = ("not_waiting", "waiting_candidate", "waiting")

# Version of the temporal interpretation engine. Bumped when temporal
# semantics change; state/checkpoints carry the version that produced them.
# 0.2.0 — Task 15.2: the presence FSM gained the ENTERING/EXITING
# intermediate states (full 4-state enter/exit model). Historical 0.1.0
# checkpoints are therefore rejected (never silently reinterpreted).
TEMPORAL_ENGINE_VERSION = "0.2.0"

# Fixed namespace for content-derived transition identities (UUID5).
# Deterministic per (key, observation, outcome) — the same timeline
# always yields the same transition IDs.
TEMPORAL_ID_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


class TemporalReason(StrEnum):
    """Why the FSM produced this transition/fact (deterministic)."""

    OBSERVED_STAY = "observed_stay"  # accepted observation; state unchanged
    ENTER_CONFIRMED = "enter_confirmed"  # entry confirmation reached
    EXIT_CONFIRMED = "exit_confirmed"  # exit confirmation reached
    MISSING_EXPIRED = "missing_expired"  # occlusion gap exceeded tolerance
    DEDUPLICATED = "deduplicated"  # identical observation already applied
    REORDERED = "reordered"  # late observation accepted within the window
    SESSION_CLOSED = "session_closed"  # session explicitly closed
    MOVEMENT_EXCEEDED = "movement_exceeded"  # Task 15.5.2 MOVING confirmed while waiting


class TemporalOcclusionState(StrEnum):
    """Distinguishes 'not observed' from 'temporarily missing' (Task 15 §11).

    OBSERVED            — the entity was last positively present.
    TEMPORARILY_MISSING — a short gap within ``occlusion_tolerance_seconds``;
                          state is NOT flipped by a missing observation.
    NOT_OBSERVED        — never positively observed in this context.
    """

    OBSERVED = "observed"
    TEMPORARILY_MISSING = "temporarily_missing"
    NOT_OBSERVED = "not_observed"


class TemporalStateKey(BaseModel, frozen=True):
    """Identity of one temporal state — never mixes unrelated entities.

    Every component is a canonical ID.    ``semantic_context`` scopes the
    state to a spatial identity (zone/table/entrance profile id) when
    the FSM is context-specific. ``fsm_kind`` names the FSM family
    (``"presence"`` / ``"dwell"`` / ``"occupancy"`` / ``"movement"`` /
    ``"movement_classification"`` / ``"waiting"``).
    """

    model_config = {"extra": "forbid"}

    fsm_kind: str = Field(..., min_length=1)
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId
    configuration_version_id: ConfigurationVersionId
    track_id: TrackId
    semantic_context: str | None = Field(default=None, min_length=1)

    def canonical(self) -> str:
        """Deterministic identity string for this state key.

        Used for transition-id derivation and to prove isolation: two
        keys differing in ANY component produce different identities.
        """
        return "|".join([
            self.fsm_kind,
            str(self.tenant_id),
            str(self.venue_id),
            str(self.session_id),
            str(self.camera_id),
            str(self.configuration_version_id),
            str(self.track_id),
            self.semantic_context or "",
        ])


class TemporalPolicy(BaseModel, frozen=True):
    """Configurable temporal behavior — values are NEVER hardcoded in logic.

    ``revision`` identifies this exact configuration; a checkpoint
    restored under a different revision is rejected (typed error) so
    changed thresholds can never silently reinterpret historical state.

    Semantics (documented foundation defaults, all overridable):
      - ``reorder_window_seconds``  — late events at least this close to
        the watermark are accepted (REORDERED fact, no state rewind);
        older events raise ``LateEventError``.
      - ``entry_confirmation``      — consecutive `present` observations
        required to move ABSENT -> PRESENT (via the ENTERING intermediate
        state). With ``entry_confirmation == 1`` a single positive
        observation confirms directly (the configured policy explicitly
        allows instant entry).
      - ``exit_confirmation``       — consecutive qualifying `absent`
        observations required to move PRESENT -> ABSENT (via the EXITING
        intermediate state). With ``exit_confirmation == 1`` a single
        qualifying absence confirms directly.
      - ``minimum_dwell_seconds``   — PRESENT must persist at least this
        long (event time, from ``state_since``) before any exit logic.
      - ``exit_grace_seconds``      — an `absent` observation must be at
        least this far (event time) from the last positive presence
        before it counts toward exit confirmation (anti-jitter). Grace
        is DERIVED from ``last_present_seen`` + this threshold and is
        deliberately not persisted as a ``grace_until`` deadline — a
        persisted deadline could go stale across restarts (Task 15 §11).
      - ``occlusion_tolerance_seconds`` — a `not_observed` gap beyond
        this (event time, from ``last_present_seen``) confirms exit;
        shorter gaps set TEMPORARILY_MISSING without flipping state.
      - ``dwell_minimum_seconds`` — the Task 15.3 dwell-fact qualification
        threshold: a closed dwell interval is marked ``qualified`` only
        when its duration is at least this long. The threshold NEVER
        alters the recorded interval (dwell_start/dwell_end/duration are
        the actual presence span) — it only flags the fact.
      - ``movement_threshold`` — the Task 15.5.1 movement displacement
        magnitude (in the point's declared coordinate space) that a
        consecutive observation pair must EXCEED to classify the entity
        as MOVING; at-or-below it is STATIONARY (strictly-above
        semantics: a zero-displacement pair is stationary even under the
        degenerate ``0.0`` default). This is the ONLY movement knob in
        the foundation — hysteresis and qualification durations are the
        Task 15.5.2 classification concern and are deliberately absent
        here.
      - ``movement_enter_threshold`` — the Task 15.5.2 classification
        displacement above which a measurement counts as MOVING
        evidence (the "movement policy"). Strictly-above: a pair whose
        distance exactly equals the threshold is NOT moving evidence.
      - ``movement_exit_threshold`` — the Task 15.5.2 classification
        displacement below which a measurement counts as STATIONARY
        evidence (the "stationary policy"). Strictly-below.
      - ``movement_qualification_seconds`` — the Task 15.5.2 event-time
        window a measurement's evidence must remain sustained (in the
        direction away from the current state) before the classification
        changes. ``0.0`` disables qualification: one qualifying
        measurement changes the state immediately (the degenerate
        single-threshold behavior). Qualification uses event time only.
      - ``waiting_qualification_seconds`` — the Task 15.5.3 event-time
        duration a WAITING_CANDIDATE must remain (confirmed presence +
        stationary + waiting-capable context) before the classification
        becomes WAITING. ``0.0`` disables the delay: the first qualifying
        step confirms immediately. Qualification uses event time only;
        the same value is the ``WaitingInterval`` minimum-duration
        threshold (the flag marks, never alters the recorded span).
      - ``waiting_contexts`` — the EXPLICIT set of waiting-capable
        spatial contexts (``TemporalStateKey.semantic_context`` values,
        the Task 10 profile ids). Waiting is NEVER inferred: an empty set
        (the safe default) means no context in the venue can produce
        WAITING. Populate it from a Task 10 configuration snapshot with
        ``waiting_contexts_from_configuration`` (queue areas, service
        areas, and ``ZoneType.WAITING_AREA`` zones only — never
        restaurant/lobby/hallway/table/entrance by themselves).
      - ``transition_history_limit`` — bounded recent-transition ring on
        the state (no unbounded in-memory history).

    Hysteresis (Task 15.5.2): ``movement_exit_threshold <=
    movement_enter_threshold`` is validated at construction. With equal
    thresholds the band is degenerate (a single boundary); with a wider
    enter threshold, a measurement between the two thresholds retains the
    current classification and never flips it.
    """

    model_config = {"extra": "forbid"}

    revision: str = Field(default="v1", min_length=1)
    reorder_window_seconds: float = Field(default=60.0, ge=0)
    entry_confirmation: int = Field(default=2, ge=1)
    exit_confirmation: int = Field(default=3, ge=1)
    minimum_dwell_seconds: float = Field(default=10.0, ge=0)
    exit_grace_seconds: float = Field(default=30.0, ge=0)
    occlusion_tolerance_seconds: float = Field(default=60.0, ge=0)
    dwell_minimum_seconds: float = Field(default=0.0, ge=0)
    movement_threshold: float = Field(default=0.0, ge=0)
    movement_enter_threshold: float = Field(default=0.0, ge=0)
    movement_exit_threshold: float = Field(default=0.0, ge=0)
    movement_qualification_seconds: float = Field(default=0.0, ge=0)
    waiting_qualification_seconds: float = Field(default=0.0, ge=0)
    waiting_contexts: frozenset[str] = Field(default_factory=frozenset)
    transition_history_limit: int = Field(default=8, ge=0)

    @model_validator(mode="after")
    def _validate_hysteresis(self) -> TemporalPolicy:
        """Hysteresis requires the enter threshold to dominate the exit.

        ``movement_exit_threshold`` must never exceed
        ``movement_enter_threshold`` — otherwise the "below exit is
        STATIONARY, above enter is MOVING" contract has no band and the
        two policies contradict (a distance could be simultaneously
        above enter AND below exit). Equal thresholds are allowed (the
        degenerate single-boundary case).
        """
        if self.movement_exit_threshold > self.movement_enter_threshold:
            raise ValueError(
                "movement_exit_threshold must not exceed movement_enter_threshold "
                "(hysteresis requires enter >= exit)"
            )
        return self


class TemporalTransition(BaseModel, frozen=True):
    """One deterministic FSM step: an accepted observation or a fact.

    Produced by the pure temporal engine for every applied observation
    (including deduplicated and reordered inputs). ``transition_id`` is
    content-derived (UUID5) — replaying the same timeline reproduces the
    same identities. This is an observation/fact record, never a side
    effect: the engine performs no I/O when producing it.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    transition_id: EventId
    fsm_kind: str
    key: TemporalStateKey
    from_state: str
    to_state: str
    event_kind: str  # the observation kind that triggered the step
    reason: TemporalReason
    observation_frame_id: FrameId
    event_time: datetime
    processing_time: datetime
    configuration_version_id: ConfigurationVersionId
    fsm_version: str

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_processing = field_validator("processing_time")(validate_utc)


class TemporalState(BaseModel, frozen=True):
    """Checkpointable temporal state for one TemporalStateKey.

    Bounded by design: fixed scalar fields plus a bounded
    ``recent_transitions`` ring (``TemporalPolicy.transition_history_limit``).
    No per-observation history is retained, so a long-running session
    cannot grow the state. ``configuration_version_id`` is carried inside
    ``key``; historical reconstruction always uses the pinned version the
    key names — never \"the latest configuration\".
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    fsm_version: str
    key: TemporalStateKey
    current_state: str
    # When the current state was entered (None only for the pristine
    # initial state before the first observation).
    state_since: datetime | None = None
    last_seen: datetime | None = None  # last observation event_time
    last_present_seen: datetime | None = None  # last positive presence
    # Event-time position of the last applied observation (the watermark).
    watermark_event_time: datetime | None = None
    last_applied_frame_id: FrameId | None = None
    entry_confirm_count: int = 0
    exit_confirm_count: int = 0
    occlusion_state: TemporalOcclusionState = TemporalOcclusionState.NOT_OBSERVED
    missing_since: datetime | None = None
    recent_transitions: tuple[TemporalTransition, ...] = ()

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_state_since = field_validator("state_since")(validate_utc)
    _validate_last_seen = field_validator("last_seen")(validate_utc)
    _validate_last_present = field_validator("last_present_seen")(validate_utc)
    _validate_watermark = field_validator("watermark_event_time")(validate_utc)
    _validate_missing_since = field_validator("missing_since")(validate_utc)


class DwellInterval(BaseModel, frozen=True):
    """One closed or running dwell interval (Task 15.3 §10/§11).

    Dwell is the event-time span during which an entity was continuously
    PRESENT (per the Enter/Exit FSM) within one spatial context: it
    starts at the confirmed-PRESENT transition and ends at the confirmed
    ABSENT transition (or explicit session closure). It is never computed
    from raw frames or wall-clock time.

    - ``dwell_end`` is None while the interval is still OPEN (running);
      ``last_seen`` is then the most recent accepted observation
      event_time (no fabricated end).
    - ``qualified`` distinguishes the ACTUAL presence span from a fact
      that meets the configured ``minimum_dwell_seconds`` threshold. A
      short interval is still a real interval — it is simply not
      qualified (§9: the threshold must never pretend the entity was
      absent).
    - ``reason`` records how a closed interval ended (confirmed exit,
      occlusion expiry, or session closure); None while open.
    - ``interval_id`` is content-derived (UUID5 over the canonical key +
      interval bounds + versions), so replaying the same timeline
      reproduces the same identities — the same idempotency principle as
      ``TemporalTransition``.

    All provenance (tenant/venue/session/camera/track/configuration
    version/spatial context) is carried by the nested ``key`` — never
    duplicated.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    interval_id: EventId
    fsm_kind: str = Field(..., min_length=1)
    key: TemporalStateKey
    dwell_start: datetime
    dwell_end: datetime | None = None
    last_seen: datetime
    duration_seconds: float | None = None
    qualified: bool = False
    minimum_dwell_seconds: float = Field(..., ge=0)
    reason: TemporalReason | None = None
    fsm_version: str
    policy_revision: str

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_start = field_validator("dwell_start")(validate_utc)
    _validate_end = field_validator("dwell_end")(validate_utc)
    _validate_last_seen = field_validator("last_seen")(validate_utc)

    @field_validator("duration_seconds")
    @classmethod
    def _duration_non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("duration_seconds must be >= 0")
        return value

    @model_validator(mode="after")
    def _validate_ordering(self) -> DwellInterval:
        """Reject corrupted intervals: end or last_seen before start."""
        if self.dwell_end is not None and self.dwell_end < self.dwell_start:
            raise ValueError("dwell_end must not precede dwell_start")
        if self.last_seen < self.dwell_start:
            raise ValueError("last_seen must not precede dwell_start")
        return self


class OccupancyState(BaseModel, frozen=True):
    """Aggregate occupancy state for one spatial scope (Task 15.4 §18).

    Occupancy answers \"how many UNIQUE entities are confirmed PRESENT in
    a defined spatial context at event-time\". It is an AGGREGATE across
    tracks, so this state is scoped by the occupancy key (tenant + venue +
    session + camera + configuration version + semantic context, with the
    track component replaced by the canonical aggregate sentinel) rather
    than per-track.

    - ``occupied_tracks`` — the entities currently counted. A frozenset
      keeps membership/add/remove O(1) (Task 15.4 §28: prefer keyed
      collections); ``occupancy_count`` is DERIVED as its size, so the
      invariant ``count == number of occupied identities`` holds by
      construction and count and set can never drift.
    - ``entity_positions`` — per-track last-applied ``(event_time,
      frame_id)`` in a dict for O(1) lookups (Task 15.4 §28: prefer
      keyed collections over linear scans). This is the idempotency
      bookkeeping: replaying the same transition for the same track is
      detected as a duplicate (Task 7 principle, reused — not
      re-architected). It is bounded by the number of distinct tracks
      ever seen in the scope and MUST be retained after an exit so a
      replayed exit is deduplicated instead of raising a false invariant
      failure. The dict is never mutated in place — every transition
      rebuilds it via ``model_copy`` — and the checkpoint serializes it
      sorted by track id for byte determinism.
    - ``watermark_event_time`` / ``last_applied_frame_id`` — the
      aggregate event-time position (max over applied transitions across
      all tracks), driving the 15.1 late/out-of-order policy.

    Invariants (validated at construction): fsm_kind must be
    ``"occupancy"`` and every occupied track must have a recorded
    position. A negative count is unrepresentable (count is derived); an
    \"exit for an entity never present\" is rejected by the engine's FSM
    (never clamped).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    fsm_version: str
    key: TemporalStateKey
    occupied_tracks: frozenset[TrackId] = frozenset()
    entity_positions: dict[TrackId, tuple[datetime, FrameId]] = Field(default_factory=dict)
    watermark_event_time: datetime | None = None
    last_applied_frame_id: FrameId | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_watermark = field_validator("watermark_event_time")(validate_utc)

    @property
    def occupancy_count(self) -> int:
        """The occupancy count — derived from the entity set (§2/§17)."""
        return len(self.occupied_tracks)

    @model_validator(mode="after")
    def _validate_occupancy_invariants(self) -> OccupancyState:
        if self.key.fsm_kind != "occupancy":
            raise ValueError("OccupancyState key fsm_kind must be 'occupancy'")
        if not self.occupied_tracks <= set(self.entity_positions):
            raise ValueError(
                "occupied_tracks must be a subset of entity_positions "
                "(every occupied entity must have a recorded position)"
            )
        return self


class OccupancySnapshot(BaseModel, frozen=True):
    """One deterministic occupancy fact (Task 15.4 §16/§17).

    Emitted whenever the entity set changes (a confirmed enter or exit
    was applied), carrying everything needed to reproduce the result:

    - the occupancy scope (nested ``key`` — tenant/venue/session/camera/
      configuration version/spatial context; never duplicated),
    - ``event_time`` of the source presence transition,
    - ``previous_count`` + ``delta`` = ``occupancy_count`` (every change
      has an explicit source — validated), and ``occupied_tracks`` (the
      entities counted, sorted for deterministic serialization),
    - ``source_transition_id`` — the presence ``TemporalTransition`` that
      caused the change (§17: no unexplained count changes),
    - ``fsm_version`` + ``policy_revision`` provenance.

    ``snapshot_id`` is content-derived (UUID5 over the scope + event-time
    + resulting count + source transition), so replaying the same
    timeline reproduces the same identities (the same idempotency
    principle as ``TemporalTransition`` / ``DwellInterval``).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    snapshot_id: EventId
    fsm_kind: str = Field(..., min_length=1)
    key: TemporalStateKey
    event_time: datetime
    previous_count: int = Field(..., ge=0)
    delta: int
    occupancy_count: int = Field(..., ge=0)
    occupied_tracks: tuple[TrackId, ...] = ()
    source_transition_id: EventId
    fsm_version: str
    policy_revision: str

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_delta(self) -> OccupancySnapshot:
        if self.previous_count + self.delta != self.occupancy_count:
            raise ValueError(
                "previous_count + delta must equal occupancy_count "
                "(every occupancy change must be explained)"
            )
        return self


class OccupancyCheckpoint(BaseModel, frozen=True):
    """Serializable checkpoint for occupancy restart recovery (Task 15.4 §18/§19).

    The same versioned discipline as ``TemporalCheckpoint`` applied to
    the aggregate occupancy state: engine version + policy revision +
    the ``OccupancyState`` (count derived from the entity set, occupied
    entity identities, spatial scope, session, configuration version,
    watermark, FSM version). Restoring under a different version or
    policy revision is rejected with a typed error — historical
    occupancy is never silently reinterpreted.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    engine_version: str = Field(default=TEMPORAL_ENGINE_VERSION)
    policy_revision: str
    state: OccupancyState

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready serialization (deterministic field order).

        The ``occupied_tracks`` frozenset is serialized sorted so the
        bytes are deterministic regardless of set iteration order.
        """
        data = self.model_dump(mode="json")
        data["state"]["occupied_tracks"] = sorted(data["state"]["occupied_tracks"], key=str)
        data["state"]["entity_positions"] = {
            k: v
            for k, v in sorted(data["state"]["entity_positions"].items(), key=lambda kv: str(kv[0]))
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OccupancyCheckpoint:
        """Deserialize a checkpoint previously produced by ``to_dict``."""
        return cls.model_validate(data)


class MovementState(BaseModel, frozen=True):
    """Movement classification state for one tracked entity (Task 15.5.1).

    Movement is PER-TRACK: it classifies an existing entity as UNKNOWN /
    STATIONARY / MOVING from consecutive canonical spatial observations
    (``current_state`` holds the classification, ``MOVEMENT_STATES``).

    - ``previous_position`` / ``previous_event_time`` — the anchor of
      the next measurement: the last applied in-order observation. They
      are set together (validated) and are never used for ordering —
      ordering is the watermark's job (``watermark_event_time`` /
      ``last_applied_frame_id``, the 15.1 discipline). For an in-order
      per-track stream they coincide with ``last_seen``; after a
      within-window reorder only ``last_seen`` may refresh, never the
      anchor or the watermark.
    - ``state_since`` — when the current classification was entered
      (None while UNKNOWN before the first measurement); the foundation
      for the 15.5.5 qualification windows, not implemented here.
    - All provenance (tenant/venue/session/camera/configuration version/
      track) is carried by the nested ``key`` — never duplicated.

    The state carries no time history: measurement pairing needs exactly
    one previous observation, so the state is bounded by construction.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    fsm_version: str
    key: TemporalStateKey
    current_state: str = "unknown"
    state_since: datetime | None = None
    previous_position: SpatialPointModel | None = None
    previous_event_time: datetime | None = None
    last_seen: datetime | None = None
    watermark_event_time: datetime | None = None
    last_applied_frame_id: FrameId | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_state_since = field_validator("state_since")(validate_utc)
    _validate_previous_time = field_validator("previous_event_time")(validate_utc)
    _validate_last_seen = field_validator("last_seen")(validate_utc)
    _validate_watermark = field_validator("watermark_event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_movement_invariants(self) -> MovementState:
        if self.key.fsm_kind != "movement":
            raise ValueError("MovementState key fsm_kind must be 'movement'")
        if self.current_state not in MOVEMENT_STATES:
            raise ValueError(
                f"current_state must be one of {', '.join(MOVEMENT_STATES)}, "
                f"got {self.current_state!r}"
            )
        if (self.previous_position is None) != (self.previous_event_time is None):
            raise ValueError(
                "previous_position and previous_event_time must be set together "
                "(a measurement anchor is an observation, not a bare point)"
            )
        return self


class MovementMeasurement(BaseModel, frozen=True):
    """One deterministic movement measurement of a consecutive pair (15.5.1 §3).

    For two consecutive in-order observations of the SAME entity:

      distance        = Euclidean distance(previous_position, current_position)
      time_delta      = current_event_time - previous_event_time

    ``distance`` is expressed in the pair's declared coordinate space
    (the two points are validated to share it). It is a displacement in
    that space ONLY — the engine deliberately computes NO velocity: an
    IMAGE_NORMALIZED displacement is never pretended to be physical
    speed (Task 15.5.1 §4). ``time_delta_seconds`` may be 0.0 for equal
    event timestamps (handled, never a division).

    Every field is canonical provenance (nested ``key``) or a declared
    measurement result; ``measurement_id`` is content-derived (UUID5
    over the key + pair + versions), so replaying the same timeline
    reproduces the same identities (the same idempotency principle as
    ``TemporalTransition`` / ``DwellInterval``).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    measurement_id: EventId
    fsm_kind: str = Field(..., min_length=1)
    key: TemporalStateKey
    previous_position: SpatialPointModel
    current_position: SpatialPointModel
    previous_event_time: datetime
    event_time: datetime
    distance: float = Field(..., ge=0)
    time_delta_seconds: float = Field(..., ge=0)
    fsm_version: str
    policy_revision: str

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_previous_time = field_validator("previous_event_time")(validate_utc)
    _validate_event_time = field_validator("event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_pair(self) -> MovementMeasurement:
        if self.event_time < self.previous_event_time:
            raise ValueError(
                "event_time must not precede previous_event_time "
                "(a measurement is previous -> current)"
            )
        if self.previous_position.coordinate_space != self.current_position.coordinate_space:
            raise ValueError(
                "previous and current positions must share a coordinate space "
                "(mixing IMAGE_NORMALIZED and VENUE_LOCAL displacement is undefined)"
            )
        return self


class MovementCheckpoint(BaseModel, frozen=True):
    """Serializable checkpoint for movement restart recovery (Task 15.5.1 §15).

    The same versioned discipline as ``TemporalCheckpoint`` /
    ``OccupancyCheckpoint`` applied to the per-track movement state:
    engine version + policy revision + the ``MovementState`` (current
    classification, previous position, previous event time, identity,
    configuration version via the key, watermark). Restoring under a
    different version or policy revision is rejected with a typed error
    — historical movement state is never silently reinterpreted.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    engine_version: str = Field(default=TEMPORAL_ENGINE_VERSION)
    policy_revision: str
    state: MovementState

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready serialization (deterministic field order)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MovementCheckpoint:
        """Deserialize a checkpoint previously produced by ``to_dict``."""
        return cls.model_validate(data)


class MovementClassificationState(BaseModel, frozen=True):
    """Movement classification state for one tracked entity (Task 15.5.2).

    The 15.5.2 layer ON TOP of the 15.5.1 measurement foundation: it is
    fed the ``MovementMeasurement`` facts of one entity and applies
    hysteresis + event-time qualification to decide UNKNOWN / STATIONARY /
    MOVING. The measurement (distance + time delta) is NEVER recomputed
    here — it is consumed verbatim from the foundation.

    - ``current_state`` / ``state_since`` — the classification and the
      event_time at which it was entered (None while UNKNOWN before the
      first measurement).
    - ``pending_state`` / ``qualification_started`` — the qualification
      run: when a measurement's evidence points away from the current
      state, the target state is held pending and the classification
      only changes once the evidence stays in that direction for
      ``TemporalPolicy.movement_qualification_seconds`` of event time.
      Both are set together (validated); pending is always the state the
      entity is NOT currently in (a state is never qualified toward
      itself). A measurement that contradicts the pending direction, or
      falls into the hysteresis band, cancels the run — "qualified"
      means every measurement since the run started sustained it.
    - ``watermark_event_time`` / ``last_applied_frame_id`` — the 15.1
      ordering discipline (event_time authoritative, per-track).

    All provenance (tenant/venue/session/camera/configuration version/
    track/spatial context) is carried by the nested ``key`` — never
    duplicated. The state carries no measurement history: each step
    needs only the current measurement, so the state is bounded by
    construction.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    fsm_version: str
    key: TemporalStateKey
    current_state: str = "unknown"
    state_since: datetime | None = None
    pending_state: str | None = None
    qualification_started: datetime | None = None
    last_seen: datetime | None = None
    watermark_event_time: datetime | None = None
    last_applied_frame_id: FrameId | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_state_since = field_validator("state_since")(validate_utc)
    _validate_qualification_started = field_validator("qualification_started")(validate_utc)
    _validate_last_seen = field_validator("last_seen")(validate_utc)
    _validate_watermark = field_validator("watermark_event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_classification_invariants(self) -> MovementClassificationState:
        if self.key.fsm_kind != "movement_classification":
            raise ValueError(
                "MovementClassificationState key fsm_kind must be 'movement_classification'"
            )
        if self.current_state not in MOVEMENT_STATES:
            raise ValueError(
                f"current_state must be one of {', '.join(MOVEMENT_STATES)}, "
                f"got {self.current_state!r}"
            )
        if self.pending_state is not None and self.pending_state not in MOVEMENT_STATES:
            raise ValueError(
                f"pending_state must be one of {', '.join(MOVEMENT_STATES)} or None, "
                f"got {self.pending_state!r}"
            )
        if (self.pending_state is None) != (self.qualification_started is None):
            raise ValueError(
                "pending_state and qualification_started must be set together "
                "(a qualification run is a target state plus its event-time start)"
            )
        if self.pending_state is not None and self.pending_state == self.current_state:
            raise ValueError(
                "pending_state must differ from current_state "
                "(a state is never qualified toward itself)"
            )
        return self


class MovementClassificationTransition(BaseModel, frozen=True):
    """One deterministic movement-classification state change (Task 15.5.2).

    Emitted ONLY when the classification actually changes (UNKNOWN ->
    STATIONARY/MOVING, or a qualification-completed STATIONARY <-> MOVING)
    — stays never produce one. ``transition_id`` is content-derived
    (UUID5 over the key + transition + event-time + confirming
    measurement), so replaying the same timeline reproduces the same
    identities (the same idempotency principle as ``TemporalTransition`` /
    ``MovementMeasurement``).

    - ``event_time`` — the event time of the measurement that confirmed
      the change (the qualification-completed boundary, never the
      pending start, never processing time).
    - ``measurement_id`` — the 15.5.1 measurement that drove the change
      (the classification result preserves the measurement without
      duplicating it).
    - ``qualification_started`` — when the qualification run began; None
      for immediate first-classification changes (UNKNOWN -> x) and for
      the degenerate ``movement_qualification_seconds == 0`` policy.

    Full provenance (tenant/venue/session/camera/configuration version/
    track/spatial context) is carried by the nested ``key``.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    transition_id: EventId
    fsm_kind: str = Field(..., min_length=1)
    key: TemporalStateKey
    from_state: str
    to_state: str
    event_time: datetime
    measurement_id: EventId
    qualification_started: datetime | None = None
    fsm_version: str
    policy_revision: str

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_qualification_started = field_validator("qualification_started")(validate_utc)

    @model_validator(mode="after")
    def _validate_transition(self) -> MovementClassificationTransition:
        if self.from_state not in MOVEMENT_STATES or self.to_state not in MOVEMENT_STATES:
            raise ValueError(
                "classification transitions stay within UNKNOWN/STATIONARY/MOVING "
                f"(got {self.from_state!r} -> {self.to_state!r})"
            )
        if self.from_state == self.to_state:
            raise ValueError(
                "a classification transition must change the state "
                "(stays are never emitted as transitions)"
            )
        return self


class MovementClassificationCheckpoint(BaseModel, frozen=True):
    """Serializable checkpoint for classification restart recovery (15.5.2 §17).

    The same versioned discipline as ``MovementCheckpoint`` applied to the
    per-track classification state: engine version + policy revision + the
    ``MovementClassificationState`` (classification, state_since,
    qualification run, last event time, identity, configuration version
    via the key, watermark). Restoring under a different version or policy
    revision is rejected with a typed error — historical classification is
    never silently reinterpreted.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    engine_version: str = Field(default=TEMPORAL_ENGINE_VERSION)
    policy_revision: str
    state: MovementClassificationState

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready serialization (deterministic field order)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MovementClassificationCheckpoint:
        """Deserialize a checkpoint previously produced by ``to_dict``."""
        return cls.model_validate(data)


class WaitingState(BaseModel, frozen=True):
    """Waiting classification state for one tracked entity (Task 15.5.3).

    Waiting is an OPERATIONAL temporal interpretation ON TOP of the
    15.5.1/15.5.2 movement foundation and the 15.2 presence foundation: an
    entity is WAITING only when it is confirmed PRESENT, inside a
    configured waiting-capable context, classified STATIONARY by Task
    15.5.2, and has stayed that way for ``waiting_qualification_seconds``
    of event time. It is NEVER merely "not moving" — a stationary person
    in a lobby is STATIONARY, not WAITING.

    - ``current_state`` — NOT_WAITING / WAITING_CANDIDATE / WAITING.
    - ``presence_confirmed`` — whether this engine has SEEN the confirmed
      PRESENT (an ENTER_CONFIRMED presence event) for the entity. §2.1
      requires confirmed presence for waiting; a ``stay`` without a prior
      ``enter_confirmed`` never starts a candidate. Set by
      ``enter_confirmed``, cleared by confirmed presence loss / session
      closure, preserved by ``stay`` — and REQUIRED (True) in
      WAITING_CANDIDATE and WAITING.
    - ``candidate_start`` — the event_time at which the entity first
      satisfied (confirmed presence + stationary + waiting context);
      None outside WAITING_CANDIDATE/WAITING. It is preserved into
      WAITING as provenance (the task explicitly keeps it separate from
      the confirmed ``waiting_start``) and cleared on termination.
    - ``waiting_start`` — the event_time at which the qualification
      completed and WAITING was confirmed (never candidate_start, never
      processing time); None until confirmed.
    - ``watermark_event_time`` / ``last_applied_frame_id`` — the 15.1
      ordering discipline (event_time authoritative, per-track).

    All provenance (tenant/venue/session/camera/configuration version/
    track/spatial context) is carried by the nested ``key`` — never
    duplicated. The state holds scalars only: each step needs exactly the
    current presence transition + classification, so it is bounded by
    construction (no per-frame history).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    fsm_version: str
    key: TemporalStateKey
    current_state: str = "not_waiting"
    presence_confirmed: bool = False
    candidate_start: datetime | None = None
    waiting_start: datetime | None = None
    last_seen: datetime | None = None
    watermark_event_time: datetime | None = None
    last_applied_frame_id: FrameId | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_candidate_start = field_validator("candidate_start")(validate_utc)
    _validate_waiting_start = field_validator("waiting_start")(validate_utc)
    _validate_last_seen = field_validator("last_seen")(validate_utc)
    _validate_watermark = field_validator("watermark_event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_waiting_invariants(self) -> WaitingState:
        if self.key.fsm_kind != "waiting":
            raise ValueError("WaitingState key fsm_kind must be 'waiting'")
        if self.current_state not in WAITING_STATES:
            raise ValueError(
                f"current_state must be one of {', '.join(WAITING_STATES)}, "
                f"got {self.current_state!r}"
            )
        if self.current_state == "not_waiting":
            if self.candidate_start is not None or self.waiting_start is not None:
                raise ValueError(
                    "not_waiting must not carry candidate_start or waiting_start "
                    "(termination clears both)"
                )
        elif self.current_state == "waiting_candidate":
            if self.candidate_start is None:
                raise ValueError(
                    "waiting_candidate requires candidate_start "
                    "(a candidate is anchored to its qualifying event-time)"
                )
            if self.waiting_start is not None:
                raise ValueError(
                    "waiting_candidate must not carry waiting_start "
                    "(qualification has not completed)"
                )
            if not self.presence_confirmed:
                raise ValueError(
                    "waiting_candidate requires presence_confirmed "
                    "(waiting needs confirmed PRESENT — §2.1)"
                )
        else:  # waiting
            if self.candidate_start is None or self.waiting_start is None:
                raise ValueError(
                    "waiting requires both candidate_start and waiting_start "
                    "(candidate_start is preserved as provenance)"
                )
            if not self.presence_confirmed:
                raise ValueError(
                    "waiting requires presence_confirmed (waiting needs confirmed PRESENT — §2.1)"
                )
        return self


class WaitingInterval(BaseModel, frozen=True):
    """One confirmed waiting interval (Task 15.5.3 §10).

    Waiting is the event-time span during which an entity was confirmed
    WAITING inside one waiting-capable spatial context: it starts at the
    confirmation event_time (the moment the qualification duration was
    satisfied) and ends at the first relevant termination condition
    (confirmed presence exit, occlusion expiry, session closure, or a
    Task 15.5.2 MOVING confirmation). It is never computed from raw
    frames or wall-clock time.

    - ``waiting_end`` is None while the interval is still OPEN;
      ``last_seen`` is then the most recent accepted observation
      event_time (no fabricated end).
    - ``qualified`` distinguishes the ACTUAL waiting span from one that
      meets the configured ``minimum_waiting_seconds`` threshold (the
      same value as ``TemporalPolicy.waiting_qualification_seconds``) —
      a short confirmed waiting is still a real interval, it is simply
      not qualified. The threshold flags, never alters.
    - ``reason`` records how a closed interval ended (confirmed exit,
      occlusion expiry, session closure, or MOVEMENT_EXCEEDED); None
      while open.
    - ``interval_id`` is content-derived (UUID5 over the canonical key +
      interval bounds + versions), so replaying the same timeline
      reproduces the same identities — the same idempotency principle as
      ``DwellInterval`` / ``TemporalTransition``.

    All provenance (tenant/venue/session/camera/track/configuration
    version/spatial context) is carried by the nested ``key`` — never
    duplicated.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    interval_id: EventId
    fsm_kind: str = Field(..., min_length=1)
    key: TemporalStateKey
    waiting_start: datetime
    waiting_end: datetime | None = None
    last_seen: datetime
    duration_seconds: float | None = None
    qualified: bool = False
    minimum_waiting_seconds: float = Field(..., ge=0)
    reason: TemporalReason | None = None
    fsm_version: str
    policy_revision: str

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_start = field_validator("waiting_start")(validate_utc)
    _validate_end = field_validator("waiting_end")(validate_utc)
    _validate_last_seen = field_validator("last_seen")(validate_utc)

    @field_validator("duration_seconds")
    @classmethod
    def _duration_non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("duration_seconds must be >= 0")
        return value

    @model_validator(mode="after")
    def _validate_ordering(self) -> WaitingInterval:
        """Reject corrupted intervals: end or last_seen before start."""
        if self.waiting_end is not None and self.waiting_end < self.waiting_start:
            raise ValueError("waiting_end must not precede waiting_start")
        if self.last_seen < self.waiting_start:
            raise ValueError("last_seen must not precede waiting_start")
        return self


class WaitingCheckpoint(BaseModel, frozen=True):
    """Serializable checkpoint for waiting restart recovery (15.5.3 §22).

    The same versioned discipline as the sibling families: engine version
    + policy revision + the ``WaitingState`` (classification,
    candidate_start, waiting_start, identity, configuration version via
    the key, watermark). Restoring under a different version or policy
    revision is rejected with a typed error — historical waiting state is
    never silently reinterpreted.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    engine_version: str = Field(default=TEMPORAL_ENGINE_VERSION)
    policy_revision: str
    state: WaitingState

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready serialization (deterministic field order)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaitingCheckpoint:
        """Deserialize a checkpoint previously produced by ``to_dict``."""
        return cls.model_validate(data)


class TemporalCheckpoint(BaseModel, frozen=True):
    """Serializable checkpoint for restart recovery (Task 15 §12/§13/§21).

    Contains everything needed to resume without losing semantic state:
    the ``TemporalState`` (state, state_since, last_seen, watermark,
    event-time position, FSM version, key, configuration version) plus
    the engine version and policy revision that produced it. Restoring
    under different versions/revision is rejected (typed error).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    engine_version: str = Field(default=TEMPORAL_ENGINE_VERSION)
    policy_revision: str
    state: TemporalState

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready serialization (deterministic field order)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemporalCheckpoint:
        """Deserialize a checkpoint previously produced by ``to_dict``."""
        return cls.model_validate(data)


__all__ = [
    "MOVEMENT_STATES",
    "TEMPORAL_ENGINE_VERSION",
    "TEMPORAL_ID_NAMESPACE",
    "WAITING_STATES",
    "DwellInterval",
    "MovementCheckpoint",
    "MovementClassificationCheckpoint",
    "MovementClassificationState",
    "MovementClassificationTransition",
    "MovementMeasurement",
    "MovementState",
    "OccupancyCheckpoint",
    "OccupancySnapshot",
    "OccupancyState",
    "TemporalCheckpoint",
    "TemporalOcclusionState",
    "TemporalPolicy",
    "TemporalReason",
    "TemporalState",
    "TemporalStateKey",
    "TemporalTransition",
    "WaitingCheckpoint",
    "WaitingInterval",
    "WaitingState",
]
