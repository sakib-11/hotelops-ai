"""Task 18.16 — VERTICAL SLICE FAILURE INJECTION.

Simulates a failure at EVERY major boundary of the 18.14 vertical slice
and verifies, for each, the cross-cutting failure contract:

    A. file decode failure        H. outbox worker crash
    B. YOLO initialization fail.  I. duplicate message
    C. tracker failure            J. evidence extraction failure
    D. ROI/configuration failure  K. object storage failure
    E. FSM restart                L. API unauthorized
    F. database failure           M. API dependency failure
    G. transaction rollback       N. Tauri API failure (desktop suite)

For every boundary the applicable subset of the seven verifications is
asserted:

    - no silent corruption       — a failure is never absorbed into a
                                   fabricated/partial result;
    - no duplicate logical fact  — one fact/event/evidence request per
                                   logical occurrence, even under retry;
    - no lost committed event    — an event durable in PostgreSQL (the
                                   outbox row) is never dropped;
    - correct retry behavior     — the failure retries with the
                                   documented policy (backoff / lease);
    - correct structured telemetry — the failure is observable (typed
                                   errors, counters, audit events, logs);
    - correct error classification — every failure raises its typed
                                   error (never a generic catch-all);
    - correct recovery behavior  — after the fault clears, the slice
                                   recovers to exactly the golden outcome.

STOP conditions (the task's): failures are EXPLICIT and OBSERVABLE —
nothing is swallowed by a bare ``except Exception: continue``; a failed
boundary never produces a partial or fabricated fact/event; and a
committed event is never lost (the Task 7 outbox row is the durability
boundary). The only synthesis is the documented 18.6 boundary
interception (an on-edge centroid is NEVER silently classified) — the
exact seam the 18.14 E2E already uses. No production code is weakened.
"""

from __future__ import annotations

import logging
import pathlib
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.routes.operational import (
    get_operational_event,
    get_operational_event_evidence,
)
from backend.app.application.services.evidence_audit import (
    EVENT_RETRYABLE_FAILURE,
    EVENT_TERMINAL_FAILURE,
)
from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.application.services.operational_errors import OperationalNotFoundError
from backend.app.application.services.operational_persistence import (
    OperationalPersistenceService,
)
from backend.app.domain.evidence.extraction import ExtractionStatus
from backend.app.infrastructure.auth.deps import get_token_data, require_permission
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.service import verify_token
from backend.app.infrastructure.database.repositories.evidence_work import (
    EVIDENCE_LAST_ERROR_KEY,
    EVIDENCE_RETRY_AT_KEY,
)
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.intelligence.detectors import ModelLoadError
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from backend.app.intelligence.pipeline import FramePipeline
from backend.app.intelligence.sources.base import FrameSourceState
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    SourceTerminatedError,
)
from backend.app.intelligence.sources.file import FileFrameSource
from backend.app.intelligence.sources.queue import (
    BoundedFrameQueue,
    QueueFullPolicy,
)
from backend.app.intelligence.spatial.engine import (
    SpatialEvaluationInput,
    evaluate_spatial,
)
from backend.app.intelligence.spatial.exceptions import (
    BoundaryPolicyUndefinedError,
    CameraNotInConfigurationError,
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
from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
)
from backend.app.intelligence.tracking.base import TrackerConfig, track_uuid
from backend.app.intelligence.tracking.bytetrack_adapter import ByteTrackAdapter
from backend.app.intelligence.tracking.exceptions import TrackingExecutionError
from backend.app.workers.operational_effects import build_operational_effect_handlers
from contracts.common import (
    EventId,
    TenantId,
    UserId,
    VenueId,
    utc_now,
)
from contracts.identity import (
    ActorContext,
    Permission,
    RoleName,
)
from contracts.rules import RuleEvaluationStatus
from contracts.spatial import (
    SpatialStatus,
)
from contracts.temporal import TemporalStateKey
from tests.unit.fakes import make_actor
from tests.unit.test_evidence_worker import (
    _REF,
    Clock,
    FakeCandidates,
    FakeEvidenceWorkStore,
    FakeExtractor,
    RecordingAuditSink,
    _candidate,
    _make_ref,
    _make_worker,
    _raise,
    _state,
    _status,
)
from tests.unit.test_vertical_slice_api import (
    FakeSession as ApiSession,
)
from tests.unit.test_vertical_slice_api import (
    _actor,
    _expired_token,
    _session_with,
    _settings,
    _slice_rows,
)
from tests.unit.test_vertical_slice_detection import make_config, make_spec
from tests.unit.test_vertical_slice_e2e import (
    _FRAME_IDS,
    ASSET_ID,
    IDS,
    MANIFEST,
    OBJECT_KEY,
    _ChainConsumer,
    _install_deterministic_seams,
    _object_stream,
)
from tests.unit.test_vertical_slice_evidence import FakeEvidenceLinkageRepository
from tests.unit.test_vertical_slice_fixture import _build_decoded_frames, _FixtureDecoder
from tests.unit.test_vertical_slice_outbox import FakePipeline
from tests.unit.test_vertical_slice_persistence import (
    FakeOutbox as PersistenceOutbox,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeSession as PersistenceSession,
)
from tests.unit.test_vertical_slice_persistence import (
    FakeStore,
    _integrity,
)
from tests.unit.test_vertical_slice_rule import (
    _evaluate_snapshots,
    _processing,
    _slice_policy,
)
from tests.unit.test_vertical_slice_spatial import _published_configuration
from tests.unit.test_vertical_slice_tracking import FakeBYTETracker, _plan_for_fixture

pytestmark = pytest.mark.e2e

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
_CAPTURE_TIME = datetime.fromisoformat(MANIFEST["metadata"]["capture_time"])

# =============================================================================
# Shared slice driver — the REAL chain, one deterministic run
# =============================================================================


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deterministic seams every slice run needs (18.14's installer)."""
    _install_deterministic_seams(monkeypatch)


@dataclass
class ChainParts:
    """The real CV-chain components of one slice run, with injection points."""

    consumer: _ChainConsumer
    yolo: YOLOv8Adapter
    tracker: ByteTrackAdapter
    presence: PresenceTemporalEngine
    occupancy: OccupancyEngine
    actor: Any
    configuration: Any
    pkey: TemporalStateKey
    scope: TemporalStateKey
    policy: Any
    processing: datetime


def _make_chain() -> ChainParts:
    """Build the REAL chain (mirrors 18.14's ``_run_e2e`` construction)."""
    _FRAME_IDS.reset()
    FakeBYTETracker.update_plan = _plan_for_fixture(MANIFEST)

    actor = make_actor(tenant_id=uuid.UUID(str(IDS["tenant_id"])))
    configuration = _published_configuration(MANIFEST)
    yolo = YOLOv8Adapter(model_spec=make_spec(MANIFEST), config=make_config(MANIFEST))
    tracker = ByteTrackAdapter(
        session_id=IDS["session_id"],
        config=TrackerConfig(
            track_thresh=0.5,
            match_thresh=0.8,
            track_buffer=30,
            frame_rate=30,
            min_hits=1,
            detection_match_iou=0.5,
        ),
    )
    track_id = track_uuid(IDS["session_id"], 1)
    pkey = TemporalStateKey(
        fsm_kind="presence",
        tenant_id=IDS["tenant_id"],
        venue_id=IDS["venue_id"],
        session_id=IDS["session_id"],
        camera_id=IDS["camera_id"],
        configuration_version_id=IDS["configuration_version_id"],
        track_id=track_id,
        semantic_context=IDS["semantic_context"],
    )
    scope = occupancy_scope_key(pkey)
    policy = _slice_policy()
    processing = _processing(MANIFEST)
    presence = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=policy)
    occupancy = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=policy)
    consumer = _ChainConsumer(
        yolo=yolo,
        tracker=tracker,
        configuration=configuration,
        camera_id=IDS["camera_id"],
        presence=presence,
        occupancy=occupancy,
        pkey=pkey,
        scope=scope,
        processing=processing,
    )
    return ChainParts(
        consumer=consumer,
        yolo=yolo,
        tracker=tracker,
        presence=presence,
        occupancy=occupancy,
        actor=actor,
        configuration=configuration,
        pkey=pkey,
        scope=scope,
        policy=policy,
        processing=processing,
    )


async def _run_cv(consumer: _ChainConsumer, source: Any) -> None:
    """Run the fixture through the REAL FramePipeline into the chain."""
    pipeline = FramePipeline(
        queue=BoundedFrameQueue(maxsize=16, full_policy=QueueFullPolicy.BLOCK),
        consumer=consumer,
    )
    await pipeline.run(source)


def _facts_and_events(consumer: _ChainConsumer) -> tuple[list[Any], list[Any]]:
    """The REGISTERED occupancy rule over the chain's confirmed facts."""
    results = _evaluate_snapshots(MANIFEST, IDS, consumer.snapshots)
    events = [r.event for r in results if r.status is RuleEvaluationStatus.MATCH]
    return results, events


async def _uploaded_storage() -> FakeStorageAdapter:
    """The fixture recording uploaded to the Task 9 storage port."""
    storage = FakeStorageAdapter()
    object_data = b"".join(
        (FIXTURES_DIR / f"frame_{frame:03d}.png").read_bytes()
        for frame in range(MANIFEST["metadata"]["frame_count"])
    )
    await storage.put_object_stream(
        OBJECT_KEY,
        _object_stream(object_data),
        content_type="video/mp4",
        size_bytes=len(object_data),
    )
    return storage


async def _source(
    *,
    decoder: Any | None = None,
    storage: FakeStorageAdapter | None = None,
    max_consecutive_decode_errors: int = 100,
) -> FileFrameSource:
    """The fixture source (defaults = the golden 18.14 source)."""
    return FileFrameSource(
        session_id=IDS["session_id"],
        source_ref=ASSET_ID,
        storage=storage if storage is not None else await _uploaded_storage(),
        object_key=OBJECT_KEY,
        decoder=decoder or _FixtureDecoder(_build_decoded_frames(MANIFEST)),
        capture_time=_CAPTURE_TIME,
        max_consecutive_decode_errors=max_consecutive_decode_errors,
    )


async def _persisted_run() -> tuple[ChainParts, list[Any], FakeStore]:
    """Run the full CV chain + the authoritative 18.10 commit (no worker)."""
    chain = _make_chain()
    source = await _source()
    await _run_cv(chain.consumer, source)
    _results, events = _facts_and_events(chain.consumer)
    assert events, "the slice must produce the one logical occupancy event"
    store = FakeStore()
    session = PersistenceSession(store)
    service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
    for snapshot, event in zip(chain.consumer.snapshots, events, strict=True):
        result = await service.persist(session, fact=snapshot, event=event, actor=chain.actor)
        assert result.created is True
    await session.commit()
    return chain, events, store


async def _golden_event_id() -> Any:
    """The event identity of a clean 18.14 run (deterministic)."""
    from tests.unit.test_vertical_slice_e2e import _run_e2e

    return (await _run_e2e()).event.event_id


# =============================================================================
# A. FILE DECODE FAILURE
# =============================================================================


class TestFileDecodeFailure:
    async def test_corrupt_frame_is_explicit_counted_and_chain_recovers(self) -> None:
        """A corrupt source frame is an explicit FrameDecodeError — counted
        and skipped by the REAL source, never silently absorbed; the chain
        still produces the ONE golden event (no duplicate, no loss)."""
        chain = _make_chain()
        source = await _source(
            decoder=_FixtureDecoder(_build_decoded_frames(MANIFEST), decode_error_at={12})
        )
        await _run_cv(chain.consumer, source)

        # Explicit + observable: the source counted the decode failure.
        assert source.decode_errors == 1
        # No silent corruption: exactly 29 packets were emitted (the
        # corrupt source frame was skipped — never turned into a
        # fabricated packet), with canonical contiguous indices.
        assert chain.consumer.packets_seen == list(range(29))
        # No duplicate logical fact / no lost committed event: the SAME
        # content-derived event the golden run produces.
        _results, events = _facts_and_events(chain.consumer)
        assert len(events) == 1
        assert events[0].event_id == await _golden_event_id()

    async def test_sustained_corruption_terminates_loudly_no_partial_state(self) -> None:
        """Sustained decode corruption is terminal (SourceTerminatedError) —
        the pipeline fails loudly and NOTHING partial is ever committed."""
        chain = _make_chain()
        source = await _source(
            decoder=_FixtureDecoder(_build_decoded_frames(MANIFEST), decode_error_at={5, 6, 7}),
            max_consecutive_decode_errors=3,
        )
        with pytest.raises(SourceTerminatedError, match="consecutive"):
            await _run_cv(chain.consumer, source)

        # The failure is explicit and observable (never swallowed): the
        # terminal error propagated and the pipeline cleanly released the
        # source (no resource leak).
        assert source.state is FrameSourceState.CLOSED
        assert source.decode_errors == 3
        # No partial business state: frames before the corruption produced
        # no observation (the person had not yet entered) and no fact.
        assert chain.consumer.packets_seen == [0, 1, 2, 3, 4]
        assert chain.consumer.snapshots == []
        _results, events = _facts_and_events(chain.consumer)
        assert events == []

    def test_decode_error_classification(self) -> None:
        """The corrupt-frame condition is a typed FrameDecodeError (the
        source's policy classifies it — never a bare exception)."""
        exc = FrameDecodeError("corrupt frame at source position 3")
        assert "corrupt frame" in exc.message
        assert isinstance(exc, FrameSourceError)


# =============================================================================
# B. YOLO INITIALIZATION FAILURE
# =============================================================================


class TestYoloInitFailure:
    async def test_yolo_init_failure_is_typed_chain_stops_and_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B — YOLO initialization failure: typed ModelLoadError, no
        fabricated detections, and recovery produces the ONE golden event."""
        base_yolo = sys.modules["ultralytics"].YOLO

        class _ToggleYOLO(base_yolo):  # type: ignore[misc]
            fail: ClassVar[bool] = True

            def __init__(self, artifact_uri: str) -> None:
                if _ToggleYOLO.fail:
                    msg = "cuda OOM during model load"
                    raise RuntimeError(msg)
                super().__init__(artifact_uri)

        monkeypatch.setattr(sys.modules["ultralytics"], "YOLO", _ToggleYOLO)

        chain = _make_chain()
        source = await _source()
        with pytest.raises(ModelLoadError) as excinfo:
            await _run_cv(chain.consumer, source)
        # Correct error classification: the adapter wraps the SDK failure.
        assert isinstance(excinfo.value.cause, RuntimeError)
        # No silent corruption / no fabricated output: the model never
        # became available (observable state), and no detection/snapshot
        # ever leaked out.
        assert chain.yolo.loaded is False
        assert chain.consumer.detections_by_frame == {}
        assert chain.consumer.snapshots == []

        # Correct recovery: once the model is loadable, a fresh run
        # produces exactly the ONE golden event.
        _ToggleYOLO.fail = False
        chain2 = _make_chain()
        source2 = await _source()
        await _run_cv(chain2.consumer, source2)
        _results, events = _facts_and_events(chain2.consumer)
        assert len(events) == 1
        assert events[0].event_id == await _golden_event_id()


# =============================================================================
# C. TRACKER FAILURE
# =============================================================================


class _ToggleTracker(FakeBYTETracker):
    """Backend double with a per-test update failure toggle."""

    fail: ClassVar[bool] = False

    def update(self, dets: Any, img_info: Any, img_size: Any) -> list[Any]:
        if _ToggleTracker.fail:
            msg = "tracker cuda sync error"
            raise RuntimeError(msg)
        return super().update(dets, img_info, img_size)


class TestTrackerFailure:
    async def test_tracker_backend_failure_is_typed_and_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C — tracker failure: typed TrackingExecutionError (never raw,
        never 'zero tracks'), the chain stops, and recovery reproduces the
        golden event."""
        monkeypatch.setattr(sys.modules["bytetrack"], "BYTETracker", _ToggleTracker)

        _ToggleTracker.fail = True
        chain = _make_chain()
        source = await _source()
        with pytest.raises(TrackingExecutionError) as excinfo:
            await _run_cv(chain.consumer, source)
        # Error classification: the backend exception is wrapped, not leaked.
        assert isinstance(excinfo.value.cause, RuntimeError)
        # No silent corruption: a failing update is never converted into an
        # empty track list — the chain stopped with no snapshots.
        assert chain.consumer.snapshots == []
        assert chain.tracker.stats().total_failed_updates >= 1

        # Correct recovery: fresh tracker (restart) → the ONE golden event.
        _ToggleTracker.fail = False
        chain2 = _make_chain()
        source2 = await _source()
        await _run_cv(chain2.consumer, source2)
        _results, events = _facts_and_events(chain2.consumer)
        assert len(events) == 1
        assert events[0].event_id == await _golden_event_id()


# =============================================================================
# D. ROI / CONFIGURATION FAILURE
# =============================================================================


class TestRoiConfigurationFailure:
    async def test_camera_not_in_configuration_is_typed_never_silent(self) -> None:
        """D — a configuration that does not contain the camera fails with
        the typed CameraNotInConfigurationError — never an empty/silent
        classification."""
        chain = _make_chain()
        source = await _source()
        await _run_cv(chain.consumer, source)

        # A real track + point from the chain (frame 7, INSIDE).
        track = chain.consumer.tracks_by_frame[7][0]
        point = chain.consumer.spatial_by_frame[7].spatial_point
        wrong_camera = uuid.UUID(int=424242)
        with pytest.raises(CameraNotInConfigurationError):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=chain.configuration,
                    track=track,
                    camera_id=wrong_camera,
                    point=point,
                )
            )

    async def test_boundary_centroid_raises_raw_and_is_intercepted_not_classified(
        self,
    ) -> None:
        """D — the on-edge centroid makes the REAL engine raise
        BoundaryPolicyUndefinedError (explicit, never INSIDE/OUTSIDE); the
        slice's ONLY sanctioned interception records it as EXCLUDED with
        full provenance and a counter — never a silent classification."""
        chain = _make_chain()
        source = await _source()
        await _run_cv(chain.consumer, source)

        # The raw engine refuses the on-edge point explicitly.
        track6 = chain.consumer.tracks_by_frame[6][0]
        obs6 = chain.consumer.spatial_by_frame[6]
        with pytest.raises(BoundaryPolicyUndefinedError):
            evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=chain.configuration,
                    track=track6,
                    camera_id=IDS["camera_id"],
                    point=obs6.spatial_point,
                )
            )
        # The interception is observable: exactly one, recorded as EXCLUDED
        # (never INSIDE/OUTSIDE, never dropped), provenance preserved.
        assert chain.consumer.boundary_interceptions == 1
        assert obs6.status is SpatialStatus.EXCLUDED
        assert obs6.zone_profile_id is None
        assert obs6.track_id == track6.track_id


# =============================================================================
# E. FSM RESTART
# =============================================================================


def _apply_all(
    presence: PresenceTemporalEngine,
    occupancy: OccupancyEngine,
    pstate: Any,
    ostate: Any,
    observations: list[Any],
    *,
    pkey: TemporalStateKey,
    scope: TemporalStateKey,
    processing: datetime,
) -> tuple[list[Any], Any, Any]:
    """Replay observations through the REAL engines (identical to the
    chain's ``_apply``), returning (snapshots, final pstate, final ostate)."""
    snapshots: list[Any] = []
    for obs in observations:
        pr = presence.apply(
            pstate,
            TemporalInput(
                key=pkey,
                observation=obs,
                observation_kind=presence_kind(obs),
                processing_time=processing,
            ),
        )
        pstate = pr.state
        transition = pr.transitions[0]
        or_ = occupancy.apply(
            ostate,
            OccupancyInput(
                key=scope,
                transition=transition,
                observation_kind=occupancy_event_from_presence(transition),
                processing_time=processing,
            ),
        )
        ostate = or_.state
        if or_.snapshot is not None:
            snapshots.append(or_.snapshot)
    return snapshots, pstate, ostate


class TestFsmRestart:
    async def test_restart_from_checkpoint_reproduces_exact_facts(self) -> None:
        """E — an FSM restart (crash mid-stream, restore from the durable
        checkpoint into a FRESH engine) reproduces byte-identical facts —
        no duplicate logical fact, no lost event."""
        chain = _make_chain()
        source = await _source()
        await _run_cv(chain.consumer, source)
        observations = [
            chain.consumer.spatial_by_frame[i] for i in sorted(chain.consumer.spatial_by_frame)
        ]
        assert len(observations) >= 2

        # Continuous run (never crashed) — the golden snapshot sequence.
        presence_c = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=chain.policy)
        occupancy_c = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=chain.policy)
        continuous, _p, _o = _apply_all(
            presence_c,
            occupancy_c,
            presence_c.initial_state(chain.pkey),
            occupancy_c.initial_state(chain.scope),
            observations,
            pkey=chain.pkey,
            scope=chain.scope,
            processing=chain.processing,
        )

        # Restart at the split: engine A processes the first half, its
        # checkpoints are serialized, engine B (FRESH) restores them and
        # finishes — exactly the durable-restart contract.
        split = len(observations) // 2
        presence_a = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=chain.policy)
        occupancy_a = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=chain.policy)
        first, pstate_a, ostate_a = _apply_all(
            presence_a,
            occupancy_a,
            presence_a.initial_state(chain.pkey),
            occupancy_a.initial_state(chain.scope),
            observations[:split],
            pkey=chain.pkey,
            scope=chain.scope,
            processing=chain.processing,
        )
        cp_presence = presence_a.checkpoint(pstate_a)
        cp_occupancy = occupancy_a.checkpoint(ostate_a)

        presence_b = PresenceTemporalEngine(fsm=PRESENCE_FSM, policy=chain.policy)
        occupancy_b = OccupancyEngine(fsm=OCCUPANCY_FSM, policy=chain.policy)
        restarted, _p2, _o2 = _apply_all(
            presence_b,
            occupancy_b,
            presence_b.restore(cp_presence),
            occupancy_b.restore(cp_occupancy),
            observations[split:],
            pkey=chain.pkey,
            scope=chain.scope,
            processing=chain.processing,
        )

        # The pre-crash facts are byte-identical to the continuous run —
        # the enter snapshot (0 -> 1) is the ONE logical occupancy fact
        # and survives the restart with the same content-derived identity.
        assert [s.model_dump(mode="json") for s in first] == [
            s.model_dump(mode="json") for s in continuous
        ]
        assert len(continuous) == 1
        assert continuous[0].occupancy_count == 1
        # The restarted tail produces ZERO additional snapshots — the
        # restored FSM knows the count is already 1 and never re-fires
        # (no duplicate logical fact after the restart).
        assert restarted == []

    async def test_restore_rejects_version_drift_typed(self) -> None:
        """E — restoring a checkpoint under a different engine version or
        policy revision is an explicit typed rejection (never silent)."""
        chain = _make_chain()
        presence = chain.presence
        cp = presence.checkpoint(presence.initial_state(chain.pkey))

        with pytest.raises(FsmVersionMismatchError):
            presence.restore(cp.model_copy(update={"engine_version": "0.0"}))
        with pytest.raises(CheckpointIntegrityError):
            presence.restore(cp.model_copy(update={"policy_revision": "v2"}))


# =============================================================================
# F. DATABASE FAILURE
# =============================================================================


class _FlakySession(PersistenceSession):
    """A transaction whose flush fails once (DB connection lost mid-tx)."""

    def __init__(self, store: FakeStore, *, fail_once: bool = True) -> None:
        super().__init__(store)
        self._fail = fail_once

    async def flush(self) -> None:
        if self._fail:
            self._fail = False
            msg = "database connection lost"
            raise RuntimeError(msg)
        await super().flush()


class TestDatabaseFailure:
    async def test_db_outage_during_persist_leaves_nothing_durable_and_recovers(self) -> None:
        """F — the DB going down mid-transaction propagates the raw error
        (never treated as replay), leaves NOTHING durable, and a retry
        after recovery commits exactly one clean set."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        actor = chain.actor

        store = FakeStore()
        session = _FlakySession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        with pytest.raises(RuntimeError, match="connection lost"):
            await service.persist(session, fact=snapshot, event=event, actor=actor)
        # No silent corruption / no partial commit.
        assert store.count() == 0

        # Correct recovery: a fresh transaction commits exactly one of each.
        session2 = PersistenceSession(store)
        result = await service.persist(session2, fact=snapshot, event=event, actor=actor)
        assert result.created is True
        await session2.commit()
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1

    async def test_db_outage_is_not_misclassified_as_replay(self) -> None:
        """F — a database outage is NOT an idempotency replay: the failure
        propagates and the failed write never records the event as durable."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        actor = chain.actor

        store = FakeStore()
        session = _FlakySession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        with pytest.raises(RuntimeError):
            await service.persist(session, fact=snapshot, event=event, actor=actor)
        # The failed write left no durable trace (nothing to dedup against).
        assert store.outbox_by_event == {}


# =============================================================================
# G. TRANSACTION ROLLBACK
# =============================================================================


class TestTransactionRollback:
    async def test_mid_transaction_failure_rolls_back_all_four_rows(self) -> None:
        """G — a failure AFTER the fact/event rows are staged but BEFORE
        the outbox enqueue aborts the WHOLE transaction: no orphan fact or
        event ever appears; a retry commits one clean set."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        actor = chain.actor

        store = FakeStore()
        outbox = PersistenceOutbox(store)
        outbox.enqueue_failure = RuntimeError("outbox table unavailable")
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=outbox)
        with pytest.raises(RuntimeError, match="outbox table unavailable"):
            await service.persist(session, fact=snapshot, event=event, actor=actor)
        # The four rows roll back together — nothing durable.
        assert store.count() == 0

        # Correct recovery: the fault clears; one clean set commits.
        outbox.enqueue_failure = None
        session2 = PersistenceSession(store)
        result = await service.persist(session2, fact=snapshot, event=event, actor=actor)
        assert result.created is True
        await session2.commit()
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1

    async def test_explicit_rollback_discards_all_four_retry_commits_once(self) -> None:
        """G — an explicit rollback (transaction aborted before COMMIT)
        discards all four rows; the retry commits exactly one logical set."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        actor = chain.actor

        store = FakeStore()
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        result = await service.persist(session, fact=snapshot, event=event, actor=actor)
        assert result.created is True
        await session.rollback()
        assert store.count() == 0

        session2 = PersistenceSession(store)
        result2 = await service.persist(session2, fact=snapshot, event=event, actor=actor)
        assert result2.created is True
        await session2.commit()
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1

    async def test_genuine_integrity_failure_propagates_never_masked_as_replay(self) -> None:
        """G — only the KNOWN duplicate constraints are treated as replay; a
        genuine (non-duplicate) integrity failure propagates explicitly and
        rolls the partial rows back (no silent dedup of real corruption)."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        actor = chain.actor

        store = FakeStore()
        outbox = PersistenceOutbox(store)
        outbox.enqueue_failure = _integrity("fk_operational_events_configuration_version")
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=outbox)
        with pytest.raises(IntegrityError):
            await service.persist(session, fact=snapshot, event=event, actor=actor)
        assert store.count() == 0


# =============================================================================
# H. OUTBOX WORKER CRASH
# =============================================================================


class TestOutboxWorkerCrash:
    async def test_publisher_crash_after_publish_reclaims_and_delivers_once(self) -> None:
        """H — the publisher crashing AFTER the Redis write never loses the
        committed event: the outbox row survives, the lease expires, a new
        worker reclaims and re-publishes (at-least-once), and the dedup +
        idempotent effect yield ONE logical evidence request."""
        _chain, events, store = await _persisted_run()
        event_id = uuid.UUID(str(events[0].event_id))

        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        row = pipeline.outbox_by_event[event_id]
        assert row.status == "pending"  # durable, committed by 18.10

        # Crash after publish: the row stays processing with a live lease.
        assert pipeline.publish_once("publisher-1", crash_after_publish=True) == 0
        assert row.status == "processing"
        assert len(pipeline.stream) == 1
        # A second worker cannot double-process while the lease is live.
        assert pipeline.claim_outbox("publisher-2") == []

        # Lease expiry → recovery re-publishes (at-least-once).
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-2") == 1
        assert row.status == "published"  # no lost committed event

        # Two stream messages, but the bridge dedups to ONE inbox row.
        assert len(pipeline.stream) == 2
        assert pipeline.relay_once() == 2
        assert len(pipeline.inbox) == 1

        # One effect → ONE durable evidence request.
        evidence = FakeEvidenceLinkageRepository()
        handlers = build_operational_effect_handlers(
            evidence_linkage=EvidenceLinkageService(repository=evidence),
        )
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 1
        assert {r.status for r in pipeline.inbox.values()} == {"processed"}
        assert len(evidence.rows) == 1
        (ref_row,) = evidence.rows.values()
        assert ref_row.event_id == event_id


# =============================================================================
# I. DUPLICATE MESSAGE
# =============================================================================


class TestDuplicateMessage:
    async def test_duplicate_stream_delivery_collapses_to_one_effect(self) -> None:
        """I — the same event delivered twice on the stream is deduplicated
        by the inbox's (source, event_id) key: ONE inbox row, ONE effect,
        ONE evidence request — and the second delivery is acknowledged
        without inserting anything."""
        _chain, events, store = await _persisted_run()
        event_id = uuid.UUID(str(events[0].event_id))

        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        row = pipeline.outbox_by_event[event_id]
        assert pipeline.publish_once("publisher-1") == 1
        # Duplicate delivery: the SAME event is delivered again on the stream.
        pipeline.republish(row)
        assert len(pipeline.stream) == 2

        # The bridge dedups: 2 messages read+acked, exactly 1 inbox row.
        assert pipeline.relay_once() == 2
        assert len(pipeline.acked) == 2
        assert len(pipeline.inbox) == 1

        # One effect → one evidence request; the duplicate changed nothing.
        evidence = FakeEvidenceLinkageRepository()
        handlers = build_operational_effect_handlers(
            evidence_linkage=EvidenceLinkageService(repository=evidence),
        )
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 1
        assert await pipeline.consume_once("consumer-2", handlers=handlers) == 0
        assert len(evidence.rows) == 1

    async def test_duplicate_persist_records_replay_telemetry_and_writes_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """I — re-persisting the same logical event is detected by the Task
        7 pre-check: structured telemetry records the replay and NOTHING is
        written (one logical fact/event/audit/outbox)."""
        chain, events, store = await _persisted_run()
        event = events[0]
        snapshot = chain.consumer.snapshots[0]
        actor = chain.actor

        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        with caplog.at_level(
            logging.INFO, logger="backend.app.application.services.operational_persistence"
        ):
            result = await service.persist(session, fact=snapshot, event=event, actor=actor)
        # Correct error classification: a duplicate is a replay, not an error.
        assert result.created is False
        assert result.replayed is True
        # Correct structured telemetry: the replay is logged with the id.
        messages = [
            r.getMessage()
            for r in caplog.records
            if r.name == "backend.app.application.services.operational_persistence"
        ]
        assert any("replayed" in m and str(event.event_id) in m for m in messages)
        # No duplicate business records anywhere.
        assert len(store.facts) == 1
        assert len(store.events) == 1
        assert len(store.audits) == 1
        assert len(store.outbox) == 1


# =============================================================================
# J. EVIDENCE EXTRACTION FAILURE (the real Task 17.11 worker)
# =============================================================================


class TestEvidenceExtractionFailure:
    async def test_transient_extraction_failure_retries_then_finalizes_one_package(
        self,
    ) -> None:
        """J — a transient extraction failure lands in RETRYABLE_FAILURE
        with a persisted backoff + last-error + audit (classified), the
        retry succeeds, and exactly ONE logical package is ever persisted."""
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref())
        clock = Clock()
        audit = RecordingAuditSink()
        extractor = FakeExtractor(
            behaviors=[_raise(RuntimeError("transcoder oom during extraction"))]
        )
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            audit=audit,
        )

        # Cycle 1: the extraction raises → RETRYABLE_FAILURE + backoff.
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert ref.metadata_ is not None
        assert EVIDENCE_RETRY_AT_KEY in ref.metadata_  # persisted backoff
        assert "transcoder oom" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY))
        # Structured telemetry: the retry is audited with the reason.
        assert audit.types() == [EVENT_RETRYABLE_FAILURE]
        assert len(store.packages) == 0  # nothing finalized

        # Cycle 2 (backoff elapsed): the retry succeeds → ONE package.
        clock.advance(seconds=120)
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1

        # Idempotent: another cycle persists no second package.
        await worker.run_once()
        assert len(store.packages) == 1

    async def test_corrupt_source_dead_letters_preserved(self) -> None:
        """J — an irrecoverable extraction (CORRUPT_SOURCE) is classified as
        TERMINAL_FAILURE: dead-lettered, preserved for audit, never retried,
        never silently dropped."""
        store = FakeEvidenceWorkStore()
        store.seed(_make_ref())
        clock = Clock()
        audit = RecordingAuditSink()
        extractor = FakeExtractor(
            behaviors=[_status(ExtractionStatus.CORRUPT_SOURCE)],
            repeat=True,
        )
        worker = _make_worker(
            store=store,
            extractor=extractor,
            candidates=FakeCandidates([_candidate()]),
            clock=clock,
            audit=audit,
        )

        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "terminal_failure"
        assert ref.metadata_ is not None
        assert "corrupt" in str(ref.metadata_.get(EVIDENCE_LAST_ERROR_KEY))
        # Structured telemetry + preserved for audit.
        assert audit.types() == [EVENT_TERMINAL_FAILURE]
        assert store.get(uuid.UUID(str(_REF))) is not None  # never deleted
        assert len(store.packages) == 0

        # Terminal is terminal: a further cycle changes nothing.
        await worker.run_once()
        assert _state(ref) == "terminal_failure"
        assert len(store.packages) == 0


# =============================================================================
# K. OBJECT STORAGE FAILURE
# =============================================================================


class TestObjectStorageFailure:
    async def test_storage_outage_fails_open_and_recovers(self) -> None:
        """K — the object store going down fails the source OPEN explicitly
        (FrameSourceError, FAILED — never a silent empty stream); when the
        store returns, a re-run produces the ONE golden event."""
        storage = await _uploaded_storage()
        storage.simulate_unavailable(True)

        source = await _source(storage=storage)
        with pytest.raises(FrameSourceError):
            await source.open()
        # Error classification + observable terminal state.
        assert source.state is FrameSourceState.FAILED
        await source.aclose()

        # Correct recovery: the store returns; the slice runs to the event.
        storage.simulate_unavailable(False)
        chain = _make_chain()
        source2 = await _source(storage=storage)
        await _run_cv(chain.consumer, source2)
        _results, events = _facts_and_events(chain.consumer)
        assert len(events) == 1
        assert events[0].event_id == await _golden_event_id()


# =============================================================================
# L. API UNAUTHORIZED — error classification across the auth boundary
# =============================================================================


class TestApiUnauthorized:
    def test_expired_token_classified_authentication(self) -> None:
        """L — an expired credential is AuthenticationError (401 semantics)."""
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(_expired_token(_settings()), _settings())

    async def test_missing_actor_classified_authentication(self) -> None:
        """L — a missing Authorization header is AuthenticationError (401)."""
        with pytest.raises(AuthenticationError, match="Missing Authorization header"):
            await get_token_data(credentials=None, settings=_settings())

    async def test_missing_permission_classified_authorization(self) -> None:
        """L — an authenticated actor WITHOUT the permission is
        AuthorizationError (403 semantics) — never silently downgraded."""
        gate = require_permission(Permission.ANALYTICS_READ)
        forged = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await gate(forged)

    async def test_wrong_tenant_classified_not_found(self) -> None:
        """L — an out-of-scope tenant is 404 (OperationalNotFoundError),
        indistinguishable from nonexistent — no enumeration, no leak."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(tenant_id=uuid.uuid4())
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_wrong_venue_classified_not_found(self) -> None:
        """L — a same-tenant actor scoped to another venue is 404."""
        event_row, fact_row = _slice_rows()
        session = _session_with(event_row, fact_row)
        actor = _actor(
            tenant_id=event_row.tenant_id,
            role=RoleName.OPERATOR,
            venue_ids=frozenset({VenueId(uuid.uuid4())}),
        )
        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_permission_gate_admits_authorized_roles(self) -> None:
        """L — the gate is not blanket-deny: an authorized manager/operator
        passes (the boundary is permission-precise, never a catch-all)."""
        gate = require_permission(Permission.ANALYTICS_READ)
        for role in (RoleName.MANAGER, RoleName.OPERATOR):
            await gate(_actor(tenant_id=uuid.uuid4(), role=role))  # no exception


# =============================================================================
# M. API DEPENDENCY FAILURE
# =============================================================================


class _BoomSession(ApiSession):
    """A request session whose repository read fails (DB down)."""

    async def execute(self, statement: Any) -> Any:
        msg = "database connection lost"
        raise RuntimeError(msg)


class _BoomRlsConnection:
    """A connection whose RLS statement fails (RLS layer unavailable)."""

    async def execute(self, statement: Any) -> None:
        msg = "SET LOCAL app.tenant_id failed"
        raise RuntimeError(msg)


class _BoomRlsSession(ApiSession):
    async def connection(self) -> Any:
        return _BoomRlsConnection()


class TestApiDependencyFailure:
    async def test_database_dependency_failure_propagates_no_fabricated_response(self) -> None:
        """M — the DB dependency failing inside the route propagates the
        error explicitly; the route NEVER fabricates a partial DTO."""
        event_row, fact_row = _slice_rows()
        session = _BoomSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
        )
        actor = _actor(tenant_id=event_row.tenant_id)
        with pytest.raises(RuntimeError, match="connection lost"):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_rls_dependency_failure_propagates_before_any_read(self) -> None:
        """M — the RLS scoping failing raises before any read can happen: a
        request is never served without its tenant RLS boundary."""
        event_row, fact_row = _slice_rows()
        session = _BoomRlsSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
        )
        actor = _actor(tenant_id=event_row.tenant_id)
        with pytest.raises(RuntimeError, match="SET LOCAL"):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_evidence_endpoint_dependency_failure_is_explicit(self) -> None:
        """M — the evidence availability endpoint has the same explicit
        dependency failure behavior (no fabricated availability answer)."""
        event_row, fact_row = _slice_rows()
        session = _BoomSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
        )
        actor = _actor(tenant_id=event_row.tenant_id)
        with pytest.raises(RuntimeError, match="connection lost"):
            await get_operational_event_evidence(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )


# =============================================================================
# STOP condition — the suite never defines its own rule/FSM/boundary
# =============================================================================


def test_suite_defines_no_rule_fsm_or_second_boundary() -> None:
    """STOP: this suite injects failures into the REAL boundaries only — it
    never declares its own rule, FSM, DTO, or repository."""
    source = pathlib.Path(__file__).read_text()
    body = source.split('"""', 2)[2]
    guard_start = body.index("def test_suite_defines_no_rule_fsm_or_second_boundary")
    non_guard = body[:guard_start]
    assert "RuleDefinition(" not in non_guard
    assert "FsmRule(" not in non_guard
    assert "DeterministicFsm(" not in non_guard
    assert "OperationalPersistenceService(" in non_guard
    assert "build_operational_effect_handlers(" in non_guard
    assert "get_operational_event(" in non_guard
