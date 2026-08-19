"""Task 18.8 — Task 16 occupancy rule integration (vertical slice).

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 15
chain (18.7) AND the REGISTERED Task 16 rule:

    SpatialObservation → PresenceTemporalEngine → OccupancyEngine
        → OccupancySnapshot (the confirmed Task 15.4 fact)
        → RuleEvaluationEngine (occupancy_session:v1 — the Task 16.4 rule
          registered by ``build_operational_engine``)
        → EventEnvelope

No new rule is implemented here: the slice resolves the packaged
``occupancy_session:v1`` rule from the operational engine's registry and
evaluates the fixture's confirmed occupancy snapshots through it. The
fixture produces exactly two confirmed boundaries — enter (0 -> 1, the
exact start threshold) and exit (1 -> 0, the exact end threshold) — so
the slice yields exactly two logical events.

Verified here (the task's list, each against the fixture's pinned
identity block):

- rule_id               → the registered ``occupancy_session`` rule id;
- rule_version          → ``v1`` (the registered version), preserved on
                          the result AND the payload;
- configuration_version → the manifest's pinned configuration version
                          (participates in the event identity);
- tenant_id / venue_id / session_id → preserved from the fact's scope;
- source_id             → the envelope's canonical ``source``
                          (``rule:occupancy_session:v1``);
- event_time            → the qualifying snapshot's event time (never
                          processing time);
- deterministic event identity → the same fixture + same versions always
                          produce the same logical event (byte-exact).

Tests (the task's list):

1. normal event          → the fixture's enter/exit snapshots become
                           STARTED / ENDED envelopes with full provenance;
2. threshold boundary    → the exact 0 -> 1 and 1 -> 0 boundaries fire;
                           mid-session changes (1 -> 2, 2 -> 1) never
                           fire; no snapshot → no event;
3. duplicate evaluation  → re-evaluating the same snapshot is idempotent
                           (one logical event, byte-identical result);
4. replay                → re-running the whole slice from scratch (fresh
                           engines) reproduces the same logical events,
                           even under a different processing time;
5. different rule version→ ``v1`` resolves; an unsupported version is a
                           typed error (never a silent fallback);
6. different configuration version → the config version participates in
                           the deterministic identity — each evaluation is
                           distinct yet reproducible.

STOP-condition: the event is deterministic — the event_id is content-
derived, so the same fixture + same versions can never emit a second,
different logical event.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.intelligence.rules import (
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
from contracts.events import EventEnvelope
from contracts.geometry import CoordinateSpace
from contracts.rules import (
    OccupancySessionPayload,
    OccupancySessionPhase,
    RuleEvaluationInput,
    RuleEvaluationResult,
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

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = FIXTURES_DIR / "manifest.json"

# The fixture's on-frame interval (manifest trajectory constants).
BOUNDARY_FRAME = 6
INSIDE_FROM = 7
GONE_FROM = 28
EXIT_CONFIRM_FRAMES = (GONE_FROM, GONE_FROM + 1, GONE_FROM + 2)

RULE_ID = RuleId(RuleIdentifier.OCCUPANCY_SESSION.value)
RULE_VERSION = RuleVersion("v1")

# A second configuration version for the identity-participation tests.
_CONFIG_V2 = ConfigurationVersionId(uuid.UUID("55555555-5555-4555-8555-555555555555"))


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _uuid(seed: str) -> UUID:
    """Deterministic content-derived id (same namespace as the engines)."""
    return uuid.uuid5(uuid.uuid5(UUID(int=0), "hotelops-slice"), seed)


def _event_at(manifest: dict, frame_index: int) -> datetime:
    """Fixture event time: capture + frame_index / FPS (never wall clock)."""
    meta = manifest["metadata"]
    capture = datetime.fromisoformat(meta["capture_time"])
    return capture + timedelta(seconds=frame_index / meta["fps"])


def _processing(manifest: dict) -> datetime:
    """Caller-supplied processing metadata — always AFTER the recording."""
    return _event_at(manifest, 0) + timedelta(hours=1)


def _identities(manifest: dict) -> dict:
    """The pinned identity block: the manifest's one published version."""
    spatial = manifest["spatial"]
    return {
        "tenant_id": TenantId(UUID(spatial["tenant_id"])),
        "venue_id": VenueId(UUID(spatial["venue_id"])),
        "session_id": VideoSessionId(_uuid("slice-session")),
        "camera_id": CameraId(UUID(spatial["camera_id"])),
        "configuration_version_id": ConfigurationVersionId(
            UUID(spatial["configuration_version_id"])
        ),
        "track_id": TrackId(_uuid("track-person-001")),
        "semantic_context": spatial["zone_profile_id"],
    }


def _presence_key(ids: dict) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind="presence",
        tenant_id=ids["tenant_id"],
        venue_id=ids["venue_id"],
        session_id=ids["session_id"],
        camera_id=ids["camera_id"],
        configuration_version_id=ids["configuration_version_id"],
        track_id=ids["track_id"],
        semantic_context=ids["semantic_context"],
    )


def _point(manifest: dict, frame_index: int) -> SpatialPointModel:
    timeline = manifest["timeline"]
    if 0 <= frame_index < len(timeline):
        golden = timeline[frame_index].get("spatial_point")
        if golden is not None:
            return SpatialPointModel(
                x=golden["x"],
                y=golden["y"],
                coordinate_space=CoordinateSpace.VENUE_LOCAL,
                policy=SpatialPointPolicy.CENTROID,
            )
    return SpatialPointModel(
        x=0.0,
        y=0.0,
        coordinate_space=CoordinateSpace.VENUE_LOCAL,
        policy=SpatialPointPolicy.FOOTPOINT,
    )


def _observation(
    manifest: dict,
    ids: dict,
    *,
    frame_index: int,
    status: SpatialStatus,
) -> SpatialObservation:
    return SpatialObservation(
        session_id=ids["session_id"],
        track_id=ids["track_id"],
        frame_id=FrameId(_uuid(f"frame-{frame_index}")),
        event_time=_event_at(manifest, frame_index),
        camera_id=ids["camera_id"],
        configuration_version_id=ids["configuration_version_id"],
        spatial_point=_point(manifest, frame_index),
        status=status,
    )


def _fixture_stream(manifest: dict, ids: dict) -> list[tuple[int, SpatialObservation]]:
    """The canonical slice stream (same as Task 18.7):

    - frame 6:   not_observed (the boundary blocker instant);
    - frames 7..27: present (INSIDE — the 18.6 output);
    - frames 28..30: absent (exit confirmation completes at fixture cadence).
    """
    stream: list[tuple[int, SpatialObservation]] = [
        (
            BOUNDARY_FRAME,
            _observation(manifest, ids, frame_index=BOUNDARY_FRAME, status=SpatialStatus.EXCLUDED),
        )
    ]
    for frame_index in range(INSIDE_FROM, GONE_FROM):
        stream.append((
            frame_index,
            _observation(manifest, ids, frame_index=frame_index, status=SpatialStatus.INSIDE),
        ))
    for frame_index in EXIT_CONFIRM_FRAMES:
        stream.append((
            frame_index,
            _observation(manifest, ids, frame_index=frame_index, status=SpatialStatus.OUTSIDE),
        ))
    return stream


def _slice_policy(**kwargs: object) -> TemporalPolicy:
    base = TemporalPolicy(
        revision="v1",
        reorder_window_seconds=5.0,
        entry_confirmation=2,
        exit_confirmation=2,
        minimum_dwell_seconds=1.0,
        exit_grace_seconds=0.15,
        occlusion_tolerance_seconds=0.5,
    )
    return base.model_copy(update=kwargs)


def _run_occupancy_chain(
    manifest: dict,
    ids: dict,
    *,
    stream: list[tuple[int, SpatialObservation]] | None = None,
    policy: TemporalPolicy | None = None,
) -> tuple[list[OccupancySnapshot], object]:
    """The 18.7 chain: fixture stream → presence → occupancy snapshots."""
    policy = policy or _slice_policy()
    presence = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy)
    occupancy = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy)
    pkey = _presence_key(ids)
    scope = occupancy_scope_key(pkey)
    pstate = presence.initial_state(pkey)
    ostate = occupancy.initial_state(scope)
    processing = _processing(manifest)

    snapshots: list[OccupancySnapshot] = []
    for _frame_index, obs in stream or _fixture_stream(manifest, ids):
        presence_result = presence.apply(
            pstate,
            TemporalInput(
                key=pkey,
                observation=obs,
                observation_kind=presence_kind(obs),
                processing_time=processing,
            ),
        )
        pstate = presence_result.state
        occ_result = occupancy.apply(
            ostate,
            OccupancyInput(
                key=scope,
                transition=presence_result.transitions[0],
                observation_kind=occupancy_event_from_presence(presence_result.transitions[0]),
                processing_time=processing,
            ),
        )
        ostate = occ_result.state
        if occ_result.snapshot is not None:
            snapshots.append(occ_result.snapshot)
    return snapshots, ostate


def _evaluate_snapshots(
    manifest: dict,
    ids: dict,
    snapshots: list[OccupancySnapshot],
    *,
    config_version: ConfigurationVersionId | None = None,
    rule_version: str = "v1",
    processing_time: datetime | None = None,
) -> list[RuleEvaluationResult]:
    """Evaluate every confirmed fact through the REGISTERED Task 16 rule."""
    engine = build_operational_engine()
    results: list[RuleEvaluationResult] = []
    for snapshot in snapshots:
        inp = RuleEvaluationInput(
            facts=(snapshot,),
            configuration={},
            configuration_version_id=config_version or ids["configuration_version_id"],
            rule_version=RuleVersion(rule_version),
            event_time=snapshot.event_time,
            processing_time=processing_time or _processing(manifest),
        )
        results.append(engine.evaluate(RULE_ID, RuleVersion(rule_version), inp))
    return results


@dataclass(frozen=True, slots=True)
class _SliceOutcome:
    """The full 18.8 slice: confirmed facts + evaluated events."""

    snapshots: list[OccupancySnapshot]
    results: list[RuleEvaluationResult]

    @property
    def events(self) -> list[EventEnvelope]:
        return [r.event for r in self.results if r.status is RuleEvaluationStatus.MATCH]


def _run_full_slice(
    manifest: dict,
    ids: dict,
    *,
    processing_time: datetime | None = None,
) -> _SliceOutcome:
    """The complete vertical slice: fixture → Task 15 facts → Task 16 events."""
    snapshots, _ostate = _run_occupancy_chain(manifest, ids)
    results = _evaluate_snapshots(manifest, ids, snapshots, processing_time=processing_time)
    return _SliceOutcome(snapshots=snapshots, results=results)


def _snapshot(
    *,
    previous_count: int,
    delta: int,
    event_time: datetime,
    ids: dict,
    index: int = 0,
) -> OccupancySnapshot:
    """A canonical confirmed OccupancySnapshot fact (deterministic ids)."""
    count = previous_count + delta
    tracks = () if count == 0 else (ids["track_id"],)
    return OccupancySnapshot(
        snapshot_id=EventId(_uuid(f"occupancy-snapshot-{index}-{event_time.isoformat()}")),
        fsm_kind="occupancy",
        key=occupancy_scope_key(_presence_key(ids)),
        event_time=event_time,
        previous_count=previous_count,
        delta=delta,
        occupancy_count=count,
        occupied_tracks=tracks,
        source_transition_id=EventId(_uuid(f"source-{index}-{event_time.isoformat()}")),
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


# =============================================================================
# 1. NORMAL EVENT — the fixture's confirmed boundaries become envelopes
# =============================================================================


class TestNormalEvent:
    """The expected slice: exactly one STARTED and one ENDED logical event."""

    def test_fixture_yields_two_logical_events(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        # The 18.7 chain produced exactly two confirmed boundaries.
        assert [s.previous_count for s in outcome.snapshots] == [0, 1]
        assert [s.occupancy_count for s in outcome.snapshots] == [1, 0]
        # Both fire through the registered rule.
        assert [r.status for r in outcome.results] == [
            RuleEvaluationStatus.MATCH,
            RuleEvaluationStatus.MATCH,
        ]
        events = outcome.events
        assert len(events) == 2
        assert [e.event_type for e in events] == [RuleEventType.OCCUPANCY_SESSION.value] * 2
        assert [e.payload.phase for e in events] == [
            OccupancySessionPhase.STARTED,
            OccupancySessionPhase.ENDED,
        ]

    def test_started_event_preserves_full_provenance(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        # rule_id + rule_version (verified on the result AND the payload).
        assert outcome.results[0].rule_id == RuleIdentifier.OCCUPANCY_SESSION.value
        assert outcome.results[0].rule_version == "v1"
        payload: OccupancySessionPayload = event.payload
        assert payload.rule_id == RuleIdentifier.OCCUPANCY_SESSION.value
        assert payload.rule_version == "v1"
        # configuration_version (the manifest's ONE published version).
        assert outcome.results[0].configuration_version_id == ids["configuration_version_id"]
        assert payload.configuration_version_id == ids["configuration_version_id"]
        # tenant / venue / session from the fact's scope.
        assert payload.tenant_id == ids["tenant_id"]
        assert payload.venue_id == ids["venue_id"]
        assert payload.session_id == ids["session_id"]
        # source_id: the canonical envelope source of the registered rule.
        assert event.source == "rule:occupancy_session:v1"
        # event_time == the qualifying fact's instant (frame 8 = the confirmed enter).
        assert event.event_time == _event_at(manifest, 8)
        assert event.event_time == outcome.snapshots[0].event_time
        # Deterministic event identity (content-derived UUID).
        assert isinstance(event.event_id, UUID)
        # Payload detail: the boundary semantics + the ROI context.
        assert payload.phase is OccupancySessionPhase.STARTED
        assert payload.occupancy_count == 1
        assert payload.occupied_tracks == (ids["track_id"],)
        assert payload.occupancy_time == event.event_time
        assert payload.camera_id == ids["camera_id"]
        assert payload.spatial_context_id == ids["semantic_context"]
        # The result mirrors the scope identity.
        assert outcome.results[0].tenant_id == ids["tenant_id"]
        assert outcome.results[0].venue_id == ids["venue_id"]
        assert outcome.results[0].session_id == ids["session_id"]

    def test_ended_event_uses_the_confirmed_exit_instant(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[1]
        assert event.payload.phase is OccupancySessionPhase.ENDED
        assert event.payload.occupancy_count == 0
        assert event.event_time == _event_at(manifest, EXIT_CONFIRM_FRAMES[-1])
        assert event.event_time == outcome.snapshots[1].event_time
        assert event.event_id != outcome.events[0].event_id  # two distinct events


# =============================================================================
# 2. THRESHOLD BOUNDARY — exact 0 -> 1 and 1 -> 0 fire; mid-session never
# =============================================================================


class TestThresholdBoundary:
    def test_exact_start_boundary_fires(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        # The fixture's confirmed enter IS the exact 0 -> 1 boundary.
        result = _evaluate_snapshots(
            manifest,
            ids,
            [_snapshot(previous_count=0, delta=1, event_time=_event_at(manifest, 8), ids=ids)],
        )[0]
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.phase is OccupancySessionPhase.STARTED

    def test_exact_end_boundary_fires(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        result = _evaluate_snapshots(
            manifest,
            ids,
            [_snapshot(previous_count=1, delta=-1, event_time=_event_at(manifest, 30), ids=ids)],
        )[0]
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.phase is OccupancySessionPhase.ENDED

    def test_mid_session_changes_never_fire(self) -> None:
        """1 -> 2 and 2 -> 1 are mid-session changes: the scope is still
        occupied — NO session event (the session continues)."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        results = _evaluate_snapshots(
            manifest,
            ids,
            [
                _snapshot(
                    previous_count=1, delta=1, event_time=_event_at(manifest, 12), ids=ids, index=0
                ),
                _snapshot(
                    previous_count=2, delta=-1, event_time=_event_at(manifest, 20), ids=ids, index=1
                ),
            ],
        )
        assert [r.status for r in results] == [
            RuleEvaluationStatus.NO_MATCH,
            RuleEvaluationStatus.NO_MATCH,
        ]
        assert all(r.event is None for r in results)

    def test_no_snapshot_means_no_event(self) -> None:
        """Before the confirmed enter there is NO snapshot (Task 15.4 emits
        none for unconfirmed candidates) — nothing to evaluate, no event."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        snapshots, _ = _run_occupancy_chain(manifest, ids, stream=stream[:2])  # frames 6..7
        assert snapshots == []
        assert _evaluate_snapshots(manifest, ids, snapshots) == []


# =============================================================================
# 3. DUPLICATE EVALUATION — idempotent, one logical event
# =============================================================================


class TestDuplicateEvaluation:
    def test_duplicate_start_is_one_logical_event(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        snapshots, _ = _run_occupancy_chain(manifest, ids)
        enter = snapshots[0]
        first = _evaluate_snapshots(manifest, ids, [enter])[0]
        second = _evaluate_snapshots(manifest, ids, [enter])[0]
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()
        assert first.event is not None and second.event is not None
        assert first.event.event_id == second.event.event_id  # ONE logical event

    def test_duplicate_end_is_one_logical_event(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        snapshots, _ = _run_occupancy_chain(manifest, ids)
        exit_snapshot = snapshots[1]
        first = _evaluate_snapshots(manifest, ids, [exit_snapshot])[0]
        second = _evaluate_snapshots(manifest, ids, [exit_snapshot])[0]
        assert first.event is not None and second.event is not None
        assert first.event.event_id == second.event.event_id


# =============================================================================
# 4. REPLAY — same fixture + same versions → same logical event
# =============================================================================


class TestReplayDeterminism:
    def test_full_replay_is_byte_identical(self) -> None:
        """Re-running the ENTIRE slice from scratch (fresh engines) produces
        byte-identical events."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        first = _run_full_slice(manifest, ids)
        second = _run_full_slice(manifest, ids)
        assert [e.event_id for e in first.events] == [e.event_id for e in second.events]
        assert [e.model_dump_json() for e in first.events] == [
            e.model_dump_json() for e in second.events
        ]

    def test_processing_time_never_changes_the_logical_event(self) -> None:
        """A different processing time changes ONLY the transport metadata
        (``produced_at``) — never the logical event identity or payload."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        baseline = _run_full_slice(manifest, ids)
        other = _run_full_slice(
            manifest, ids, processing_time=_processing(manifest) + timedelta(days=1)
        )
        assert [e.event_id for e in baseline.events] == [e.event_id for e in other.events]
        assert [e.payload for e in baseline.events] == [e.payload for e in other.events]
        # event_time is the fact's instant — identical across runs.
        assert [e.event_time for e in baseline.events] == [e.event_time for e in other.events]
        # Only the produced_at metadata differs (transport, not truth).
        assert baseline.events[0].produced_at != other.events[0].produced_at

    def test_event_id_is_content_derived(self) -> None:
        """The event_id equals the canonical deterministic_event_id over the
        rule + input + fact instant — proof of the determinism STOP condition."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        engine = build_operational_engine()
        rule = engine._registry.resolve(RULE_ID, RULE_VERSION)
        for snapshot, result in zip(outcome.snapshots, outcome.results, strict=True):
            inp = RuleEvaluationInput(
                facts=(snapshot,),
                configuration={},
                configuration_version_id=ids["configuration_version_id"],
                rule_version=RULE_VERSION,
                event_time=snapshot.event_time,
                processing_time=_processing(manifest),
            )
            expected = deterministic_event_id(
                rule, inp, event_time=inp.event_time, event_type=rule.output_event_type.value
            )
            assert result.event is not None
            assert result.event.event_id == expected


# =============================================================================
# 5. RULE VERSION — the registered v1; unsupported versions rejected
# =============================================================================


class TestRuleVersion:
    def test_registered_rule_is_occupancy_session_v1(self) -> None:
        """The slice uses the REGISTERED Task 16 rule — no reimplementation."""
        engine = build_operational_engine()
        rule = engine._registry.resolve(RULE_ID, RULE_VERSION)
        assert rule.canonical_identity == "occupancy_session:v1"
        assert rule.evaluator_id == OCCUPANCY_SESSION_EVALUATOR_ID
        assert rule.output_event_type.value == RuleEventType.OCCUPANCY_SESSION.value

    def test_unsupported_rule_version_rejected(self) -> None:
        """A different rule version is a typed error — never a silent
        fallback to the registered version."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        snapshots, _ = _run_occupancy_chain(manifest, ids)
        engine = build_operational_engine()
        inp = RuleEvaluationInput(
            facts=(snapshots[0],),
            configuration={},
            configuration_version_id=ids["configuration_version_id"],
            rule_version=RuleVersion("v9"),
            event_time=snapshots[0].event_time,
            processing_time=_processing(manifest),
        )
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(RULE_ID, RuleVersion("v9"), inp)


# =============================================================================
# 6. CONFIGURATION VERSION — participates in the deterministic identity
# =============================================================================


class TestConfigurationVersion:
    def test_configuration_version_participates_in_event_identity(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        snapshots, _ = _run_occupancy_chain(manifest, ids)
        enter = snapshots[0]

        v1 = _evaluate_snapshots(
            manifest, ids, [enter], config_version=ids["configuration_version_id"]
        )[0]
        v2 = _evaluate_snapshots(manifest, ids, [enter], config_version=_CONFIG_V2)[0]
        assert v1.event is not None and v2.event is not None
        assert v1.event.payload.configuration_version_id == ids["configuration_version_id"]
        assert v2.event.payload.configuration_version_id == _CONFIG_V2
        # The config version is part of the content-derived identity — the
        # two evaluations are distinct logical events...
        assert v1.event.event_id != v2.event.event_id
        # ...and both are reproducible.
        v1_replay = _evaluate_snapshots(
            manifest, ids, [enter], config_version=ids["configuration_version_id"]
        )[0]
        v2_replay = _evaluate_snapshots(manifest, ids, [enter], config_version=_CONFIG_V2)[0]
        assert v1_replay.event.event_id == v1.event.event_id
        assert v2_replay.event.event_id == v2.event.event_id

    def test_historical_replay_never_uses_latest_configuration(self) -> None:
        """Re-evaluating a fixture fact under its pinned version AFTER a
        different version exists reproduces the pinned result byte-exact."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        snapshots, _ = _run_occupancy_chain(manifest, ids)
        enter = snapshots[0]
        pinned = _evaluate_snapshots(
            manifest, ids, [enter], config_version=ids["configuration_version_id"]
        )[0]
        _ = _evaluate_snapshots(manifest, ids, [enter], config_version=_CONFIG_V2)
        pinned_replay = _evaluate_snapshots(
            manifest, ids, [enter], config_version=ids["configuration_version_id"]
        )[0]
        assert pinned_replay.model_dump_json() == pinned.model_dump_json()


# =============================================================================
# STOP condition — the slice reuses the registered rule; no new rule
# =============================================================================


class TestNoNewRule:
    def test_engine_is_the_registered_operational_engine(self) -> None:
        engine = build_operational_engine()
        registry_ids = [r.canonical_identity for r in engine._registry.list()]
        assert "occupancy_session:v1" in registry_ids
        # The evaluator behind the registered rule is the packaged one.
        rule = engine._registry.resolve(RULE_ID, RULE_VERSION)
        assert rule.evaluator_id == OCCUPANCY_SESSION_EVALUATOR_ID

    def test_slice_test_defines_no_rule(self) -> None:
        """This vertical slice never declares its own rule — the STOP
        condition for Task 18.8: use the registered Task 16 rule."""
        source = Path(__file__).read_text()
        body = source.split('"""', 2)[2]
        guard_start = body.index("def test_slice_test_defines_no_rule")
        non_guard = body[:guard_start]
        assert "RuleDefinition(" not in non_guard
        assert "FsmRule(" not in non_guard
