"""Task 18.14 — FIRST VERTICAL SLICE E2E TEST (the primary Task 18 acceptance test).

One deterministic end-to-end test through the REAL production boundaries:

    fixture (18.2)            → the committed manifest + 30 PNG frames
    authenticated actor (5)   → server-built ActorContext + ANALYTICS_READ
    published config (10)     → the ONE PUBLISHED configuration version
    FileFrameSource (18.3)    → FramePipeline → canonical FramePackets
    YOLO (18.4)               → YOLOv8Adapter (fixture-driven fake SDK seam)
    ByteTrack (18.5)          → ByteTrackAdapter (fixture-driven fake backend)
    ROI (18.6)                → extract_point → evaluate_spatial (pinned config)
    occupancy FSM (18.7)      → PresenceTemporalEngine → OccupancyEngine
    occupancy rule (18.8)     → the REGISTERED occupancy_session:v1 engine
    EventEnvelope (16)        → the deterministic rule output
    persistence (18.10)       → fact + event + audit + outbox, ONE commit
    outbox (18.11)            → lease → publish → inbox dedup → effect
    evidence (18.9/17)        → durable REQUESTED evidence request (ref_id)
    FastAPI (18.12)           → the REAL route → service → repository
    Tauri (18.13)             → the canonical DTO the desktop card renders

The scenario the task lists — fixture uploaded/registered → FileFrameSource
starts → FramePacket → YOLO detects → ByteTrack tracks → ROI membership →
occupancy FSM changes state → deterministic rule fires → EventEnvelope →
PostgreSQL transaction commits (fact + audit + outbox) → worker processes →
EvidenceRef created → evidence linked → FastAPI retrieves → Tauri displays —
is walked in ONE test with an assertion at every step.

Seams (all documented, none bypass a business boundary):

- the storage/decoder ports are the Task 9 ``FakeStorageAdapter`` and the
  fixture's in-memory decoder (the manifest declares ``no_network: true`` —
  real S3/ffmpeg are external);
- the Ultralytics and ByteTrack SDKs are injected behind the adapters'
  lazy seams (exactly like 18.4/18.5 — the adapters' real code runs);
- ``FrameSource._make_packet`` draws ``FramePacket.frame_id`` from uuid4;
  the frame id participates in the content-derived transition/snapshot/
  event identities, so the E2E pins it to a deterministic sequence — the
  REAL source runs unchanged, only the id source is reproducible;
- the transaction store, Task 7 pipeline, and evidence repository are the
  same in-memory models the 18.10/18.11/18.9 tests use (faithful to the
  SQL/unique-key/lease semantics the boundaries rely on).

STOP conditions (the task's): every stage is the real component (the only
synthesis is the documented 18.6 boundary interception — a centroid
exactly on the ROI edge is NEVER silently classified; the slice represents
that instant as a policy-intercepted not_observed observation); the route
accepts no tenant/venue input; the API returns only canonical DTOs (the
exact wire shape the 18.13 desktop card renders); and the whole run is
reproducible — the same fixture + same versions always produce the same
logical event identity.
"""

from __future__ import annotations

import itertools
import sys
import types
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from backend.app.api.routes.operational import (
    get_operational_event,
    get_operational_event_evidence,
)
from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.application.services.operational_persistence import (
    OperationalPersistenceService,
)
from backend.app.infrastructure.auth.deps import require_permission
from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.storage.fake import FakeStorageAdapter
from backend.app.intelligence.detectors import DetectionInput
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from backend.app.intelligence.geometry import extract_point
from backend.app.intelligence.pipeline import FramePipeline
from backend.app.intelligence.sources import base as sources_base
from backend.app.intelligence.sources.file import FileFrameSource
from backend.app.intelligence.sources.queue import (
    BoundedFrameQueue,
    QueuedFrame,
    QueueFullPolicy,
)
from backend.app.intelligence.spatial.engine import (
    SpatialEvaluationInput,
    evaluate_spatial,
)
from backend.app.intelligence.spatial.exceptions import BoundaryPolicyUndefinedError
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
from backend.app.intelligence.tracking.base import TrackerConfig, TrackingInput, track_uuid
from backend.app.intelligence.tracking.bytetrack_adapter import ByteTrackAdapter
from backend.app.workers.operational_effects import build_operational_effect_handlers
from contracts.common import EventId, VideoAssetId
from contracts.events import EventEnvelope
from contracts.geometry import CoordinateSpace
from contracts.identity import Permission
from contracts.operational import EvidenceAvailabilityResponse, OccupancyEventResponse
from contracts.rules import (
    OccupancySessionPhase,
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
from contracts.temporal import OccupancySnapshot, TemporalStateKey
from contracts.vision import TrackState
from tests.unit.fakes import make_actor
from tests.unit.test_vertical_slice_api import FakeSession as ApiSession
from tests.unit.test_vertical_slice_detection import (
    install_fake_sdk,
    make_config,
    make_spec,
)
from tests.unit.test_vertical_slice_evidence import (
    FakeEvidenceLinkageRepository,
    _request_contract,
)
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
)
from tests.unit.test_vertical_slice_rule import (
    _evaluate_snapshots,
    _event_at,
    _identities,
    _load_manifest,
    _processing,
    _slice_policy,
)
from tests.unit.test_vertical_slice_spatial import _published_configuration
from tests.unit.test_vertical_slice_tracking import (
    TRACKER_SDK_VERSION,
    FakeBYTETracker,
    _plan_for_fixture,
)

pytestmark = pytest.mark.e2e

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "vertical_slice"
MANIFEST = _load_manifest()
IDS = _identities(MANIFEST)
OBJECT_KEY = "tenants/fixture/vertical_slice/recording.bin"
ASSET_ID = VideoAssetId(uuid.uuid5(uuid.uuid5(UUID(int=0), "hotelops-slice"), "e2e-slice-asset"))

# The wire shape the FastAPI route returns is EXACTLY the DTO the desktop
# consumes (desktop/src/api/types/operational.ts — OccupancyEventResponse).
# The 18.13 card test renders this same shape; the E2E proves the backend
# emits nothing else (no ORM-internal columns leak through).
DESKTOP_EVENT_DTO_KEYS = frozenset({
    "event_id",
    "event_type",
    "schema_version",
    "tenant_id",
    "venue_id",
    "session_id",
    "camera_id",
    "event_time",
    "produced_at",
    "source",
    "correlation_id",
    "causation_id",
    "payload",
})


# =============================================================================
# Deterministic seams (see the module docstring — none bypass a boundary)
# =============================================================================


class _DeterministicFrameIds:
    """Deterministic stand-in for the uuid4 the REAL FileFrameSource draws
    for ``FramePacket.frame_id`` (sources/base.py ``_make_packet``).

    The frame id participates in the content-derived presence transition,
    occupancy snapshot, and event identities, so pinning it is what makes
    the E2E reproducible THROUGH the real source. The source's code path is
    unchanged — only the id source is deterministic.
    """

    _NAMESPACE = UUID(int=0)

    def __init__(self) -> None:
        self._counter = itertools.count()

    def reset(self) -> None:
        self._counter = itertools.count()

    def next(self) -> uuid.UUID:
        n = next(self._counter)
        return uuid.uuid5(self._NAMESPACE, f"vertical-slice-e2e-frame-{n:04d}")


_FRAME_IDS = _DeterministicFrameIds()


def _install_deterministic_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the deterministic seams every E2E run needs.

    - the REAL YOLOv8Adapter's lazy SDK seam → the fixture-driven fake SDK;
    - the REAL ByteTrackAdapter's lazy SDK seam → the fixture-driven fake
      backend (one stable track per visible frame);
    - the REAL FileFrameSource's frame-id source → deterministic ids.

    Shared with the 18.15 replay test so replays of the full chain run
    under the identical seams.
    """
    install_fake_sdk(monkeypatch, MANIFEST)

    from backend.app.intelligence.tracking import bytetrack_adapter as tracker_module

    FakeBYTETracker.instances = []
    FakeBYTETracker.update_plan = []
    module = types.ModuleType("bytetrack")
    module.BYTETracker = FakeBYTETracker
    module.__version__ = TRACKER_SDK_VERSION
    monkeypatch.setitem(sys.modules, "bytetrack", module)
    monkeypatch.setattr(tracker_module, "_to_dets_array", lambda rows: rows)

    monkeypatch.setattr(sources_base, "uuid4", _FRAME_IDS.next)


@pytest.fixture(autouse=True)
def _deterministic_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse wrapper — the 18.15 replay test imports the installer."""
    _install_deterministic_seams(monkeypatch)


async def _object_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _open_source() -> tuple[FileFrameSource, Any, FakeStorageAdapter]:
    """Fixture uploaded/registered + FileFrameSource over the Task 9 port.

    The recording object is the concatenated fixture frames (a stand-in for
    the recorded-video container — deterministic, no network), decoded by
    the fixture's in-memory decoder.
    """
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
    decoder = _FixtureDecoder(_build_decoded_frames(MANIFEST))
    source = FileFrameSource(
        session_id=IDS["session_id"],
        source_ref=ASSET_ID,
        storage=storage,
        object_key=OBJECT_KEY,
        decoder=decoder,
        capture_time=datetime.fromisoformat(MANIFEST["metadata"]["capture_time"]),
    )
    return source, decoder, storage


# =============================================================================
# The CV-side chain consumer (REAL components, canonical (packet, data) pairs)
# =============================================================================


class _ChainConsumer:
    """The FramePipeline consumer that runs the REAL CV chain per frame:

        FramePacket + FrameData → YOLO adapter → ByteTrack adapter
            → extract_point → evaluate_spatial → presence → occupancy

    The ONLY synthesis is the documented boundary interception: when the
    REAL spatial engine refuses an on-edge centroid
    (``BoundaryPolicyUndefinedError`` — Task 14 records that BOUNDARY is
    NEVER silently converted to INSIDE/OUTSIDE), the slice represents that
    unclassifiable instant as a policy-intercepted ``not_observed``
    observation — the exact 18.6/18.7 convention. Nothing else is invented.
    """

    def __init__(
        self,
        *,
        yolo: YOLOv8Adapter,
        tracker: ByteTrackAdapter,
        configuration: Any,
        camera_id: Any,
        presence: PresenceTemporalEngine,
        occupancy: OccupancyEngine,
        pkey: TemporalStateKey,
        scope: TemporalStateKey,
        processing: datetime,
    ) -> None:
        self.yolo = yolo
        self.tracker = tracker
        self.configuration = configuration
        self.camera_id = camera_id
        self.presence = presence
        self.occupancy = occupancy
        self.pkey = pkey
        self.scope = scope
        self.processing = processing
        self.pstate = presence.initial_state(pkey)
        self.ostate = occupancy.initial_state(scope)
        # Observable chain output (asserted by the E2E).
        self.packets_seen: list[int] = []
        self.detections_by_frame: dict[int, list[Any]] = {}
        self.tracks_by_frame: dict[int, list[Any]] = {}
        self.spatial_by_frame: dict[int, SpatialObservation] = {}
        self.snapshots: list[OccupancySnapshot] = []
        self.boundary_interceptions = 0
        self.first_detection: Any | None = None
        self.first_track: Any | None = None

    async def consume(self, frame: QueuedFrame) -> None:
        packet = frame.packet
        data = frame.data
        self.packets_seen.append(packet.frame_index)

        # 6. YOLO detects (real adapter; the fake SDK serves the golden
        # predictions of the frame whose PNG bytes were decoded).
        detections = await self.yolo.detect(DetectionInput(frame=packet, image=data.data))
        # 7. ByteTrack generates the track (real adapter; the fake backend
        # serves one stable track while the person is visible).
        tracks = await self.tracker.update(TrackingInput(frame=packet, detections=detections))
        self.detections_by_frame[packet.frame_index] = detections
        self.tracks_by_frame[packet.frame_index] = tracks
        if detections and self.first_detection is None:
            self.first_detection = detections[0]
        if tracks and self.first_track is None:
            self.first_track = tracks[0]

        by_id = {det.detection_id: det for det in detections}
        for track in tracks:
            # A TERMINATED observation means the entity is gone — there is
            # no spatial membership to evaluate for it.
            if track.track_state is TrackState.TERMINATED:
                continue
            detection = by_id.get(track.detection_id)
            if detection is None:
                continue
            # 8. ROI spatial membership (real geometry + spatial engine).
            observation = self._spatial(packet, track, detection)
            self.spatial_by_frame[packet.frame_index] = observation
            # 9. occupancy FSM over the confirmed presence transitions.
            self._apply(observation)

    def _spatial(self, packet: Any, track: Any, detection: Any) -> SpatialObservation:
        """Real Step 2 point extraction + real spatial engine, or the
        documented policy interception for the on-edge boundary frame."""
        normalized = extract_point(detection.bounding_box, SpatialPointPolicy.CENTROID)
        point = SpatialPointModel(
            x=normalized.x * packet.width,
            y=normalized.y * packet.height,
            coordinate_space=CoordinateSpace.VENUE_LOCAL,
            policy=SpatialPointPolicy.CENTROID,
        )
        try:
            result = evaluate_spatial(
                SpatialEvaluationInput(
                    configuration=self.configuration,
                    track=track,
                    camera_id=self.camera_id,
                    point=point,
                )
            )
            return result.observation
        except BoundaryPolicyUndefinedError:
            # Task 14: BOUNDARY is NEVER silently converted. The slice
            # records the unclassifiable instant as not_observed (EXCLUDED),
            # which presence treats as a missing observation — never an exit.
            self.boundary_interceptions += 1
            return SpatialObservation(
                session_id=track.session_id,
                track_id=track.track_id,
                frame_id=track.frame_id,
                event_time=track.event_time,
                camera_id=self.camera_id,
                configuration_version_id=self.configuration.configuration_version_id,
                spatial_point=point,
                status=SpatialStatus.EXCLUDED,
            )

    def _apply(self, observation: SpatialObservation) -> None:
        presence_result = self.presence.apply(
            self.pstate,
            TemporalInput(
                key=self.pkey,
                observation=observation,
                observation_kind=presence_kind(observation),
                processing_time=self.processing,
            ),
        )
        self.pstate = presence_result.state
        transition = presence_result.transitions[0]
        occ_result = self.occupancy.apply(
            self.ostate,
            OccupancyInput(
                key=self.scope,
                transition=transition,
                observation_kind=occupancy_event_from_presence(transition),
                processing_time=self.processing,
            ),
        )
        self.ostate = occ_result.state
        if occ_result.snapshot is not None:
            self.snapshots.append(occ_result.snapshot)


# =============================================================================
# The full E2E driver
# =============================================================================


@dataclass
class E2ERun:
    """Everything the primary acceptance test asserts against."""

    actor: Any
    source: FileFrameSource
    consumer: _ChainConsumer
    results: list[RuleEvaluationResult]
    events: list[EventEnvelope[Any]]
    snapshots: list[OccupancySnapshot]
    store: FakeStore
    evidence: FakeEvidenceLinkageRepository
    pipeline: FakePipeline
    api_event: OccupancyEventResponse
    api_evidence: EvidenceAvailabilityResponse

    @property
    def event(self) -> EventEnvelope[Any] | None:
        return self.events[0] if self.events else None


async def _run_e2e() -> E2ERun:
    """Run the FULL vertical slice once (deterministic)."""
    _FRAME_IDS.reset()
    FakeBYTETracker.update_plan = _plan_for_fixture(MANIFEST)

    # 1 + 2. authenticated tenant/venue + the ONE published configuration
    actor = make_actor(tenant_id=uuid.UUID(str(IDS["tenant_id"])))
    await require_permission(Permission.ANALYTICS_READ)(actor)
    configuration = _published_configuration(MANIFEST)

    # 3 + 4. fixture uploaded/registered + FileFrameSource starts
    source, _decoder, _storage = await _open_source()

    # The real CV adapters (behind their documented lazy-SDK seams).
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

    # The real temporal chain (engines + FSMs, the slice's explicit policy).
    # The presence key uses the tracker's deterministic session-scoped id —
    # the observation the chain produces carries exactly this track id.
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

    # 4 + 5. FileFrameSource runs through the REAL FramePipeline; every
    # canonical (FramePacket, FrameData) pair reaches the CV chain.
    pipeline = FramePipeline(
        queue=BoundedFrameQueue(maxsize=16, full_policy=QueueFullPolicy.BLOCK),
        consumer=consumer,
    )
    await pipeline.run(source)

    # 10 + 11. the REGISTERED occupancy rule over the confirmed facts.
    results = _evaluate_snapshots(MANIFEST, IDS, consumer.snapshots)
    events = [r.event for r in results if r.status is RuleEvaluationStatus.MATCH]
    assert events, "the slice must produce at least one logical occupancy event"

    # 12-14. authoritative persistence: fact + event + audit + outbox, ONE
    # commit (the four rows commit or roll back together — Task 7 outbox row
    # is the durability boundary; nothing is published before this commit).
    store = FakeStore()
    session = PersistenceSession(store)
    service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
    for snapshot, event in zip(consumer.snapshots, events, strict=True):
        persisted = await service.persist(session, fact=snapshot, event=event, actor=actor)
        assert persisted.created is True
    await session.commit()

    # 15-17. the Task 7 worker pipeline: lease → publish → inbox (dedup) →
    # the REAL effect handler → the durable REQUESTED evidence request.
    evidence = FakeEvidenceLinkageRepository()
    handlers = build_operational_effect_handlers(
        evidence_linkage=EvidenceLinkageService(repository=evidence),
    )
    task7 = FakePipeline()
    task7.seed_outbox(store)
    published = task7.publish_once("e2e-publisher")
    relayed = task7.relay_once()
    processed = await task7.consume_once("e2e-consumer", handlers=handlers)
    assert published == relayed == processed == len(events)

    # 18. FastAPI retrieval through the REAL route → service → repository
    # (authorized actor; the session fake is faithful to the tenant-scoped
    # SQL the repository emits).
    api_session = ApiSession(
        events={row.event_id: row for row in store.events.values()},
        facts={row.fact_id: row for row in store.facts.values()},
        evidence={ref.event_id: ref for ref in evidence.rows.values()},
    )
    event_id = EventId(events[0].event_id)
    api_event = await get_operational_event(
        event_id=event_id, actor=actor, _perm=None, session=api_session
    )
    api_evidence = await get_operational_event_evidence(
        event_id=event_id, actor=actor, _perm=None, session=api_session
    )

    return E2ERun(
        actor=actor,
        source=source,
        consumer=consumer,
        results=results,
        events=events,
        snapshots=consumer.snapshots,
        store=store,
        evidence=evidence,
        pipeline=task7,
        api_event=api_event,
        api_evidence=api_evidence,
    )


# =============================================================================
# The primary acceptance test — the full 19-step scenario
# =============================================================================


class TestPrimaryAcceptance:
    """Walk the ENTIRE scenario with an assertion at every step."""

    async def test_full_chain_produces_one_logical_event_and_complete_downstream(
        self,
    ) -> None:
        run = await _run_e2e()
        assert run.event is not None

        # 1. authenticated tenant/venue exists — the server-built actor is
        # scoped to the fixture's tenant and admitted by the permission gate.
        assert run.actor.tenant_id == IDS["tenant_id"]
        assert Permission.ANALYTICS_READ in run.actor.permissions

        # 2. published camera configuration exists — the pinned PUBLISHED
        # version (never "the latest"), whose camera matches the fixture.
        configuration = _published_configuration(MANIFEST)
        assert configuration.venue_id == IDS["venue_id"]
        assert configuration.cameras[0].camera_id == IDS["camera_id"]

        # 3. fixture uploaded/registered — the committed deterministic
        # fixture (manifest + 30 PNG frames) is the ONLY input.
        assert MANIFEST["metadata"]["no_network"] is True
        assert MANIFEST["metadata"]["deterministic"] is True

        # 4 + 5. FileFrameSource starts; every frame becomes a canonical
        # FramePacket (monotonic indices, shared session, deterministic
        # event times) and the source closed cleanly after EOF.
        assert run.source.state.value == "closed"
        assert run.consumer.packets_seen == list(range(MANIFEST["metadata"]["frame_count"]))

        # 6. YOLO detects the object — the real adapter, one person per
        # on-frame entry, provenance carries the governed model identity.
        first_detection = run.consumer.first_detection
        assert first_detection is not None
        assert first_detection.class_name == "person"
        assert first_detection.class_id == 0
        meta = first_detection.detector_metadata or {}
        assert meta["model_id"] == MANIFEST["model"]["id"]  # yolo-person-detector
        assert meta["model"] == MANIFEST["model"]["name"]  # yolov8n
        assert meta["model_version"] == MANIFEST["model"]["version"]  # 8.1.0
        assert meta["device"] == "cpu"

        # 7. ByteTrack generates ONE stable track for the one logical person.
        assert run.consumer.first_track is not None
        assert run.consumer.first_track.tracking_metadata["tracker"] == "bytetrack"
        assert run.consumer.first_track.tracking_metadata["tracker_version"] == TRACKER_SDK_VERSION
        expected_track = track_uuid(IDS["session_id"], 1)
        seen_tracks = {
            obs.track_id for frame_obs in run.consumer.tracks_by_frame.values() for obs in frame_obs
        }
        assert seen_tracks == {expected_track}

        # 8. ROI recognizes spatial membership — INSIDE with the pinned zone
        # on the inside interval; the single on-edge frame (frame 6) is the
        # documented policy interception (never a silent classification).
        for frame_index in range(7, 28):
            obs = run.consumer.spatial_by_frame[frame_index]
            assert obs.status is SpatialStatus.INSIDE
            assert obs.zone_profile_id == IDS["semantic_context"]
        assert run.consumer.boundary_interceptions == 1

        # 9. occupancy FSM changes state exactly once — the confirmed enter
        # (0 -> 1) is the only snapshot; the count never flickers.
        assert len(run.snapshots) == 1
        assert run.snapshots[0].previous_count == 0
        assert run.snapshots[0].delta == 1
        assert run.snapshots[0].occupancy_count == 1

        # 10 + 11. the deterministic rule fires → exactly ONE logical
        # occupancy event (STARTED), with full provenance.
        assert [r.status for r in run.results] == [RuleEvaluationStatus.MATCH]
        assert len(run.events) == 1  # exactly one logical occupancy event
        event = run.event
        assert event.event_type == RuleEventType.OCCUPANCY_SESSION.value
        assert event.payload.phase is OccupancySessionPhase.STARTED
        assert event.payload.occupancy_count == 1
        assert event.payload.tenant_id == IDS["tenant_id"]
        assert event.payload.venue_id == IDS["venue_id"]
        assert event.payload.session_id == IDS["session_id"]
        assert event.payload.camera_id == IDS["camera_id"]
        assert event.source == "rule:occupancy_session:v1"
        assert event.event_time == _event_at(MANIFEST, 8)  # the confirmed enter
        assert event.payload.configuration_version_id == IDS["configuration_version_id"]
        assert event.payload.rule_id == RuleIdentifier.OCCUPANCY_SESSION.value
        assert event.payload.rule_version == "v1"
        assert run.results[0].rule_version == "v1"

        # 12. the PostgreSQL transaction commits — one fact row + one event
        # row are durable (plus audit + outbox below).
        assert len(run.store.facts) == 1
        assert len(run.store.events) == 1
        fact_row = run.store.facts[uuid.UUID(str(run.snapshots[0].snapshot_id))]
        assert fact_row.tenant_id == uuid.UUID(str(IDS["tenant_id"]))
        assert fact_row.venue_id == uuid.UUID(str(IDS["venue_id"]))
        assert fact_row.configuration_version_id == uuid.UUID(str(IDS["configuration_version_id"]))
        assert OccupancySnapshot.model_validate(fact_row.payload) == run.snapshots[0]

        # 13. audit created — one audit row for the persisted event.
        assert len(run.store.audits) == 1
        audit = run.store.audits[0]
        assert audit.action == "operational.event.persisted"
        assert audit.tenant_id == uuid.UUID(str(IDS["tenant_id"]))

        # 14. outbox created — the durable publication unit (Task 7).
        assert len(run.store.outbox) == 1
        outbox_row = run.store.outbox_by_event[uuid.UUID(str(event.event_id))]
        assert outbox_row.event_id == uuid.UUID(str(event.event_id))
        restored = EventEnvelope[Any].model_validate(outbox_row.payload)
        assert restored.model_dump(mode="json") == event.model_dump(mode="json")

        # 15. worker processes the event — publish → inbox → effect, all
        # three stages ran; the outbox row is published, the inbox processed.
        assert {row.status for row in run.pipeline.outbox.values()} == {"published"}
        assert {row.status for row in run.pipeline.inbox.values()} == {"processed"}
        assert len(run.pipeline.stream) == 1
        assert len(run.pipeline.inbox) == 1

        # 16 + 17. EvidenceRef created; evidence is generated/linked — one
        # durable REQUESTED request with the full provenance chain.
        assert len(run.evidence.rows) == 1
        (ref_row,) = run.evidence.rows.values()
        assert ref_row.metadata_["processing_state"] == "requested"
        ref = _request_contract(ref_row)
        assert ref.event_id == event.event_id
        assert ref.tenant_id == IDS["tenant_id"]
        assert ref.venue_id == IDS["venue_id"]
        assert ref.video_session_id == IDS["session_id"]
        assert ref.camera_id == IDS["camera_id"]
        assert ref.event_time == event.event_time
        assert ref.configuration_version_id == IDS["configuration_version_id"]
        assert str(ref.rule_id) == RuleIdentifier.OCCUPANCY_SESSION.value
        assert str(ref.rule_version) == "v1"
        assert ref.metadata["source"] == event.source

        # 18. FastAPI retrieves the result — the canonical DTO through the
        # real route/service/repository, with the pinned values.
        assert isinstance(run.api_event, OccupancyEventResponse)
        assert run.api_event.event_id == EventId(event.event_id)
        assert run.api_event.tenant_id == IDS["tenant_id"]
        assert run.api_event.venue_id == IDS["venue_id"]
        assert run.api_event.session_id == IDS["session_id"]
        assert run.api_event.camera_id == IDS["camera_id"]
        assert run.api_event.event_time == event.event_time
        assert run.api_event.source == event.source
        assert run.api_event.payload.phase is OccupancySessionPhase.STARTED
        assert run.api_event.payload.occupancy_count == 1
        assert run.api_event.payload.configuration_version_id == IDS["configuration_version_id"]
        assert run.api_event.payload.rule_version == "v1"
        # Evidence availability is answered by the server from the durable
        # linkage — the desktop never derives it.
        assert isinstance(run.api_evidence, EvidenceAvailabilityResponse)
        assert run.api_evidence.event_id == EventId(event.event_id)
        assert run.api_evidence.available is True
        assert run.api_evidence.evidence_ref_id == EventId(ref_row.ref_id)

        # 19. Tauri receives/display result — the API returns exactly the
        # canonical DTO whose wire shape the 18.13 desktop card renders
        # (desktop/src/api/types/operational.ts); no ORM row leaks through.
        data = run.api_event.model_dump(mode="json")
        assert set(data) == DESKTOP_EVENT_DTO_KEYS
        for banned in ("ingestion_time", "created_at", "updated_at", "last_error"):
            assert banned not in data


# =============================================================================
# Determinism — the same fixture + same versions → the same logical event
# =============================================================================


class TestDeterminism:
    """The E2E is reproducible: two full runs (fresh engines, fresh stores,
    fresh workers) produce the SAME logical event end to end."""

    async def test_two_full_runs_are_logically_identical(self) -> None:
        first = await _run_e2e()
        second = await _run_e2e()

        assert first.event is not None and second.event is not None
        # The event identity is content-derived and reproducible.
        assert first.event.event_id == second.event.event_id
        assert first.event.model_dump_json() == second.event.model_dump_json()
        # The confirmed facts are byte-identical.
        assert [s.model_dump(mode="json") for s in first.snapshots] == [
            s.model_dump(mode="json") for s in second.snapshots
        ]
        # The evidence request identity is the same logical ref_id.
        assert {str(r.ref_id) for r in first.evidence.rows.values()} == {
            str(r.ref_id) for r in second.evidence.rows.values()
        }
        # The API answers are byte-identical (the desktop renders the same).
        assert first.api_event.model_dump(mode="json") == second.api_event.model_dump(mode="json")
        assert first.api_evidence.evidence_ref_id == second.api_evidence.evidence_ref_id
        # The outbox row payload round-trips to the same envelope.
        event_uuid = uuid.UUID(str(first.event.event_id))
        assert EventEnvelope[Any].model_validate(
            first.store.outbox_by_event[event_uuid].payload
        ).model_dump(mode="json") == EventEnvelope[Any].model_validate(
            second.store.outbox_by_event[event_uuid].payload
        ).model_dump(mode="json")


# =============================================================================
# STOP conditions — no boundary is bypassed
# =============================================================================


class TestStopConditions:
    async def test_desktop_receives_the_canonical_dto_never_the_orm_row(self) -> None:
        """The API surface returns ONLY the canonical response DTO — an ORM
        row is never exposed (the 18.13 card consumes this exact shape)."""
        run = await _run_e2e()
        assert isinstance(run.api_event, OccupancyEventResponse)
        assert not isinstance(run.api_event, OperationalEventModel)
        assert set(run.api_event.model_dump(mode="json")) == DESKTOP_EVENT_DTO_KEYS

    async def test_route_has_no_client_tenant_or_venue_input(self) -> None:
        """Authorization never relies on frontend filtering: the route
        accepts ONLY the resource id — tenant/venue come from the
        server-side ActorContext, so the desktop can never select a tenant
        (no tenant bypass)."""
        import inspect

        for endpoint in (get_operational_event, get_operational_event_evidence):
            params = set(inspect.signature(endpoint).parameters)
            assert "tenant_id" not in params
            assert "venue_id" not in params
            assert "actor" in params
            assert "session" in params

    def test_test_defines_no_rule_fsm_or_second_boundary(self) -> None:
        """STOP condition: the E2E reuses the REGISTERED rule, the packaged
        FSMs, and the REAL persistence/evidence boundaries — it never
        declares its own rule, FSM, DTO, or repository."""
        source = Path(__file__).read_text()
        body = source.split('"""', 2)[2]
        guard_start = body.index("def test_test_defines_no_rule_fsm_or_second_boundary")
        non_guard = body[:guard_start]
        assert "RuleDefinition(" not in non_guard
        assert "FsmRule(" not in non_guard
        assert "DeterministicFsm(" not in non_guard
        # The real boundaries are the ones used.
        assert "OperationalPersistenceService(" in non_guard
        assert "build_operational_effect_handlers(" in non_guard
        assert "get_operational_event(" in non_guard
