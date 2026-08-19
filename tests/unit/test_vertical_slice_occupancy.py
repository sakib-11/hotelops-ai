"""Task 18.7 — occupancy FSM vertical slice.

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 15
presence + occupancy state machines:

    SpatialObservation → PresenceTemporalEngine (Task 15.2) → confirmed
    presence transitions → occupancy_event_from_presence →
    OccupancyEngine (Task 15.4) → stable per-scope occupancy

The slice uses the EXISTING Task 15 semantics — the packaged
``PRESENCE_FSM`` / ``OCCUPANCY_FSM`` singletons, ``presence_kind`` for the
structural SpatialStatus → kind mapping, and
``occupancy_event_from_presence`` as the ONLY sanctioned
presence → occupancy wiring. No FSM is created here.

The observation stream comes from the fixture manifest (the same 18.6
output): the entering track is INSIDE the configured ROI on frames 7..27,
frame 6's centroid lies exactly on the ROI edge (the recorded boundary
blocker — the slice represents that unclassifiable instant as a
policy-intercepted ``not_observed`` observation, which presence treats as
a missing observation, never an exit), and the person is gone on frames
28..30 (two manifest frames plus one deterministic continuation at the
fixture cadence so the configured exit confirmation completes).

The slice policy is explicit and documented (never hardcoded in the
engine): entry/exit confirmation of 2, a minimum dwell of 1.0s, an exit
grace of 0.15s (a single-frame absent is absorbed — anti-jitter), and an
occlusion tolerance of 0.5s.

Verified here (the task's list, against event-time only — processing time
is metadata and never affects business state):

- enter                → the track enters the ROI → OCCUPIED (count 1),
                         only after ENTER_CONFIRMED (ENTERING never counts);
- remain               → continuous presence stays exactly 1, no flicker;
- exit                 → the confirmed exit removes the entity once → 0;
- jitter               → a single absent frame mid-occupancy is absorbed
                         (dwell + grace) — no exit fact, count stable;
- short occlusion      → a not_observed gap within tolerance keeps the
                         entity PRESENT — count stable, no missing-expired;
- late timestamp       → older than the reorder window → typed
                         LateEventError (never silently re-ordered);
- duplicate observation→ the same observation/transition re-applied is
                         idempotent — never a second count change;
- restart/checkpoint   → checkpoint + restore mid-occupancy equals
                         uninterrupted processing (byte-identical state
                         and snapshots).

STOP-condition: the slice produces EXACTLY ONE stable occupancy
transition — one enter snapshot (+1) and one exit snapshot (-1), the
count never exceeds 1 and never flickers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    TemporalResult,
    occupancy_event_from_presence,
    occupancy_scope_key,
    presence_kind,
)
from backend.app.intelligence.temporal.exceptions import LateEventError
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
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
    OccupancyCheckpoint,
    OccupancyState,
    TemporalCheckpoint,
    TemporalOcclusionState,
    TemporalPolicy,
    TemporalReason,
    TemporalState,
    TemporalStateKey,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = FIXTURES_DIR / "manifest.json"

# The fixture's on-frame interval (manifest trajectory constants):
# frame 6 = boundary blocker instant, 7..27 = inside the ROI, 28+ = gone.
BOUNDARY_FRAME = 6
INSIDE_FROM = 7
GONE_FROM = 28

# Deterministic post-recording continuation count (at fixture cadence) so
# the configured exit confirmation (2 qualifying absents) completes.
EXIT_CONFIRM_FRAMES = (GONE_FROM, GONE_FROM + 1, GONE_FROM + 2)


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _uuid(seed: str) -> UUID:
    """Deterministic content-derived id (same namespace as the temporal engine)."""
    return uuid.uuid5(TEMPORAL_ID_NAMESPACE, seed)


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
    """The canonical centroid for a fixture frame (venue-local, 1:1 with
    pixels — the fixture is its own venue plane). Frames without a golden
    point (the empty scene, or the deterministic post-recording
    continuation beyond the manifest) use a fixed fallback point; the
    spatial FSM never reads the point."""
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
    frame_seed: str | None = None,
    event_time: datetime | None = None,
) -> SpatialObservation:
    """A canonical SpatialObservation for the fixture track at a frame."""
    return SpatialObservation(
        session_id=ids["session_id"],
        track_id=ids["track_id"],
        frame_id=FrameId(_uuid(frame_seed or f"frame-{frame_index}")),
        event_time=event_time or _event_at(manifest, frame_index),
        camera_id=ids["camera_id"],
        configuration_version_id=ids["configuration_version_id"],
        spatial_point=_point(manifest, frame_index),
        status=status,
    )


def _fixture_stream(
    manifest: dict,
    ids: dict,
    *,
    jitter_frame: int | None = None,
    occlusion_frames: frozenset[int] = frozenset(),
) -> list[tuple[int, SpatialObservation]]:
    """The canonical slice stream as (frame_index, observation) pairs.

    - frame 6:   not_observed (the boundary blocker instant — the 18.6
                 engine refuses the on-edge centroid, so the slice models
                 the unclassifiable instant as policy-intercepted);
    - frames 7..27: present (INSIDE — the 18.6 output);
    - frames 28..30: absent (the person is gone; 28-29 are manifest
                 frames, 30 is a deterministic continuation at the fixture
                 cadence so the configured exit confirmation completes).

    ``jitter_frame`` swaps one inside frame to OUTSIDE (a single-frame
    absent); ``occlusion_frames`` swaps frames to EXCLUDED (not_observed).
    """
    stream: list[tuple[int, SpatialObservation]] = [
        (
            BOUNDARY_FRAME,
            _observation(manifest, ids, frame_index=BOUNDARY_FRAME, status=SpatialStatus.EXCLUDED),
        )
    ]
    for frame_index in range(INSIDE_FROM, GONE_FROM):
        if frame_index == jitter_frame:
            status = SpatialStatus.OUTSIDE
        elif frame_index in occlusion_frames:
            status = SpatialStatus.EXCLUDED
        else:
            status = SpatialStatus.INSIDE
        stream.append((
            frame_index,
            _observation(manifest, ids, frame_index=frame_index, status=status),
        ))
    for frame_index in EXIT_CONFIRM_FRAMES:
        stream.append((
            frame_index,
            _observation(manifest, ids, frame_index=frame_index, status=SpatialStatus.OUTSIDE),
        ))
    return stream


def _slice_policy(**kwargs: object) -> TemporalPolicy:
    """The slice's explicit policy (documented; never hardcoded in engines)."""
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


@dataclass(frozen=True, slots=True)
class _Step:
    """One slice step: the input observation and both engine outcomes."""

    frame_index: int
    observation: SpatialObservation
    presence_result: TemporalResult
    occ_result: OccupancyResult


@dataclass(frozen=True, slots=True)
class _Run:
    """The full slice outcome: final states, snapshots, per-step records."""

    presence_state: TemporalState
    occupancy_state: OccupancyState
    snapshots: list[object]
    steps: list[_Step]

    @property
    def occupancy_counts(self) -> list[int]:
        return [step.occ_result.state.occupancy_count for step in self.steps]

    @property
    def occupancy_snapshot_counts(self) -> list[int]:
        return [snapshot.occupancy_count for snapshot in self.snapshots]

    @property
    def occupancy_snapshot_deltas(self) -> list[int]:
        return [snapshot.delta for snapshot in self.snapshots]


def _run_slice(
    manifest: dict,
    ids: dict,
    *,
    stream: list[tuple[int, SpatialObservation]] | None = None,
    policy: TemporalPolicy | None = None,
    presence_engine: PresenceTemporalEngine | None = None,
    occupancy_engine: OccupancyEngine | None = None,
    presence_state: TemporalState | None = None,
    occupancy_state: OccupancyState | None = None,
) -> _Run:
    """Run the REAL Task 15 chain over the stream.

    ``presence_engine`` / ``occupancy_engine`` / ``presence_state`` /
    ``occupancy_state`` let a caller resume from a checkpoint (restart).
    """
    policy = policy or _slice_policy()
    presence = presence_engine or PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy)
    occupancy = occupancy_engine or OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy)
    pkey = _presence_key(ids)
    scope = occupancy_scope_key(pkey)
    pstate = presence_state or presence.initial_state(pkey)
    ostate = occupancy_state or occupancy.initial_state(scope)
    processing = _processing(manifest)

    steps: list[_Step] = []
    snapshots: list[object] = []
    for frame_index, obs in stream or _fixture_stream(manifest, ids):
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
        transition = presence_result.transitions[0]
        occ_result = occupancy.apply(
            ostate,
            OccupancyInput(
                key=scope,
                transition=transition,
                observation_kind=occupancy_event_from_presence(transition),
                processing_time=processing,
            ),
        )
        ostate = occ_result.state
        if occ_result.snapshot is not None:
            snapshots.append(occ_result.snapshot)
        steps.append(_Step(frame_index, obs, presence_result, occ_result))
    return _Run(
        presence_state=pstate,
        occupancy_state=ostate,
        snapshots=snapshots,
        steps=steps,
    )


def _confirmed_reasons(run: _Run) -> list[TemporalReason]:
    """The confirmed presence reasons emitted by the slice (exactly the
    occupancy-relevant facts — the stable transition set)."""
    return [
        step.presence_result.transitions[0].reason
        for step in run.steps
        if step.presence_result.transitions[0].reason
        in (
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
            TemporalReason.MISSING_EXPIRED,
            TemporalReason.SESSION_CLOSED,
        )
    ]


# =============================================================================
# The expected fixture: ONE stable occupancy transition (enter -> exit)
# =============================================================================


class TestSingleOccupancyTransition:
    """The full fixture stream: track enters the configured ROI -> OCCUPIED
    -> EXIT. Exactly one enter and one exit — no duplicate, no flicker."""

    def test_full_slice_is_one_stable_transition(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        run = _run_slice(manifest, ids)

        # Exactly TWO snapshots: enter (+1) then exit (-1).
        assert run.occupancy_snapshot_counts == [1, 0]
        assert run.occupancy_snapshot_deltas == [1, -1]
        # Final occupancy: the entity left — count 0, empty set.
        assert run.occupancy_state.occupancy_count == 0
        assert run.occupancy_state.occupied_tracks == frozenset()
        # Exactly one ENTER_CONFIRMED and one EXIT_CONFIRMED — nothing else
        # (no missing-expired, no session-closed, no double entry).
        assert _confirmed_reasons(run) == [
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
        ]
        # The count never exceeds 1 and never goes negative at any step.
        assert max(run.occupancy_counts) == 1
        assert min(run.occupancy_counts) == 0
        # And the count is stable: it changes ONLY at the two snapshot
        # frames (enter at frame 8, exit at frame 30) — every other frame
        # leaves it untouched. A change between step i and i+1 is applied
        # BY step i+1, so it is reported at steps[i + 1].
        counts = run.occupancy_counts
        change_frames = [
            run.steps[i + 1].frame_index
            for i in range(len(counts) - 1)
            if counts[i] != counts[i + 1]
        ]
        assert change_frames == [8, EXIT_CONFIRM_FRAMES[-1]]

    def test_enter_counts_once_after_confirmation(self) -> None:
        """ENTERING never counts; the confirmed enter counts exactly once."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        # Frames 6 (not_observed) + 7 (present -> ENTERING): NOT counted.
        entering = _run_slice(manifest, ids, stream=stream[:2])
        assert entering.occupancy_state.occupancy_count == 0
        assert entering.snapshots == []
        # Frame 8 (second present -> ENTER_CONFIRMED): counted exactly once.
        confirmed = _run_slice(manifest, ids, stream=stream[:3])
        assert confirmed.occupancy_state.occupancy_count == 1
        assert confirmed.occupancy_snapshot_counts == [1]
        assert confirmed.occupancy_snapshot_deltas == [1]
        assert _confirmed_reasons(confirmed) == [TemporalReason.ENTER_CONFIRMED]

    def test_remain_is_stable_through_the_whole_inside_interval(self) -> None:
        """21 consecutive present frames produce exactly one enter — the
        occupancy is stable, never re-counted per frame."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        # The inside interval: frame 6 + frames 7..27 (no exit frames).
        inside = [pair for pair in stream if pair[0] < GONE_FROM]
        run = _run_slice(manifest, ids, stream=inside)
        assert run.occupancy_state.occupancy_count == 1
        assert run.occupancy_snapshot_counts == [1]
        # Presence is confirmed PRESENT at the end of the interval.
        assert run.presence_state.current_state == "present"
        # No confirmed exit, no missing-expired anywhere in the interval.
        assert _confirmed_reasons(run) == [TemporalReason.ENTER_CONFIRMED]

    def test_exit_confirms_once(self) -> None:
        """The exit removes the entity exactly once — the count returns to 0."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        run = _run_slice(manifest, ids)
        assert run.occupancy_state.occupancy_count == 0
        assert _confirmed_reasons(run)[-1] is TemporalReason.EXIT_CONFIRMED
        # The exit snapshot carries the confirmed event time (frame 30).
        exit_snapshot = run.snapshots[-1]
        assert exit_snapshot.occupancy_count == 0
        assert exit_snapshot.previous_count == 1
        assert exit_snapshot.delta == -1
        assert exit_snapshot.event_time == _event_at(manifest, EXIT_CONFIRM_FRAMES[-1])

    def test_occupancy_snapshot_preserves_scope_provenance(self) -> None:
        """The snapshot carries the pinned identity: tenant/venue/session/
        camera/configuration version + the ROI spatial context."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        run = _run_slice(manifest, ids)
        enter_snapshot = run.snapshots[0]
        key = enter_snapshot.key
        assert key.fsm_kind == "occupancy"
        assert key.tenant_id == ids["tenant_id"]
        assert key.venue_id == ids["venue_id"]
        assert key.session_id == ids["session_id"]
        assert key.camera_id == ids["camera_id"]
        assert key.configuration_version_id == ids["configuration_version_id"]
        assert key.semantic_context == ids["semantic_context"]
        assert key.track_id == OCCUPANCY_SCOPE_TRACK
        assert enter_snapshot.event_time == _event_at(manifest, 8)


# =============================================================================
# Jitter and occlusion — the configured hysteresis/grace semantics
# =============================================================================


class TestJitterAndOcclusion:
    """Brief absence or a short occlusion never flips occupancy."""

    def test_single_frame_jitter_is_absorbed(self) -> None:
        """One OUTSIDE frame in the middle of the occupancy is absorbed by
        the dwell/grace semantics — no exit fact, count stays stable, and
        the person is still counted once."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        run = _run_slice(manifest, ids, stream=_fixture_stream(manifest, ids, jitter_frame=15))

        # At the jitter frame the presence state stays PRESENT (dwell
        # not yet satisfied AND grace not elapsed) — never EXITING.
        jitter_step = next(step for step in run.steps if step.frame_index == 15)
        assert jitter_step.presence_result.state.current_state == "present"
        assert jitter_step.presence_result.transitions[0].reason is TemporalReason.OBSERVED_STAY
        # The slice still ends with exactly ONE stable transition.
        assert run.occupancy_snapshot_counts == [1, 0]
        assert run.occupancy_snapshot_deltas == [1, -1]
        assert _confirmed_reasons(run) == [
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
        ]

    def test_jitter_never_fabricates_a_second_occupancy(self) -> None:
        """The count never dips to 0 around the jitter — no enter/exit
        flicker, no second occupancy cycle: 0, 0, 1, ..., 1, then the one
        final exit."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        run = _run_slice(manifest, ids, stream=_fixture_stream(manifest, ids, jitter_frame=15))
        counts = run.occupancy_counts
        assert counts == [0, 0, 1] + [1] * (len(counts) - 4) + [0]

    def test_short_occlusion_keeps_the_entity_present(self) -> None:
        """A not_observed gap within occlusion_tolerance (0.5s = 5 frames)
        marks TEMPORARILY_MISSING — never MISSING_EXPIRED, count stable."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        run = _run_slice(
            manifest,
            ids,
            stream=_fixture_stream(manifest, ids, occlusion_frames=frozenset({15, 16})),
        )

        # During the gap the presence state is PRESENT + TEMPORARILY_MISSING.
        gap_step = next(step for step in run.steps if step.frame_index == 16)
        assert gap_step.presence_result.state.current_state == "present"
        assert (
            gap_step.presence_result.state.occlusion_state
            is TemporalOcclusionState.TEMPORARILY_MISSING
        )
        # No missing-expired anywhere; exactly the one stable transition.
        assert _confirmed_reasons(run) == [
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
        ]
        assert run.occupancy_snapshot_counts == [1, 0]

    def test_occlusion_beyond_tolerance_is_a_real_exit(self) -> None:
        """A not_observed gap LONGER than tolerance is MISSING_EXPIRED — the
        entity is removed exactly once. A later positive presence is a NEW
        confirmed entry (Task 15 semantics: the entity left, then returned)
        — never a stale double count."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        # Occlude frames 15..20 (gap 0.5s+ from frame 14's presence): frame
        # 19 crosses the 0.5s tolerance → MISSING_EXPIRED; the person then
        # returns on 21..27 (re-entry) and leaves at the end. The re-entry
        # confirms late (frame 22), so a shorter dwell is used here so the
        # final exit still qualifies inside the deterministic stream.
        run = _run_slice(
            manifest,
            ids,
            stream=_fixture_stream(
                manifest, ids, occlusion_frames=frozenset({15, 16, 17, 18, 19, 20})
            ),
            policy=_slice_policy(minimum_dwell_seconds=0.5),
        )
        reasons = _confirmed_reasons(run)
        assert reasons == [
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.MISSING_EXPIRED,
            TemporalReason.ENTER_CONFIRMED,
            TemporalReason.EXIT_CONFIRMED,
        ]
        # Each presence period counts exactly once — the entity set never
        # holds a stale count and the occupancy ends clean at 0.
        assert run.occupancy_snapshot_deltas == [1, -1, 1, -1]
        assert run.occupancy_snapshot_counts == [1, 0, 1, 0]
        assert run.occupancy_state.occupancy_count == 0


# =============================================================================
# Event-time discipline — processing time is never business state
# =============================================================================


class TestEventTimeDiscipline:
    def test_processing_time_never_affects_occupancy(self) -> None:
        """Identical streams with wildly different processing times produce
        byte-identical occupancy state and snapshots."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)

        run_early = _run_slice(manifest, ids, stream=stream)
        run_late = _run_slice(manifest, ids, stream=stream)
        # Re-run with a different processing window entirely (the engine
        # never reads it — event_time is authoritative).
        from backend.app.intelligence.temporal import (
            OccupancyEngine,
            PresenceTemporalEngine,
        )

        presence = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=_slice_policy())
        occupancy = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=_slice_policy())
        pkey = _presence_key(ids)
        scope = occupancy_scope_key(pkey)
        pstate = presence.initial_state(pkey)
        ostate = occupancy.initial_state(scope)
        processing = _processing(manifest) + timedelta(days=1)
        alt_snapshots = []
        for _fi, obs in stream:
            pres = presence.apply(
                pstate,
                TemporalInput(
                    key=pkey,
                    observation=obs,
                    observation_kind=presence_kind(obs),
                    processing_time=processing,
                ),
            )
            pstate = pres.state
            occ = occupancy.apply(
                ostate,
                OccupancyInput(
                    key=scope,
                    transition=pres.transitions[0],
                    observation_kind=occupancy_event_from_presence(pres.transitions[0]),
                    processing_time=processing,
                ),
            )
            ostate = occ.state
            if occ.snapshot is not None:
                alt_snapshots.append(occ.snapshot)
        assert run_early.snapshots == run_late.snapshots
        assert alt_snapshots == run_early.snapshots
        assert run_early.occupancy_state == ostate

    def test_late_timestamp_beyond_window_rejected(self) -> None:
        """An observation older than the watermark by more than the
        reorder window is a typed LateEventError — never silently
        re-ordered into event order."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        run = _run_slice(manifest, ids, stream=stream)
        assert run.presence_state.watermark_event_time == _event_at(manifest, 30)

        # An observation 10s BEFORE the recording started (13s older than
        # the 3.0s watermark — beyond the 5s window).
        late = _observation(
            manifest,
            ids,
            frame_index=BOUNDARY_FRAME,
            status=SpatialStatus.INSIDE,
            frame_seed="late-observation",
            event_time=_event_at(manifest, 0) - timedelta(seconds=10),
        )
        with pytest.raises(LateEventError, match="reordering window"):
            _run_slice(
                manifest,
                ids,
                stream=[*stream, (None, late)],
            )

    def test_within_window_reorder_never_rewinds_occupancy(self) -> None:
        """A reorder inside the window is accepted as a REORDERED fact and
        never changes the count (accept-with-no-rewind)."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        run = _run_slice(manifest, ids, stream=stream)
        assert run.occupancy_state.occupancy_count == 0

        # A "present" observation at frame 2's event time (2.5s older than
        # the watermark, within the 5s window) with a NEW frame identity.
        reorder_obs = _observation(
            manifest,
            ids,
            frame_index=2,
            status=SpatialStatus.INSIDE,
            frame_seed="reordered-present",
        )
        rerun = _run_slice(manifest, ids, stream=[*stream, (None, reorder_obs)])
        last = rerun.steps[-1]
        assert last.presence_result.transitions[0].reason is TemporalReason.REORDERED
        # The count is untouched — no rewind, no fabricated occupancy.
        assert rerun.occupancy_state.occupancy_count == 0
        assert rerun.occupancy_snapshot_counts == [1, 0]
        assert rerun.snapshots == run.snapshots


# =============================================================================
# Duplicate observations — idempotent, never a double count
# =============================================================================


class TestDuplicateObservations:
    def test_replayed_last_observation_is_deduplicated(self) -> None:
        """Re-applying the exact same observation (same event_time +
        frame_id) is DEDUPLICATED — the state is byte-identical and the
        occupancy never changes."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        run = _run_slice(manifest, ids, stream=stream)

        # Redeliver the final observation exactly as-is.
        frame_idx, last_obs = stream[-1]
        rerun = _run_slice(manifest, ids, stream=[*stream, (frame_idx, last_obs)])
        last = rerun.steps[-1]
        assert last.presence_result.deduplicated is True
        assert last.presence_result.transitions[0].reason is TemporalReason.DEDUPLICATED
        # The final state is byte-identical and the snapshots are unchanged.
        assert rerun.presence_state == run.presence_state
        assert rerun.occupancy_state == run.occupancy_state
        assert rerun.snapshots == run.snapshots

    def test_replayed_enter_transition_never_double_counts(self) -> None:
        """Re-applying the ENTER_CONFIRMED transition is idempotent: the
        entity is counted once, never twice."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        # Run through the confirmed enter only (frames 6..8).
        confirmed = _run_slice(manifest, ids, stream=stream[:3])
        assert confirmed.occupancy_state.occupancy_count == 1
        # Re-apply the SAME enter transition to the occupancy state.
        enter_step = confirmed.steps[-1]
        enter_transition = enter_step.presence_result.transitions[0]
        scope = occupancy_scope_key(_presence_key(ids))
        replay = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=_slice_policy()).apply(
            confirmed.occupancy_state,
            OccupancyInput(
                key=scope,
                transition=enter_transition,
                observation_kind=occupancy_event_from_presence(enter_transition),
                processing_time=_processing(manifest),
            ),
        )
        assert replay.deduplicated is True
        assert replay.snapshot is None
        assert replay.state.occupancy_count == 1  # never 2
        assert replay.state.occupied_tracks == confirmed.occupancy_state.occupied_tracks

    def test_replayed_enter_after_more_processing_never_rewinds(self) -> None:
        """Re-applying the enter transition AFTER later frames were
        processed stays idempotent through the reorder policy: the count
        never grows, state never rewinds."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)
        run = _run_slice(manifest, ids, stream=stream)
        enter_transition = run.steps[2].presence_result.transitions[0]  # frame 8
        scope = occupancy_scope_key(_presence_key(ids))
        replay = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=_slice_policy()).apply(
            run.occupancy_state,
            OccupancyInput(
                key=scope,
                transition=enter_transition,
                observation_kind=occupancy_event_from_presence(enter_transition),
                processing_time=_processing(manifest),
            ),
        )
        # Within the reorder window: accepted, no rewind — the count is
        # still the final 0 (the exit already removed the entity).
        assert replay.reordered is True
        assert replay.state.occupancy_count == 0


# =============================================================================
# Restart / checkpoint — resumable without changing the outcome
# =============================================================================


class TestCheckpointRestart:
    def test_checkpoint_restart_equals_uninterrupted(self) -> None:
        """Checkpointing mid-occupancy (entity counted) and resuming in a
        fresh engine produces byte-identical state and snapshots."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        stream = _fixture_stream(manifest, ids)

        # Uninterrupted: the whole stream in one engine.
        full = _run_slice(manifest, ids, stream=stream)

        # Restarted: run frames 6..12, checkpoint BOTH engines, restore
        # into fresh engines, then run the remainder.
        head, tail = stream[:7], stream[7:]
        policy = _slice_policy()
        presence_a = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy)
        occupancy_a = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy)
        part_a = _run_slice(
            manifest, ids, stream=head, presence_engine=presence_a, occupancy_engine=occupancy_a
        )
        # Mid-occupancy: the entity is counted at the checkpoint.
        assert part_a.occupancy_state.occupancy_count == 1
        presence_cp = presence_a.checkpoint(part_a.presence_state)
        occupancy_cp = occupancy_a.checkpoint(part_a.occupancy_state)
        # Checkpoints are serializable and round-trip.
        assert TemporalCheckpoint.from_dict(presence_cp.to_dict()) == presence_cp
        assert OccupancyCheckpoint.from_dict(occupancy_cp.to_dict()) == occupancy_cp

        presence_b = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy)
        occupancy_b = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy)
        part_b = _run_slice(
            manifest,
            ids,
            stream=tail,
            presence_engine=presence_b,
            occupancy_engine=occupancy_b,
            presence_state=presence_b.restore(presence_cp),
            occupancy_state=occupancy_b.restore(occupancy_cp),
        )

        # Identical final states; the replayed snapshot sequence equals the
        # uninterrupted one (enter before the checkpoint + exit after it).
        assert part_b.presence_state == full.presence_state
        assert part_b.occupancy_state == full.occupancy_state
        assert part_a.snapshots + part_b.snapshots == full.snapshots
        assert part_a.occupancy_snapshot_counts == [1]
        assert part_b.occupancy_snapshot_counts == [0]

    def test_restore_rejects_version_and_policy_drift(self) -> None:
        """A checkpoint made under a different engine version or policy
        revision is rejected — historical state is never silently
        reinterpreted."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        from backend.app.intelligence.temporal.exceptions import (
            CheckpointIntegrityError,
            FsmVersionMismatchError,
        )

        presence = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=_slice_policy())
        pstate = presence.initial_state(_presence_key(ids))
        cp = presence.checkpoint(pstate)
        # Version drift.
        drifted = TemporalCheckpoint(
            engine_version="9.9.9", policy_revision=cp.policy_revision, state=cp.state
        )
        with pytest.raises(FsmVersionMismatchError, match="engine version"):
            presence.restore(drifted)
        # Policy drift.
        drifted_policy = TemporalCheckpoint(
            engine_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v2",
            state=cp.state,
        )
        with pytest.raises(CheckpointIntegrityError, match="policy revision"):
            presence.restore(drifted_policy)


# =============================================================================
# STOP condition: the slice reuses Task 15 semantics — no new FSM
# =============================================================================


class TestNoNewFsm:
    def test_slice_uses_the_packaged_task15_fsms(self) -> None:
        """The engines are constructed with the package's PRESENCE_FSM /
        OCCUPANCY_FSM singletons — never a redefined state machine."""
        presence = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=_slice_policy())
        occupancy = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=_slice_policy())
        assert presence.fsm is PRESENCE_FSM
        assert occupancy.fsm is OCCUPANCY_FSM
        assert occupancy.fsm is not presence.fsm  # distinct families

    def test_slice_test_defines_no_fsm_rules(self) -> None:
        """This vertical slice never declares its own FSM or rules — the
        STOP condition for Task 18.7: use existing Task 15 semantics."""
        source = Path(__file__).read_text()
        # Strip the module docstring; the guard function itself is the only
        # permitted place to mention FSM construction.
        body = source.split('"""', 2)[2]
        guard_start = body.index("def test_slice_test_defines_no_fsm_rules")
        non_guard = body[:guard_start]
        assert "FsmRule(" not in non_guard
        assert "DeterministicFsm(" not in non_guard
