"""Tests for Task 16.4 — the deterministic occupancy_session rule.

The FIRST production operational rule: converts a canonical Task 15.4
``OccupancySnapshot`` boundary into an ``occupancy_session`` started/ended
event. The rule NEVER infers occupancy from detections/boxes/frames — it
consumes the confirmed canonical fact only (Task 15.4 already stabilized
and qualified it).

Covered (Task 16.4 Part 27):

- basic: valid start, valid end, no occupancy (mid-session change),
  invalid occupancy;
- boundaries: exact qualification boundary (0 -> >0 start, >0 -> 0 end),
  just-before (candidate states emit NO snapshot), just-after (mid-session
  changes never fire);
- reliability: duplicate start/end idempotent, replay deterministic,
  restart recovery, short occlusion never creates false transitions;
- security: tenant / venue / session isolation;
- configuration: config v1 vs v2 preserved, historical replay;
- rule version: v1 resolves, unsupported version rejected;
- evidence: REQUIRED request auto-constructed with correct interval +
  provenance;
- event: EventEnvelope schema validation, deterministic identity;
- invariants (Part 28): no event without confirmed occupancy, no duplicate
  start/end per session, end cannot precede start, provenance preserved,
  replay deterministic, invalid input never becomes an event, evaluation
  never mutates temporal state;
- golden scenarios (§21-24): start, end, re-entry (four transitions, no
  merging), and noise stabilized by Task 15.

All fixtures use the REAL canonical contracts with fixed deterministic IDs
so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.app.intelligence.rules import (
    MixedScopeRuleInputError,
    UnsupportedRuleVersionError,
    build_operational_engine,
    deterministic_event_id,
)
from backend.app.intelligence.rules.occupancy_session import (
    OCCUPANCY_SESSION_EVALUATOR_ID,
)
from backend.app.intelligence.temporal import (
    OCCUPANCY_FSM,
    PRESENCE_FSM,
    OccupancyEngine,
    OccupancyInput,
    PresenceTemporalEngine,
    TemporalInput,
    occupancy_event_from_presence,
    occupancy_scope_key,
    presence_kind,
)
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    FrameId,
    RuleId,
    RuleVersion,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
)
from contracts.events import EventEnvelope, EvidenceRef
from contracts.rules import (
    OccupancySessionPayload,
    OccupancySessionPhase,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.spatial import (
    SpatialObservation,
    SpatialPointModel,
    SpatialPointPolicy,
    SpatialStatus,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    OccupancySnapshot,
    TemporalPolicy,
    TemporalStateKey,
)

# --- Fixed canonical IDs (deterministic across runs) ---
_TENANT_A = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_TENANT_B = TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001"))
_VENUE_A = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_VENUE_B = VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001"))
_SESSION = VideoSessionId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_SESSION_B = VideoSessionId(uuid.UUID("93000000-0000-0000-0000-000000000001"))
_CAMERA = CameraId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_CONFIG_V1 = ConfigurationVersionId(uuid.UUID("50000000-0000-0000-0000-000000000001"))
_CONFIG_V2 = ConfigurationVersionId(uuid.UUID("60000000-0000-0000-0000-000000000001"))
_TRACK_A = TrackId(uuid.UUID("61000000-0000-0000-0000-000000000001"))
_TRACK_B = TrackId(uuid.UUID("62000000-0000-0000-0000-000000000002"))

_RULE_ID = RuleId(RuleIdentifier.OCCUPANCY_SESSION.value)
_RULE_VERSION = RuleVersion("v1")

_BASE = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
_PROCESSED = datetime(2026, 8, 1, 10, 0, 30, tzinfo=UTC)


def _event(seconds: int) -> datetime:
    """Deterministic event-time: 10:00:00 + ``seconds`` (Part 14)."""
    from datetime import timedelta

    return _BASE + timedelta(seconds=seconds)


def _frame(seconds: int) -> FrameId:
    return FrameId(uuid.uuid5(uuid.NAMESPACE_URL, f"frame-{seconds}"))


def _scope_key(
    *,
    tenant_id: TenantId = _TENANT_A,
    venue_id: VenueId = _VENUE_A,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG_V1,
    semantic_context: str | None = "zone-lobby",
) -> TemporalStateKey:
    """The canonical occupancy scope key (aggregate track, fsm=occupancy)."""
    presence_key = TemporalStateKey(
        fsm_kind="presence",
        tenant_id=tenant_id,
        venue_id=venue_id,
        session_id=session_id,
        camera_id=camera_id,
        configuration_version_id=configuration_version_id,
        track_id=_TRACK_A,
        semantic_context=semantic_context,
    )
    return occupancy_scope_key(presence_key)


def _snapshot(
    *,
    previous_count: int,
    delta: int,
    event_time: datetime,
    occupancy_count: int | None = None,
    occupied_tracks: tuple[TrackId, ...] | None = None,
    key: TemporalStateKey | None = None,
    index: int = 0,
) -> OccupancySnapshot:
    """A canonical OccupancySnapshot fact (counts validated by contract)."""
    count = occupancy_count if occupancy_count is not None else previous_count + delta
    tracks = occupied_tracks if occupied_tracks is not None else ()
    return OccupancySnapshot(
        snapshot_id=EventId(
            uuid.uuid5(uuid.NAMESPACE_URL, f"occupancy-snapshot-{index}-{event_time.isoformat()}")
        ),
        fsm_kind="occupancy",
        key=key or _scope_key(),
        event_time=event_time,
        previous_count=previous_count,
        delta=delta,
        occupancy_count=count,
        occupied_tracks=tracks,
        source_transition_id=EventId(
            uuid.uuid5(uuid.NAMESPACE_URL, f"source-{index}-{event_time.isoformat()}")
        ),
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(snapshot: OccupancySnapshot, *, config: ConfigurationVersionId = _CONFIG_V1):
    """A canonical RuleEvaluationInput for one occupancy snapshot."""
    from contracts.rules import RuleEvaluationInput

    return RuleEvaluationInput(
        facts=(snapshot,),
        configuration={},
        configuration_version_id=config,
        rule_version=_RULE_VERSION,
        event_time=snapshot.event_time,
        processing_time=_PROCESSED,
    )


def _engine():
    """The sanctioned operational engine (occupancy_session:v1 registered)."""
    return build_operational_engine()


def _evaluate(snapshot: OccupancySnapshot, *, config: ConfigurationVersionId = _CONFIG_V1):
    engine = _engine()
    return engine.evaluate(_RULE_ID, _RULE_VERSION, _input(snapshot, config=config))


# =============================================================================
# 27. BASIC — valid start / valid end / no occupancy / invalid occupancy
# =============================================================================


class TestBasicEvaluation:
    def test_valid_occupancy_start(self) -> None:
        # Confirmed boundary 0 -> 1 (scope became occupied).
        result = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0)))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.event_type == RuleEventType.OCCUPANCY_SESSION.value
        assert result.event.event_time == _event(0)
        payload = result.event.payload
        assert payload.phase is OccupancySessionPhase.STARTED
        assert payload.occupancy_count == 1
        assert payload.tenant_id == _TENANT_A
        assert payload.venue_id == _VENUE_A
        assert payload.session_id == _SESSION
        assert payload.camera_id == _CAMERA
        assert payload.spatial_context_id == "zone-lobby"
        assert payload.occupancy_time == _event(0)
        assert payload.configuration_version_id == _CONFIG_V1
        assert payload.rule_id == RuleIdentifier.OCCUPANCY_SESSION.value
        assert payload.rule_version == "v1"

    def test_valid_occupancy_end(self) -> None:
        # Confirmed boundary 1 -> 0 (scope became unoccupied).
        result = _evaluate(
            _snapshot(
                previous_count=1,
                delta=-1,
                event_time=_event(300),
                occupied_tracks=(),
            )
        )
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        payload = result.event.payload
        assert payload.phase is OccupancySessionPhase.ENDED
        assert payload.occupancy_count == 0

    def test_no_occupancy_mid_session_change(self) -> None:
        # 1 -> 2 is a mid-session change: the scope remains occupied — the
        # session continues, so NO new session event (Part 2/15).
        result = _evaluate(
            _snapshot(
                previous_count=1,
                delta=1,
                event_time=_event(60),
                occupied_tracks=(_TRACK_A, _TRACK_B),
            )
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_invalid_occupancy_fsm_kind(self) -> None:
        # A snapshot whose key claims a non-occupancy family is INVALID —
        # never silently converted into an event (Part 14).
        bad_key = _scope_key().model_copy(update={"fsm_kind": "presence"})
        result = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0), key=bad_key))
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None
        assert "fsm_kind" in (result.reason or "")

    def test_invalid_event_time_mismatch(self) -> None:
        # The input event_time must equal the snapshot's event time (the
        # qualifying fact's instant) — never a re-stamp (Part 5/11).
        snapshot = _snapshot(previous_count=0, delta=1, event_time=_event(0))
        engine = _engine()
        from contracts.rules import RuleEvaluationInput

        inp = RuleEvaluationInput(
            facts=(snapshot,),
            configuration={},
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(999),  # mismatched
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None
        assert "event_time" in (result.reason or "")


# =============================================================================
# 27. BOUNDARIES — exact / just-before / just-after
# =============================================================================


class TestBoundaries:
    def test_exact_start_boundary(self) -> None:
        # Exactly at the qualification boundary: count 0 -> 1 fires START.
        result = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0)))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.phase is OccupancySessionPhase.STARTED

    def test_exact_end_boundary(self) -> None:
        # Exactly at the qualification boundary: count 1 -> 0 fires END.
        result = _evaluate(_snapshot(previous_count=1, delta=-1, event_time=_event(0)))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.phase is OccupancySessionPhase.ENDED

    def test_just_before_boundary_no_snapshot(self) -> None:
        # Task 15.4 emits NO snapshot for a candidate (unconfirmed) — the
        # rule can only ever see confirmed boundaries.
        engine = _occ_engine()
        policy = TemporalPolicy(
            entry_confirmation=2,
            exit_confirmation=1,
            minimum_dwell_seconds=0,
            exit_grace_seconds=0,
            occlusion_tolerance_seconds=0,
        )
        _, state, snapshots = _run_chain(
            policy,
            engine,
            timeline=(("A", "present", 0, 0),),  # only starts ENTERING
        )
        assert state.occupancy_count == 0
        assert snapshots == []  # nothing to feed — no fabricated event

    def test_just_after_boundary_mid_session_no_match(self) -> None:
        # 2 -> 1 (someone left but scope still occupied): NOT an end.
        result = _evaluate(
            _snapshot(
                previous_count=2,
                delta=-1,
                event_time=_event(60),
                occupied_tracks=(_TRACK_A,),
            )
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH


# =============================================================================
# 27. RELIABILITY — duplicate / replay / restart / short occlusion
# =============================================================================


class TestReliability:
    def test_duplicate_start_is_idempotent(self) -> None:
        snapshot = _snapshot(previous_count=0, delta=1, event_time=_event(0))
        first = _evaluate(snapshot)
        second = _evaluate(snapshot)
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()
        assert first.event is not None and second.event is not None
        assert first.event.event_id == second.event.event_id  # one logical event

    def test_duplicate_end_is_idempotent(self) -> None:
        snapshot = _snapshot(previous_count=1, delta=-1, event_time=_event(300))
        first = _evaluate(snapshot)
        second = _evaluate(snapshot)
        assert first == second
        assert first.event is not None and second.event is not None
        assert first.event.event_id == second.event.event_id

    def test_replay_is_deterministic(self) -> None:
        snapshot = _snapshot(previous_count=0, delta=1, event_time=_event(0))
        engine = _engine()
        inp = _input(snapshot)
        r1 = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        serialized = r1.model_dump_json()
        r2 = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert r2.model_dump_json() == serialized
        assert r1 == r2

    def test_restart_recovery(self) -> None:
        # Rebuild the engine from scratch (restart) — the evaluation is
        # byte-identical to the uninterrupted one (Part 23 restart test).
        snapshot = _snapshot(previous_count=1, delta=-1, event_time=_event(300))
        before = _evaluate(snapshot).model_dump_json()
        after = _evaluate(snapshot).model_dump_json()
        assert after == before

    def test_short_occlusion_never_creates_false_transitions(self) -> None:
        # Task 15.4 keeps occupancy at 1 across short occlusion — the ONLY
        # snapshot is the original enter, so the rule emits exactly one
        # START and never an END (Part 16/17).
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
        assert len(snapshots) == 1
        result = _evaluate(snapshots[0])
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.phase is OccupancySessionPhase.STARTED
        # Re-evaluating the same (only) snapshot stays one logical event.
        assert _evaluate(snapshots[0]).event is not None
        assert _evaluate(snapshots[0]).event.event_id == result.event.event_id


# =============================================================================
# 27. SECURITY — tenant / venue / session isolation
# =============================================================================


class TestSecurityIsolation:
    def test_tenant_isolation(self) -> None:
        a = _evaluate(
            _snapshot(
                previous_count=0,
                delta=1,
                event_time=_event(0),
                key=_scope_key(tenant_id=_TENANT_A),
            )
        )
        b = _evaluate(
            _snapshot(
                previous_count=0,
                delta=1,
                event_time=_event(0),
                key=_scope_key(tenant_id=_TENANT_B),
            )
        )
        assert a.event is not None and b.event is not None
        assert a.event.payload.tenant_id == _TENANT_A
        assert b.event.payload.tenant_id == _TENANT_B
        assert a.event.event_id != b.event.event_id  # never shared

    def test_venue_isolation(self) -> None:
        a = _evaluate(
            _snapshot(
                previous_count=0, delta=1, event_time=_event(0), key=_scope_key(venue_id=_VENUE_A)
            )
        )
        b = _evaluate(
            _snapshot(
                previous_count=0, delta=1, event_time=_event(0), key=_scope_key(venue_id=_VENUE_B)
            )
        )
        assert a.event is not None and b.event is not None
        assert a.event.payload.venue_id == _VENUE_A
        assert b.event.payload.venue_id == _VENUE_B

    def test_session_isolation(self) -> None:
        a = _evaluate(
            _snapshot(
                previous_count=0,
                delta=1,
                event_time=_event(0),
                key=_scope_key(session_id=_SESSION),
            )
        )
        b = _evaluate(
            _snapshot(
                previous_count=0,
                delta=1,
                event_time=_event(0),
                key=_scope_key(session_id=_SESSION_B),
            )
        )
        assert a.event is not None and b.event is not None
        assert a.event.payload.session_id == _SESSION
        assert b.event.payload.session_id == _SESSION_B
        assert a.event.event_id != b.event.event_id

    def test_cross_scope_facts_rejected(self) -> None:
        # Two snapshots from different tenants in ONE input → typed
        # rejection (Task 16.2 Part 18), never a cross-tenant event.
        engine = _engine()
        from contracts.rules import RuleEvaluationInput

        snap_a = _snapshot(
            previous_count=0, delta=1, event_time=_event(0), key=_scope_key(tenant_id=_TENANT_A)
        )
        snap_b = _snapshot(
            previous_count=0, delta=1, event_time=_event(0), key=_scope_key(tenant_id=_TENANT_B)
        )
        inp = RuleEvaluationInput(
            facts=(snap_a, snap_b),
            configuration={},
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(0),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)


# =============================================================================
# 27. CONFIGURATION — config v1 vs v2, historical replay
# =============================================================================


class TestConfiguration:
    def test_configuration_version_preserved(self) -> None:
        snapshot = _snapshot(previous_count=0, delta=1, event_time=_event(0))
        r1 = _evaluate(snapshot, config=_CONFIG_V1)
        r2 = _evaluate(snapshot, config=_CONFIG_V2)
        assert r1.configuration_version_id == _CONFIG_V1
        assert r2.configuration_version_id == _CONFIG_V2
        assert r1.event is not None and r2.event is not None
        # Config version participates in the deterministic identity — the
        # events are distinct, both reproducible.
        assert r1.event.event_id != r2.event.event_id

    def test_historical_replay_config_v1(self) -> None:
        # Re-evaluating with config:v1 AFTER v2 exists yields the identical
        # v1 result (Part 12/20) — never the latest configuration.
        snapshot = _snapshot(previous_count=1, delta=-1, event_time=_event(300))
        v1_first = _evaluate(snapshot, config=_CONFIG_V1)
        _ = _evaluate(snapshot, config=_CONFIG_V2)  # v2 exists now
        v1_replay = _evaluate(snapshot, config=_CONFIG_V1)
        assert v1_replay.model_dump_json() == v1_first.model_dump_json()
        assert v1_replay.configuration_version_id == _CONFIG_V1


# =============================================================================
# 27. RULE VERSION — v1 resolves, unsupported rejected
# =============================================================================


class TestRuleVersion:
    def test_v1_resolves(self) -> None:
        engine = _engine()
        rule = engine._registry.resolve(_RULE_ID, _RULE_VERSION)
        assert rule.canonical_identity == "occupancy_session:v1"
        assert rule.evaluator_id == OCCUPANCY_SESSION_EVALUATOR_ID

    def test_unsupported_version_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(
                _RULE_ID,
                RuleVersion("v9"),
                _input(_snapshot(previous_count=0, delta=1, event_time=_event(0))),
            )


# =============================================================================
# 27. EVIDENCE — REQUIRED request with correct interval + provenance
# =============================================================================


class TestEvidence:
    def test_match_requests_evidence(self) -> None:
        result = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0)))
        assert result.status is RuleEvaluationStatus.MATCH
        assert len(result.evidence_requests) == 1
        ref = result.evidence_requests[0]
        assert isinstance(ref, EvidenceRef)
        # Part 10: the request preserves session, source, event-time
        # interval, configuration version, provenance.
        assert ref.metadata is not None
        assert ref.metadata["tenant_id"] == str(_TENANT_A)
        assert ref.metadata["venue_id"] == str(_VENUE_A)
        assert ref.metadata["session_id"] == str(_SESSION)
        assert ref.metadata["camera_id"] == str(_CAMERA)
        assert ref.metadata["event_time"] == _event(0).isoformat()
        assert ref.metadata["configuration_version_id"] == str(_CONFIG_V1)
        assert ref.metadata["rule_id"] == RuleIdentifier.OCCUPANCY_SESSION.value
        assert ref.metadata["rule_version"] == "v1"
        assert EvidenceRef.model_validate(ref.model_dump(mode="json")) == ref

    def test_no_match_no_evidence(self) -> None:
        result = _evaluate(
            _snapshot(
                previous_count=1,
                delta=1,
                event_time=_event(60),
                occupied_tracks=(_TRACK_A, _TRACK_B),
            )
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.evidence_requests == ()


# =============================================================================
# 27. EVENT — EventEnvelope schema + deterministic identity
# =============================================================================


class TestEventContract:
    def test_envelope_serializes_round_trip(self) -> None:
        result = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0)))
        assert result.event is not None
        serialized = result.event.model_dump(mode="json")
        restored = EventEnvelope.model_validate(serialized)
        # Generic envelope metadata survives byte-exact; the typed payload
        # re-validates against its canonical model (frozen, forbid-extra).
        assert restored.event_id == result.event.event_id
        assert restored.event_type == RuleEventType.OCCUPANCY_SESSION.value
        assert restored.event_time == _event(0)
        assert restored.schema_version == "1.0"
        payload = OccupancySessionPayload.model_validate(restored.payload)
        assert payload == result.event.payload

    def test_deterministic_event_identity(self) -> None:
        snapshot = _snapshot(previous_count=0, delta=1, event_time=_event(0))
        engine = _engine()
        inp = _input(snapshot)
        rule = engine._registry.resolve(_RULE_ID, _RULE_VERSION)
        expected = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.event is not None
        assert result.event.event_id == expected


# =============================================================================
# 28. PROPERTY / INVARIANT TESTS
# =============================================================================


class TestInvariants:
    def test_no_event_without_confirmed_occupancy(self) -> None:
        # A candidate-only timeline produces no snapshot → no event.
        engine = _occ_engine()
        _, state, snapshots = _run_chain(
            _quick_policy(entry_confirmation=2),
            engine,
            timeline=(("A", "present", 0, 0), ("A", "absent", 1, 1)),
        )
        assert state.occupancy_count == 0
        assert snapshots == []

    def test_no_duplicate_start_for_one_session(self) -> None:
        # One logical session (0->1, then mid-session 1->2) fires exactly
        # one START — the mid-session change never duplicates it.
        start = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0)))
        mid = _evaluate(
            _snapshot(
                previous_count=1,
                delta=1,
                event_time=_event(60),
                occupied_tracks=(_TRACK_A, _TRACK_B),
            )
        )
        assert start.status is RuleEvaluationStatus.MATCH
        assert mid.status is RuleEvaluationStatus.NO_MATCH

    def test_no_duplicate_end_for_one_session(self) -> None:
        end = _evaluate(_snapshot(previous_count=1, delta=-1, event_time=_event(300)))
        mid = _evaluate(
            _snapshot(
                previous_count=2, delta=-1, event_time=_event(240), occupied_tracks=(_TRACK_A,)
            )
        )
        assert end.status is RuleEvaluationStatus.MATCH
        assert mid.status is RuleEvaluationStatus.NO_MATCH

    def test_end_cannot_logically_precede_start(self) -> None:
        # A full timeline produces the phase sequence START then END —
        # never an END before a START for the same scope.
        engine = _occ_engine()
        _, _, snapshots = _run_chain(
            _quick_policy(),
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("A", "absent", 5, 5),
            ),
        )
        phases = [
            _evaluate(s).event.payload.phase
            for s in snapshots
            if _evaluate(s).status is RuleEvaluationStatus.MATCH
        ]
        assert phases == [OccupancySessionPhase.STARTED, OccupancySessionPhase.ENDED]

    def test_rule_version_preserved(self) -> None:
        result = _evaluate(_snapshot(previous_count=0, delta=1, event_time=_event(0)))
        assert result.rule_version == "v1"
        assert result.rule_id == RuleIdentifier.OCCUPANCY_SESSION.value

    def test_tenant_venue_identity_preserved(self) -> None:
        result = _evaluate(
            _snapshot(
                previous_count=0,
                delta=1,
                event_time=_event(0),
                key=_scope_key(tenant_id=_TENANT_A, venue_id=_VENUE_A),
            )
        )
        assert result.tenant_id == _TENANT_A
        assert result.venue_id == _VENUE_A
        assert result.session_id == _SESSION

    def test_invalid_input_never_becomes_event(self) -> None:
        bad = _snapshot(
            previous_count=0,
            delta=1,
            event_time=_event(0),
            key=_scope_key().model_copy(update={"fsm_kind": "dwell"}),
        )
        result = _evaluate(bad)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None  # never emitted

    def test_evaluation_does_not_mutate_temporal_state(self) -> None:
        snapshot = _snapshot(previous_count=0, delta=1, event_time=_event(0))
        engine = _engine()
        inp = _input(snapshot)
        before = inp.model_dump_json()
        evaluators_before = [e.evaluator_id for e in engine._evaluators.list()]
        rules_before = [r.canonical_identity for r in engine._registry.list()]
        engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert inp.model_dump_json() == before  # input untouched
        # Registries untouched by evaluation (no new evaluators/rules).
        assert [e.evaluator_id for e in engine._evaluators.list()] == evaluators_before
        assert [r.canonical_identity for r in engine._registry.list()] == rules_before


# =============================================================================
# §21-24. GOLDEN SCENARIOS
# =============================================================================


class TestGoldenScenarios:
    def test_golden_valid_start(self) -> None:
        """§21: candidate observations, then confirmed occupancy, then
        occupied — exactly ONE start event, no duplicate starts."""
        engine = _occ_engine()
        policy = _quick_policy(entry_confirmation=2)
        _, _, snapshots = _run_chain(
            policy,
            engine,
            timeline=(
                ("A", "present", 0, 0),  # 10:00 — first observation (candidate)
                ("A", "present", 2, 2),  # 10:02 — confirmed occupancy
                ("A", "present", 3, 3),  # 10:03 — occupied (stay)
            ),
        )
        # The candidate produced no snapshot; only the confirmed enter did.
        assert len(snapshots) == 1
        result = _evaluate(snapshots[0])
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.phase is OccupancySessionPhase.STARTED
        assert result.event.event_time == _event(2)  # the confirmed instant
        # Re-evaluating the same snapshot never duplicates the start.
        assert _evaluate(snapshots[0]).event.event_id == result.event.event_id

    def test_golden_valid_end(self) -> None:
        """§22: 10:00/10:01 occupied, 10:02 candidate exit, 10:03 confirmed
        exit → exactly ONE end event."""
        engine = _occ_engine()
        policy = _quick_policy(exit_confirmation=2)
        _, _, snapshots = _run_chain(
            policy,
            engine,
            timeline=(
                ("A", "present", 0, 0),
                ("A", "present", 1, 1),
                ("A", "absent", 2, 2),  # 10:02 — candidate exit (EXITING)
                ("A", "absent", 3, 3),  # 10:03 — confirmed exit
            ),
        )
        assert [s.occupancy_count for s in snapshots] == [1, 0]
        start = _evaluate(snapshots[0])
        end = _evaluate(snapshots[1])
        assert start.status is RuleEvaluationStatus.MATCH
        assert end.status is RuleEvaluationStatus.MATCH
        assert start.event.payload.phase is OccupancySessionPhase.STARTED
        assert end.event.payload.phase is OccupancySessionPhase.ENDED
        assert end.event.event_time == _event(3)
        assert end.event.event_id != start.event.event_id

    def test_golden_reentry_two_independent_sessions(self) -> None:
        """§23: Session A (10:00-10:05) + Session B (10:10-10:15) → four
        logical transitions, no merging."""
        engine = _occ_engine()
        _, _, snapshots = _run_chain(
            _quick_policy(),
            engine,
            timeline=(
                ("A", "present", 0, 0),  # A start
                ("A", "absent", 5, 5),  # A end
                ("A", "present", 10, 10),  # B start
                ("A", "absent", 15, 15),  # B end
            ),
        )
        assert len(snapshots) == 4
        results = [_evaluate(s) for s in snapshots]
        assert [r.status for r in results] == [RuleEvaluationStatus.MATCH] * 4
        phases = [r.event.payload.phase for r in results]
        assert phases == [
            OccupancySessionPhase.STARTED,
            OccupancySessionPhase.ENDED,
            OccupancySessionPhase.STARTED,
            OccupancySessionPhase.ENDED,
        ]
        ids = [r.event.event_id for r in results]
        assert len(set(ids)) == 4  # four distinct logical events — no merging

    def test_golden_noise_only_stable_events(self) -> None:
        """§24: noisy underlying observations stabilized by Task 15 → the
        rule emits ONLY the stable operational events."""
        engine = _occ_engine()
        policy = TemporalPolicy(
            entry_confirmation=2,  # flicker never confirms
            exit_confirmation=2,
            minimum_dwell_seconds=0,
            exit_grace_seconds=0,
            occlusion_tolerance_seconds=0,
        )
        _, state, snapshots = _run_chain(
            policy,
            engine,
            timeline=(
                ("A", "present", 0, 0),  # noise: starts ENTERING
                ("A", "absent", 1, 1),  # noise: flicker back
                ("A", "present", 2, 2),  # starts ENTERING again
                ("A", "present", 3, 3),  # CONFIRMED — real start
                ("A", "not_observed", 4, 4),  # short occlusion (covered)
                ("A", "present", 5, 5),  # still present
                ("A", "absent", 10, 10),  # starts EXITING
                ("A", "absent", 11, 11),  # CONFIRMED — real end
            ),
        )
        # The stable timeline: one enter + one exit → two snapshots.
        assert state.occupancy_count == 0
        assert [s.occupancy_count for s in snapshots] == [1, 0]
        results = [_evaluate(s) for s in snapshots]
        assert [r.status for r in results] == [RuleEvaluationStatus.MATCH] * 2
        assert [r.event.payload.phase for r in results] == [
            OccupancySessionPhase.STARTED,
            OccupancySessionPhase.ENDED,
        ]


# =============================================================================
# Task 15.4 chain helpers (canonical facts only, reused pattern)
# =============================================================================


def _status_obs(
    key: TemporalStateKey, *, status: SpatialStatus, event_time: datetime, frame_id: FrameId
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
    key: TemporalStateKey, *, kind: str, event_time: datetime, frame_id: FrameId
) -> SpatialObservation:
    if kind == "present":
        status = SpatialStatus.INSIDE
    elif kind == "absent":
        status = SpatialStatus.OUTSIDE
    else:  # not_observed
        status = SpatialStatus.EXCLUDED
    return _status_obs(key, status=status, event_time=event_time, frame_id=frame_id)


def _presence_input(key: TemporalStateKey, obs: SpatialObservation) -> TemporalInput:
    return TemporalInput(
        key=key, observation=obs, observation_kind=presence_kind(obs), processing_time=_PROCESSED
    )


def _quick_policy(**kwargs) -> TemporalPolicy:
    base = TemporalPolicy(
        entry_confirmation=1,
        exit_confirmation=1,
        minimum_dwell_seconds=0,
        exit_grace_seconds=0,
    )
    return base.model_copy(update=kwargs)


def _occ_engine(policy: TemporalPolicy | None = None, **kwargs) -> OccupancyEngine:
    return OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy or _quick_policy(**kwargs))


def _track_key(*, track_id: TrackId = _TRACK_A, **scope) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind="presence",
        tenant_id=scope.get("tenant_id", _TENANT_A),
        venue_id=scope.get("venue_id", _VENUE_A),
        session_id=scope.get("session_id", _SESSION),
        camera_id=scope.get("camera_id", _CAMERA),
        configuration_version_id=scope.get("configuration_version_id", _CONFIG_V1),
        track_id=track_id,
        semantic_context=scope.get("semantic_context", "zone-lobby"),
    )


def _run_chain(
    presence_policy: TemporalPolicy,
    occ_engine: OccupancyEngine,
    *,
    timeline: tuple[tuple[str, str, int, int], ...],
) -> tuple[dict[str, object], object, list[OccupancySnapshot]]:
    """Run the canonical chain (SpatialObservation -> presence -> occupancy)
    and collect the emitted snapshots."""
    presence = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=presence_policy)
    pstates: dict[str, object] = {}
    scope_key: TemporalStateKey | None = None
    occ_state: object | None = None
    snapshots: list[OccupancySnapshot] = []
    tracks = {"A": _TRACK_A, "B": _TRACK_B}
    for label, kind, seconds, frame_index in timeline:
        pkey = _track_key(track_id=tracks[label])
        if scope_key is None:
            scope_key = occupancy_scope_key(pkey)
            assert isinstance(occ_engine, OccupancyEngine)
            occ_state = occ_engine.initial_state(scope_key)
        pstate = pstates.get(label)
        if pstate is None:
            assert isinstance(presence, PresenceTemporalEngine)
            pstate = presence.initial_state(pkey)
        obs = _obs(pkey, kind=kind, event_time=_event(seconds), frame_id=_frame(frame_index))
        presence_result = presence.apply(pstate, _presence_input(pkey, obs))
        pstates[label] = presence_result.state
        assert (
            scope_key is not None
            and occ_state is not None
            and isinstance(occ_engine, OccupancyEngine)
        )
        occ_result = occ_engine.apply(
            occ_state,
            OccupancyInput(
                key=scope_key,
                transition=presence_result.transitions[0],
                observation_kind=occupancy_event_from_presence(presence_result.transitions[0]),
                processing_time=_PROCESSED,
            ),
        )
        occ_state = occ_result.state
        if occ_result.snapshot is not None:
            snapshots.append(occ_result.snapshot)
    assert scope_key is not None and occ_state is not None
    return pstates, occ_state, snapshots
