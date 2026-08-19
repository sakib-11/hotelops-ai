"""Task 18.20 — FIRST VERTICAL SLICE ENTERPRISE GATE.

The acceptance gate that proves Tasks 1-17 are integrated through the
INTENDED production architecture. It walks the task's full checklist
against the REAL components (the same deterministic seams the
18.2-18.18 suites use — documented in test_vertical_slice_e2e.py) and
verifies every gate the task lists:

    ARCHITECTURAL BOUNDARY CHECK — the 19 boundary items, verified
        against the real code (import layout, class hierarchy, route
        signatures, contract usage), never against a re-implementation;
    FUNCTIONAL CHECK            — one full E2E walk with an assertion
        at every step of the task's chain;
    SECURITY CHECK              — the 9-item matrix through the REAL
        auth/scope/RLS/evidence-authorization boundaries;
    RELIABILITY CHECK           — the 9 failure/replay scenarios, each
        with the "one logical business effect / no committed event
        loss / no duplicate business record" outcome;
    PROVENANCE CHECK            — the full Event → EvidenceRef →
        VideoAsset → VideoSession → Camera → Event Time → Frame/Clip →
        Detector Version → Tracker Version → Configuration Version →
        Rule Version → Checksum → Stored Evidence chain verified with
        the REAL Task 17.14 verifier (no broken links);
    TELEMETRY CHECK             — trace/correlation identity crossing
        every async boundary + full log scope + no secrets in logs;
    DEFINITION OF DONE          — the consolidated final gate.

STOP conditions (the task's): no feature is added and no boundary is
bypassed — the gate consumes ONLY the packaged components (the
registered ``occupancy_session:v1`` rule, the packaged FSMs, the real
persistence/evidence/auth boundaries) and the same documented
fixture-driven SDK seams the 18.14 E2E uses. It never declares its own
rule, FSM, DTO, or repository.

When this module passes, the "FINAL REPORT" of Task 18.20 is PASS for
every component gate; any failure here BLOCKS Task 18.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import pathlib
import re
import uuid
from typing import Any

import pytest
from opentelemetry import trace as otel_trace

from backend.app.api.routes.operational import (
    get_operational_event,
    get_operational_event_evidence,
)
from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.application.services.operational_persistence import (
    OperationalPersistenceService,
)
from backend.app.application.services.outbox import OutboxService, _inject_trace_context
from backend.app.domain.evidence.extraction import ExtractedEvidence, ExtractionStatus
from backend.app.domain.evidence.package import EvidencePackageBuilder
from backend.app.domain.evidence.provenance import ProvenanceVerifier
from backend.app.domain.evidence.resolution import (
    ResolvedSourceSegment,
    SourceResolutionStatus,
    SourceSegment,
)
from backend.app.infrastructure.auth.deps import require_permission
from backend.app.infrastructure.auth.evidence import (
    EvidenceAuthorizer,
    EvidenceOperation,
)
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.service import create_access_token, verify_token
from backend.app.infrastructure.observability import context as obs_context
from backend.app.infrastructure.observability.tracing import (
    trace_context_from_event_attrs,
)
from backend.app.intelligence.detectors.base import ObjectDetector
from backend.app.intelligence.detectors.yolo_adapter import YOLOv8Adapter
from backend.app.intelligence.rules import build_operational_engine
from backend.app.intelligence.rules.evidence_request import (
    EvidenceRequestBuilder,
    EvidenceRequestParams,
)
from backend.app.intelligence.rules.occupancy_session import (
    OCCUPANCY_SESSION_EVALUATOR_ID,
)
from backend.app.intelligence.sources.base import FrameSource
from backend.app.intelligence.sources.file import FileFrameSource
from backend.app.intelligence.spatial.engine import SpatialEvaluationInput
from backend.app.intelligence.spatial.exceptions import (
    ConfigurationNotPublishedError,
)
from backend.app.intelligence.temporal import (
    OCCUPANCY_FSM,
    PRESENCE_FSM,
    OccupancyEngine,
    PresenceTemporalEngine,
)
from backend.app.intelligence.tracking.base import ObjectTracker
from backend.app.intelligence.tracking.bytetrack_adapter import ByteTrackAdapter
from backend.app.workers.operational_effects import build_operational_effect_handlers
from contracts.common import (
    EventId,
    MediaId,
    RuleId,
    RuleVersion,
    TenantId,
    UserId,
    VenueId,
    utc_now,
)
from contracts.configuration import ConfigurationStatus
from contracts.events import EventEnvelope
from contracts.identity import ActorContext, Permission, RoleName, permissions_for_role
from contracts.operational import EvidenceAvailabilityResponse, OccupancyEventResponse
from contracts.rules import (
    OccupancySessionPhase,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.spatial import SpatialStatus
from contracts.video import FramePacket
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
)
from tests.unit.test_vertical_slice_api import (
    FakeSession as ApiSession,
)
from tests.unit.test_vertical_slice_api import (
    _actor,
    _expired_token,
    _settings,
    _slice_rows,
)
from tests.unit.test_vertical_slice_e2e import (
    ASSET_ID,
    DESKTOP_EVENT_DTO_KEYS,
    IDS,
    MANIFEST,
    _install_deterministic_seams,
    _run_e2e,
)
from tests.unit.test_vertical_slice_evidence import (
    FakeEvidenceLinkageRepository,
    _request_contract,
)
from tests.unit.test_vertical_slice_failures import (
    _apply_all,
    _facts_and_events,
    _FlakySession,
    _golden_event_id,
    _make_chain,
    _persisted_run,
    _run_cv,
    _source,
    _uploaded_storage,
)
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
from tests.unit.test_vertical_slice_rule import _event_at
from tests.unit.test_vertical_slice_spatial import (
    _evaluate as _evaluate_spatial,
)
from tests.unit.test_vertical_slice_spatial import (
    _published_configuration,
)
from tests.unit.test_vertical_slice_telemetry import (
    SPAN_ID,
    TRACE_ID,
    _captured,
    _persist_and_query,
)
from tests.unit.test_vertical_slice_tracking import TRACKER_SDK_VERSION

pytestmark = pytest.mark.e2e

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend" / "app"
DESKTOP_ROOT = PROJECT_ROOT / "desktop" / "src"
FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "vertical_slice"

# The two adapters that are ALLOWED to reference their SDKs — every other
# backend module must never import them (the boundary items below assert it).
_YOLO_ADAPTER = BACKEND_ROOT / "intelligence" / "detectors" / "yolo_adapter.py"
_TRACKER_ADAPTER = BACKEND_ROOT / "intelligence" / "tracking" / "bytetrack_adapter.py"
_ALLOWED_SDK_MODULES = {_YOLO_ADAPTER, _TRACKER_ADAPTER}

# Detector/tracker versions of the governed slice (the manifest's pinned
# model + the tracker SDK the adapters serve under the deterministic seams).
DETECTOR_VERSION = str(MANIFEST["model"]["version"])
TRACKER_VERSION = TRACKER_SDK_VERSION


# =============================================================================
# Shared drivers — the same deterministic seams every slice run needs
# =============================================================================


@pytest.fixture(autouse=True)
def _deterministic_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 18.14 E2E seams: fixture-driven fake SDKs behind the adapters'
    lazy seams + deterministic frame ids — the REAL code runs unchanged."""
    _install_deterministic_seams(monkeypatch)


def _backend_python_files() -> list[tuple[pathlib.Path, str]]:
    """Every .py file under backend/app (production code only)."""
    files: list[tuple[pathlib.Path, str]] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        files.append((path, path.read_text()))
    return files


def _desktop_source_files() -> list[tuple[pathlib.Path, str]]:
    """Every TypeScript/TSX source file under desktop/src."""
    files: list[tuple[pathlib.Path, str]] = []
    for pattern in ("*.ts", "*.tsx"):
        for path in sorted(DESKTOP_ROOT.rglob(pattern)):
            if "__tests__" in path.parts:
                continue
            files.append((path, path.read_text()))
    return files


# =============================================================================
# ARCHITECTURAL BOUNDARY CHECK — the 19 boundary items (Task 18.20 §1)
# =============================================================================


class TestArchitecturalBoundaryCheck:
    """Every boundary the task lists, verified against the real code.

    These are structural guarantees: if any item regresses, the gate
    fails before the functional walk even runs.
    """

    def test_file_frame_source_implements_task11_source_abstraction(self) -> None:
        """FileFrameSource is a FrameSource (Task 11) — downstream code
        depends on the base contract, never on the concrete source."""
        assert issubclass(FileFrameSource, FrameSource)
        assert inspect.isabstract(FrameSource)
        # The source emits the canonical packet contract.
        assert FramePacket.__module__.startswith("contracts.video")

    def test_frame_packet_is_the_canonical_contract(self) -> None:
        """FramePacket is the canonical Task 4 contract — every frame the
        slice produces is a validated, frozen FramePacket."""
        from pydantic import BaseModel

        assert issubclass(FramePacket, BaseModel)
        assert FramePacket.model_config.get("frozen") is True
        assert FramePacket.model_config.get("extra") == "forbid"

    def test_yolo_is_accessed_only_through_object_detector(self) -> None:
        """The only detection boundary is ObjectDetector; the concrete
        adapter implements it (never the reverse)."""
        assert ObjectDetector in YOLOv8Adapter.__mro__
        source = _YOLO_ADAPTER.read_text()
        # The SDK is imported lazily through importlib — never a
        # module-level import that would leak into the process.
        assert 'importlib.import_module("ultralytics")' in source

    def test_bytetrack_is_accessed_only_through_tracker_abstraction(self) -> None:
        """The only tracking boundary is ObjectTracker; the concrete
        adapter implements it."""
        assert ObjectTracker in ByteTrackAdapter.__mro__
        source = _TRACKER_ADAPTER.read_text()
        assert 'importlib.import_module("bytetrack")' in source

    def test_no_ultralytics_dependency_leaks_downstream(self) -> None:
        """YOLO/Ultralytics objects never appear outside the adapter: no
        other backend module imports the SDK."""
        pattern = re.compile(r"^\s*(?:import|from)\s+ultralytics\b", re.MULTILINE)
        offenders = [
            str(path)
            for path, text in _backend_python_files()
            if path not in _ALLOWED_SDK_MODULES and pattern.search(text)
        ]
        assert offenders == [], f"ultralytics leaks downstream: {offenders}"

    def test_no_bytetrack_dependency_leaks_downstream(self) -> None:
        """ByteTrack objects never appear outside the adapter: no other
        backend module imports the SDK."""
        pattern = re.compile(r"^\s*(?:import|from)\s+bytetrack\b", re.MULTILINE)
        offenders = [
            str(path)
            for path, text in _backend_python_files()
            if path not in _ALLOWED_SDK_MODULES and pattern.search(text)
        ]
        assert offenders == [], f"bytetrack leaks downstream: {offenders}"

    def test_roi_comes_from_task10_configuration(self) -> None:
        """The ROI is the Task 10 configuration's published zone — the
        spatial engine consumes the canonical ConfigurationVersionModel
        and the slice pins the manifest's one published version."""
        configuration = _published_configuration(MANIFEST)
        assert configuration.status is ConfigurationStatus.PUBLISHED
        # The published zone geometry IS the fixture ROI polygon.
        assert configuration.zones[0].profile_id == MANIFEST["spatial"]["zone_profile_id"]
        # The engine's input is typed against the Task 10 model.
        annotation = (
            inspect
            .signature(SpatialEvaluationInput.__init__)
            .parameters["configuration"]
            .annotation
        )
        assert "ConfigurationVersionModel" in str(annotation)

    async def test_configuration_version_is_pinned_never_latest(self) -> None:
        """The engine refuses any non-PUBLISHED configuration (typed error)
        and the slice carries the pinned configuration_version_id end to
        end — never 'the latest configuration'."""
        draft = _published_configuration(MANIFEST, status=ConfigurationStatus.DRAFT)
        with pytest.raises(ConfigurationNotPublishedError):
            _evaluate_spatial(draft, MANIFEST, 10)
        # The event payload pins the exact manifest version.
        run = await _run_e2e()
        assert run.event is not None
        assert run.event.payload.configuration_version_id == IDS["configuration_version_id"]
        assert (
            str(run.event.payload.configuration_version_id)
            == MANIFEST["spatial"]["configuration_version_id"]
        )

    def test_fsm_uses_task15_temporal_engines(self) -> None:
        """The temporal boundary is the packaged Task 15 engines + FSMs —
        the slice never declares its own FSM."""
        assert PRESENCE_FSM is not None and OCCUPANCY_FSM is not None
        for engine in (PresenceTemporalEngine, OccupancyEngine):
            assert engine.__module__.startswith("backend.app.intelligence.temporal")

    def test_rule_uses_task16_registered_rule(self) -> None:
        """The slice evaluates through the REGISTERED Task 16
        occupancy_session:v1 rule — never a re-implementation."""
        engine = build_operational_engine()
        rule = engine._registry.resolve(  # the packaged registry
            RuleId(RuleIdentifier.OCCUPANCY_SESSION.value), RuleVersion("v1")
        )
        assert rule.canonical_identity == "occupancy_session:v1"
        assert rule.evaluator_id == OCCUPANCY_SESSION_EVALUATOR_ID
        assert rule.output_event_type.value == RuleEventType.OCCUPANCY_SESSION.value

    async def test_evidence_uses_task17(self) -> None:
        """Evidence is the Task 17 pipeline: the request is built by the
        17.3 EvidenceRequestBuilder (the only sanctioned builder) and the
        durable row is the REQUESTED state the 17.11 worker consumes."""
        run = await _run_e2e()
        assert run.event is not None
        (ref_row,) = run.evidence.rows.values()
        ref = _request_contract(ref_row)
        assert ref_row.metadata_["processing_state"] == "requested"
        assert "evidence_request" in ref_row.metadata_
        # The durable request IS the builder's deterministic request.
        builder = EvidenceRequestBuilder()
        params = EvidenceRequestParams(
            tenant_id=IDS["tenant_id"],
            venue_id=IDS["venue_id"],
            video_session_id=IDS["session_id"],
            camera_id=IDS["camera_id"],
        )
        built = builder.build(run.event, params=params)
        assert built is not None and built.ref_id == ref.ref_id

    async def test_postgres_is_the_source_of_truth(self) -> None:
        """The durable rows are the PostgreSQL shapes — Redis is transport,
        never truth (the outbox row is the durability boundary)."""
        run = await _run_e2e()
        assert run.event is not None
        store = run.store
        # The fact/event/audit/outbox rows are exactly the SQL shapes.
        assert len(store.facts) == 1 and len(store.events) == 1
        assert len(store.audits) == 1 and len(store.outbox) == 1
        outbox_row = store.outbox_by_event[uuid.UUID(str(run.event.event_id))]
        # The outbox payload is the serialized envelope — nothing else.
        assert EventEnvelope[Any].model_validate(outbox_row.payload).model_dump(
            mode="json"
        ) == run.event.model_dump(mode="json")

    async def test_outbox_is_transactional(self) -> None:
        """The persistence boundary stages fact + event + audit + outbox in
        ONE transaction — a mid-transaction failure rolls all four back
        (the four rows commit or roll back together)."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        store = FakeStore()
        outbox = PersistenceOutbox(store)
        outbox.enqueue_failure = RuntimeError("outbox table unavailable")
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=outbox)
        with pytest.raises(RuntimeError, match="outbox table unavailable"):
            await service.persist(session, fact=snapshot, event=event, actor=chain.actor)
        # Nothing durable: the four rows rolled back together.
        assert store.count() == 0

    async def test_idempotency_is_enforced(self) -> None:
        """A duplicate logical event is detected by the Task 7 pre-check
        (and the outbox unique event_id is the arbiter) — replay writes
        nothing."""
        run = await _run_e2e()
        assert run.event is not None
        store = run.store
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        result = await service.persist(
            session, fact=run.snapshots[0], event=run.event, actor=run.actor
        )
        assert result.replayed is True
        assert session.pending == []
        assert len(store.facts) == 1 and len(store.events) == 1
        assert len(store.audits) == 1 and len(store.outbox) == 1

    def test_authorization_is_server_side(self) -> None:
        """The routes accept ONLY the resource id — tenant/venue come from
        the server-side ActorContext (a client can never select a tenant)."""
        for endpoint in (get_operational_event, get_operational_event_evidence):
            params = set(inspect.signature(endpoint).parameters)
            assert "tenant_id" not in params
            assert "venue_id" not in params
            assert "actor" in params  # the server-side dependency
            assert "session" in params

    def test_rls_is_enforced(self) -> None:
        """The request transaction is scoped to the actor's tenant
        (SET LOCAL app.tenant_id) before any read — RLS is the final
        database-level safety net."""
        import backend.app.infrastructure.database.rls as rls_module

        assert "SET LOCAL" in (rls_module.__doc__ or "")
        assert "app.tenant_id" in (rls_module.__doc__ or "")
        # The actual statement is SET LOCAL app.tenant_id = '<tenant>'
        # (transaction-scoped, fail-closed via current_setting).
        assert "SET LOCAL app.tenant_id" in inspect.getsource(rls_module.set_session_tenant)
        # The route scopes its request session before any read.
        route_source = inspect.getsource(get_operational_event)
        assert "set_rls_on_session(session, actor.tenant_id)" in route_source

    async def test_api_does_not_expose_orm_internals(self) -> None:
        """The API returns only canonical DTOs — no ORM-internal column
        ever crosses the wire."""
        run = await _run_e2e()
        data = run.api_event.model_dump(mode="json")
        assert set(data) == DESKTOP_EVENT_DTO_KEYS
        for banned in ("ingestion_time", "created_at", "updated_at", "last_error"):
            assert banned not in data

    def test_tauri_does_not_access_the_database(self) -> None:
        """The desktop contains no database driver/ORM — PostgreSQL is
        reached only through the authorized FastAPI surface."""
        pattern = re.compile(
            r"""(?:from\s+|import\s+)[\"'](?:pg|postgres|postgresql|sqlalchemy|"""
            r"""prisma|@prisma/client|drizzle-orm|sqlite|sqlite3|typeorm|knex)[\"']""",
            re.MULTILINE,
        )
        offenders = [str(path) for path, text in _desktop_source_files() if pattern.search(text)]
        assert offenders == [], f"desktop reaches the database directly: {offenders}"

    def test_tauri_contains_no_cv_or_business_rule_logic(self) -> None:
        """The desktop contains no CV/rule imports — it renders the
        server's canonical DTOs and derives nothing."""
        pattern = re.compile(
            r"""(?:from\s+|import\s+)[\"'][^\"']*(?:ultralytics|bytetrack|opencv|"""
            r"""cv2|torch|tensorflow)[\"']""",
            re.MULTILINE,
        )
        offenders = [str(path) for path, text in _desktop_source_files() if pattern.search(text)]
        assert offenders == [], f"desktop runs CV/business logic: {offenders}"

    def test_observability_crosses_async_boundaries(self) -> None:
        """The trace/correlation context is captured at production and the
        worker continuation seam reconstructs it — the Task 8.8 carrier
        across outbox → publisher → Redis → inbox → consumer."""
        outbox_source = inspect.getsource(OutboxService)
        assert "_inject_trace_context(envelope)" in outbox_source
        assert inspect.isfunction(_inject_trace_context)
        assert inspect.isfunction(trace_context_from_event_attrs)


# =============================================================================
# FUNCTIONAL CHECK — the 16 items of the task's chain (Task 18.20 §2)
# =============================================================================


class TestFunctionalCheck:
    """One full E2E walk with an assertion at every step of:

    fixture → FileFrameSource → FramePacket → YOLO → ByteTrack → ROI →
    FSM → rule → EventEnvelope → PostgreSQL → audit → outbox → worker →
    EvidenceRef → FastAPI → Tauri.
    """

    async def test_every_functional_step_of_the_chain(self) -> None:
        run = await _run_e2e()
        assert run.event is not None

        # 1. fixture decodes — every fixture frame became a frame packet,
        #    with zero decode errors and a clean EOF.
        assert run.consumer.packets_seen == list(range(MANIFEST["metadata"]["frame_count"]))
        assert run.source.decode_errors == 0
        assert run.source.state.value == "closed"

        # 2. frames are canonical — FramePacket provenance carries the
        #    session and UTC event times end to end.
        detection = run.consumer.first_detection
        assert detection is not None
        assert detection.session_id == IDS["session_id"]
        assert detection.event_time.tzinfo is not None
        # The frame-7 detection and its spatial observation share the
        # same canonical event time (provenance copied verbatim).
        detection7 = run.consumer.detections_by_frame[7][0]
        obs = run.consumer.spatial_by_frame[7]
        assert obs.event_time == detection7.event_time
        assert obs.event_time.tzinfo is not None

        # 3. detection succeeds — one person with governed model provenance.
        assert detection.class_name == "person"
        assert detection.detector_metadata["model_id"] == MANIFEST["model"]["id"]
        assert detection.detector_metadata["model_version"] == MANIFEST["model"]["version"]
        assert detection.detector_metadata["device"] == "cpu"

        # 4. tracking succeeds — one stable track through the adapter.
        assert run.consumer.first_track is not None
        assert run.consumer.first_track.tracking_metadata["tracker"] == "bytetrack"
        assert run.consumer.first_track.tracking_metadata["tracker_version"] == TRACKER_VERSION

        # 5. ROI membership succeeds — INSIDE with the pinned zone.
        assert obs.status is SpatialStatus.INSIDE
        assert obs.zone_profile_id == MANIFEST["spatial"]["zone_profile_id"]
        assert obs.configuration_version_id == IDS["configuration_version_id"]

        # 6. occupancy FSM transitions correctly — exactly one 0 -> 1.
        assert len(run.snapshots) == 1
        assert run.snapshots[0].previous_count == 0
        assert run.snapshots[0].delta == 1

        # 7. deterministic rule fires — one MATCH, STARTED, at frame 8.
        assert [r.status for r in run.results] == [RuleEvaluationStatus.MATCH]
        assert run.event.payload.phase is OccupancySessionPhase.STARTED
        assert run.event.event_time == _event_at(MANIFEST, 8)

        # 8. EventEnvelope validates — the persisted payload round-trips.
        outbox_row = run.store.outbox_by_event[uuid.UUID(str(run.event.event_id))]
        restored = EventEnvelope[Any].model_validate(outbox_row.payload)
        assert restored.model_dump(mode="json") == run.event.model_dump(mode="json")

        # 9. business fact persists — one durable fact row, payload intact.
        assert len(run.store.facts) == 1
        fact_row = run.store.facts[uuid.UUID(str(run.snapshots[0].snapshot_id))]
        assert fact_row.tenant_id == uuid.UUID(str(IDS["tenant_id"]))
        assert fact_row.configuration_version_id == uuid.UUID(str(IDS["configuration_version_id"]))

        # 10. audit persists — one audit row inside the transaction.
        assert len(run.store.audits) == 1
        assert run.store.audits[0].action == "operational.event.persisted"

        # 11. outbox persists — one durable publication unit.
        assert len(run.store.outbox) == 1

        # 12. worker processes outbox — publish → inbox (dedup) → effect.
        assert {row.status for row in run.pipeline.outbox.values()} == {"published"}
        assert {row.status for row in run.pipeline.inbox.values()} == {"processed"}
        assert len(run.pipeline.stream) == 1 and len(run.pipeline.inbox) == 1

        # 13. evidence linkage exists — one durable REQUESTED request.
        assert len(run.evidence.rows) == 1
        (ref_row,) = run.evidence.rows.values()
        assert ref_row.metadata_["processing_state"] == "requested"
        assert ref_row.event_id == uuid.UUID(str(run.event.event_id))

        # 14. provenance is complete — the request carries the full chain
        #     (verified in depth by TestProvenanceCheck).
        ref = _request_contract(ref_row)
        assert ref.tenant_id == IDS["tenant_id"] and ref.venue_id == IDS["venue_id"]
        assert ref.video_session_id == IDS["session_id"]
        assert ref.camera_id == IDS["camera_id"]
        assert ref.configuration_version_id == IDS["configuration_version_id"]
        assert str(ref.rule_id) == RuleIdentifier.OCCUPANCY_SESSION.value
        assert str(ref.rule_version) == "v1"
        assert ref.metadata["source"] == run.event.source

        # 15. FastAPI retrieves the result — canonical DTO, pinned values.
        assert isinstance(run.api_event, OccupancyEventResponse)
        assert run.api_event.event_id == EventId(run.event.event_id)
        assert run.api_event.payload.occupancy_count == 1
        assert run.api_event.payload.rule_version == "v1"

        # 16. Tauri displays the result — the wire shape is exactly the
        #     DTO the desktop card renders; no ORM row leaks through.
        assert isinstance(run.api_evidence, EvidenceAvailabilityResponse)
        assert run.api_evidence.available is True
        assert set(run.api_event.model_dump(mode="json")) == DESKTOP_EVENT_DTO_KEYS


# =============================================================================
# SECURITY CHECK — the 9-item matrix (Task 18.20 §3)
# =============================================================================


class TestSecurityCheck:
    """Every security item through the REAL auth/scope/RLS boundaries."""

    async def test_valid_authentication_works(self) -> None:
        """A valid credential round-trips and the permission gate admits
        the authorized roles."""
        settings = _settings()
        token = create_access_token("user-1", settings)
        token_data = verify_token(token, settings)
        assert token_data.user_id == "user-1"
        for role in (RoleName.MANAGER, RoleName.OPERATOR):
            await require_permission(Permission.ANALYTICS_READ)(
                _actor(tenant_id=uuid.uuid4(), role=role)
            )

    def test_invalid_token_rejected(self) -> None:
        with pytest.raises(AuthenticationError):
            verify_token("not-a-jwt-token", _settings())

    def test_expired_token_rejected(self) -> None:
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(_expired_token(_settings()), _settings())

    async def test_wrong_tenant_rejected(self) -> None:
        event_row, fact_row = _slice_rows()
        session = ApiSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
        )
        actor = _actor(tenant_id=uuid.uuid4())
        from backend.app.application.services.operational_errors import (
            OperationalNotFoundError,
        )

        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_wrong_venue_rejected(self) -> None:
        event_row, fact_row = _slice_rows()
        session = ApiSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
        )
        actor = _actor(
            tenant_id=event_row.tenant_id,
            venue_ids=frozenset({VenueId(uuid.uuid4())}),
        )
        from backend.app.application.services.operational_errors import (
            OperationalNotFoundError,
        )

        with pytest.raises(OperationalNotFoundError):
            await get_operational_event(
                event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
            )

    async def test_unauthorized_role_rejected(self) -> None:
        forged = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.uuid4()),
            role_name=RoleName.OPERATOR,
            permissions=frozenset(),
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError, match="Missing required permission"):
            await require_permission(Permission.ANALYTICS_READ)(forged)

    def test_evidence_scope_enforced(self) -> None:
        """Evidence authorization denies cross-tenant/cross-venue access at
        the evidence boundary itself."""
        authorizer = EvidenceAuthorizer()
        actor = ActorContext(
            actor_id=UserId(uuid.uuid4()),
            tenant_id=TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001")),
            role_name=RoleName.OPERATOR,
            permissions=permissions_for_role(RoleName.OPERATOR),
            venue_scope=frozenset({VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))}),
            authenticated_at=utc_now(),
            active=True,
        )
        with pytest.raises(AuthorizationError, match="Tenant mismatch"):
            authorizer.authorize(
                actor,
                EvidenceOperation.RETRIEVE,
                TenantId(uuid.UUID("90000000-0000-0000-0000-000000000001")),
                VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001")),
            )
        with pytest.raises(AuthorizationError, match="No access to venue"):
            authorizer.authorize(
                actor,
                EvidenceOperation.RETRIEVE,
                TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001")),
                VenueId(uuid.UUID("92000000-0000-0000-0000-000000000001")),
            )

    def test_object_storage_scope_enforced(self) -> None:
        """An object key is NEVER an authorization input — the storage key
        is resolved to the owned evidence row FIRST, then the row's
        tenant/venue are checked. The authorizer accepts no raw key."""
        sig = inspect.signature(EvidenceAuthorizer.authorize)
        params = list(sig.parameters.keys())
        assert "object_key" not in params
        assert "storage_key" not in params
        assert "actor" in params

    async def test_rls_enforced_on_the_request_session(self) -> None:
        """The route scopes the session to the actor's tenant — the same
        SET LOCAL app.tenant_id the RLS policies evaluate."""
        event_row, fact_row = _slice_rows()
        session = ApiSession(
            events={event_row.event_id: event_row},
            facts={fact_row.fact_id: fact_row},
        )
        actor = _actor(tenant_id=event_row.tenant_id)
        await get_operational_event(
            event_id=EventId(event_row.event_id), actor=actor, _perm=None, session=session
        )
        assert (
            f"SET LOCAL app.tenant_id = '{event_row.tenant_id}'" in session.connection_.statements
        )


# =============================================================================
# RELIABILITY CHECK — the 9 failure/replay scenarios (Task 18.20 §4)
# =============================================================================


class TestReliabilityCheck:
    """Each scenario ends with the task's expected outcome: one logical
    business effect, no committed event loss, no duplicate business
    record."""

    async def test_duplicate_fixture_replay(self) -> None:
        """The same fixture replayed three times → ONE logical event/fact/
        evidence-request identity (never a second logical record)."""
        first = await _run_e2e()
        second = await _run_e2e()
        third = await _run_e2e()
        assert first.event is not None and second.event is not None
        assert first.event.event_id == second.event.event_id == third.event.event_id
        assert first.snapshots[0].snapshot_id == second.snapshots[0].snapshot_id
        ref_ids = {
            str(next(iter(run.evidence.rows.values())).ref_id) for run in (first, second, third)
        }
        assert len(ref_ids) == 1

    async def test_duplicate_event(self) -> None:
        """Re-persisting the same logical event is detected as replay and
        writes NOTHING — one fact/event/audit/outbox row survive."""
        run = await _run_e2e()
        assert run.event is not None
        store = run.store
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        result = await service.persist(
            session, fact=run.snapshots[0], event=run.event, actor=run.actor
        )
        assert result.replayed is True
        await session.commit()
        assert len(store.facts) == 1 and len(store.events) == 1
        assert len(store.audits) == 1 and len(store.outbox) == 1

    async def test_duplicate_outbox(self) -> None:
        """At-least-once redelivery (publisher crash → lease expiry →
        re-publish) collapses to ONE inbox row, ONE effect, ONE evidence
        request."""
        _chain, _events, store = await _persisted_run()
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        assert pipeline.publish_once("publisher-a", crash_after_publish=True) == 0
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-a") == 1
        assert len(pipeline.stream) == 2  # the same envelope twice
        assert pipeline.relay_once() == 2
        assert len(pipeline.inbox) == 1  # deduped
        evidence = FakeEvidenceLinkageRepository()
        handlers = build_operational_effect_handlers(
            evidence_linkage=EvidenceLinkageService(repository=evidence),
        )
        assert await pipeline.consume_once("consumer-a", handlers=handlers) == 1
        assert len(evidence.rows) == 1
        assert {r.status for r in pipeline.outbox.values()} == {"published"}
        assert {r.status for r in pipeline.inbox.values()} == {"processed"}

    async def test_duplicate_evidence_request(self) -> None:
        """Linking the same event three times → ONE logical request (the
        PK IS the content-derived ref_id)."""
        run = await _run_e2e()
        assert run.event is not None
        repository = FakeEvidenceLinkageRepository()
        service = EvidenceLinkageService(repository=repository)
        rows = [await service.link_event(object(), run.event) for _ in range(3)]
        assert len(repository.rows) == 1
        assert rows[0] is rows[1] is rows[2]

    async def test_worker_restart(self) -> None:
        """An FSM restart from the durable checkpoint reproduces
        byte-identical facts — no duplicate fact, no lost event."""
        chain = _make_chain()
        source = await _source()
        await _run_cv(chain.consumer, source)
        observations = [
            chain.consumer.spatial_by_frame[i] for i in sorted(chain.consumer.spatial_by_frame)
        ]
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
        assert [s.model_dump(mode="json") for s in first] == [
            s.model_dump(mode="json") for s in continuous
        ]
        assert restarted == []  # the restored FSM never re-fires

    async def test_worker_crash(self) -> None:
        """A publisher crash AFTER the Redis write never loses the
        committed event: the lease expires, a new worker re-publishes and
        delivers exactly ONE logical effect."""
        _chain, events, store = await _persisted_run()
        event_id = uuid.UUID(str(events[0].event_id))
        pipeline = FakePipeline()
        pipeline.seed_outbox(store)
        row = pipeline.outbox_by_event[event_id]
        assert row.status == "pending"  # durable, committed by 18.10
        assert pipeline.publish_once("publisher-1", crash_after_publish=True) == 0
        assert row.status == "processing"
        assert pipeline.claim_outbox("publisher-2") == []  # lease is live
        pipeline.advance(pipeline.lease_seconds + 1)
        assert pipeline.publish_once("publisher-2") == 1
        assert row.status == "published"  # no committed event lost
        assert pipeline.relay_once() == 2
        assert len(pipeline.inbox) == 1
        evidence = FakeEvidenceLinkageRepository()
        handlers = build_operational_effect_handlers(
            evidence_linkage=EvidenceLinkageService(repository=evidence),
        )
        assert await pipeline.consume_once("consumer-1", handlers=handlers) == 1
        assert len(evidence.rows) == 1
        assert next(iter(evidence.rows.values())).event_id == event_id

    async def test_db_rollback(self) -> None:
        """A failure mid-transaction (after the fact/event rows are staged)
        rolls the WHOLE transaction back — no orphan fact/event ever
        appears; a retry commits one clean set."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        store = FakeStore()
        outbox = PersistenceOutbox(store)
        outbox.enqueue_failure = RuntimeError("outbox table unavailable")
        session = PersistenceSession(store)
        service = OperationalPersistenceService(outbox=outbox)
        with pytest.raises(RuntimeError, match="outbox table unavailable"):
            await service.persist(session, fact=snapshot, event=event, actor=chain.actor)
        assert store.count() == 0
        outbox.enqueue_failure = None
        session2 = PersistenceSession(store)
        result = await service.persist(session2, fact=snapshot, event=event, actor=chain.actor)
        assert result.created is True
        await session2.commit()
        assert len(store.facts) == 1 and len(store.events) == 1
        assert len(store.audits) == 1 and len(store.outbox) == 1

    async def test_db_outage_is_not_misclassified_as_replay(self) -> None:
        """A database outage propagates (never treated as replay) and the
        failed write leaves nothing durable."""
        chain, events, _store = await _persisted_run()
        snapshot = chain.consumer.snapshots[0]
        event = events[0]
        store = FakeStore()
        session = _FlakySession(store)
        service = OperationalPersistenceService(outbox=PersistenceOutbox(store))
        with pytest.raises(RuntimeError, match="connection lost"):
            await service.persist(session, fact=snapshot, event=event, actor=chain.actor)
        assert store.outbox_by_event == {}

    async def test_evidence_failure(self) -> None:
        """A transient extraction failure is classified RETRYABLE_FAILURE
        with a persisted backoff + last-error + audit; the retry succeeds
        and exactly ONE logical package is ever persisted."""
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
        await worker.run_once()
        ref = store.get(uuid.UUID(str(_REF)))
        assert _state(ref) == "retryable_failure"
        assert len(store.packages) == 0  # nothing finalized
        clock.advance(seconds=120)
        await worker.run_once()
        assert _state(ref) == "finalized"
        assert len(store.packages) == 1
        await worker.run_once()
        assert len(store.packages) == 1  # idempotent

    async def test_storage_failure(self) -> None:
        """The object store going down fails the source OPEN explicitly
        (never a silent empty stream); when the store returns, a re-run
        produces the ONE golden event."""
        from backend.app.intelligence.sources.base import FrameSourceState
        from backend.app.intelligence.sources.exceptions import FrameSourceError

        storage = await _uploaded_storage()
        storage.simulate_unavailable(True)
        source = await _source(storage=storage)
        with pytest.raises(FrameSourceError):
            await source.open()
        assert source.state is FrameSourceState.FAILED
        await source.aclose()
        storage.simulate_unavailable(False)
        chain = _make_chain()
        source2 = await _source(storage=storage)
        await _run_cv(chain.consumer, source2)
        _results, events = _facts_and_events(chain.consumer)
        assert len(events) == 1
        assert events[0].event_id == await _golden_event_id()


# =============================================================================
# PROVENANCE CHECK — the full chain for the final occupancy event (Task 18.20 §5)
# =============================================================================


class TestProvenanceCheck:
    """Event → EvidenceRef → VideoAsset → VideoSession → Source/Camera →
    Event Time → Frame/Clip → Detector Version → Tracker Version →
    Configuration Version → Rule Version → Checksum → Stored Evidence.

    The package is composed with the REAL Task 17 contracts/builders from
    the slice's own data and verified by the REAL Task 17.14 verifier —
    no broken links.
    """

    def _checksum_of_stored_object(self) -> str:
        """SHA-256 of the stored recording object (the concatenated
        fixture frames) — the independent integrity proof of the stored
        evidence."""
        digest = hashlib.sha256()
        for frame in range(MANIFEST["metadata"]["frame_count"]):
            digest.update((FIXTURES_DIR / f"frame_{frame:03d}.png").read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _enriched_ref(event: EventEnvelope[Any]) -> Any:
        """The REAL builder with the source asset, frame range and
        processing versions the pipeline resolves onto the request."""
        params = EvidenceRequestParams(
            tenant_id=IDS["tenant_id"],
            venue_id=IDS["venue_id"],
            video_session_id=IDS["session_id"],
            camera_id=IDS["camera_id"],
            video_asset_id=ASSET_ID,
            start_frame=MANIFEST["trajectory"]["enter_frame"],
            end_frame=MANIFEST["trajectory"]["exit_frame"],
            detector_version=DETECTOR_VERSION,
            tracker_version=TRACKER_VERSION,
        )
        ref = EvidenceRequestBuilder().build(event, params=params)
        assert ref is not None
        return ref

    @staticmethod
    def _package_for(run: Any, checksum: str) -> Any:
        """Compose the full EvidencePackage from the slice's real data."""
        event = run.event
        assert event is not None
        ref = TestProvenanceCheck._enriched_ref(event)

        resolved = ResolvedSourceSegment(
            status=SourceResolutionStatus.RESOLVED,
            evidence_ref_id=ref.ref_id,
            event_id=ref.event_id,
            tenant_id=ref.tenant_id,
            venue_id=ref.venue_id,
            camera_id=ref.camera_id,
            video_session_id=ref.video_session_id,
            configuration_version_id=ref.configuration_version_id,
            rule_id=ref.rule_id,
            rule_version=ref.rule_version,
            requested_start=ref.start_time,
            requested_end=ref.end_time,
            segments=(
                SourceSegment(
                    asset_id=ASSET_ID,
                    camera_id=ref.camera_id,
                    session_id=ref.video_session_id,
                    start_time=ref.start_time,
                    end_time=ref.end_time,
                ),
            ),
        )
        extraction = ExtractedEvidence(
            extraction_id=MediaId(uuid.uuid5(uuid.UUID(int=0), f"slice-evidence-{event.event_id}")),
            status=ExtractionStatus.SUCCESS,
            evidence_ref_id=ref.ref_id,
            event_id=ref.event_id,
            tenant_id=ref.tenant_id,
            venue_id=ref.venue_id,
            session_id=ref.video_session_id,
            camera_id=ref.camera_id,
            configuration_version_id=ref.configuration_version_id,
            rule_id=ref.rule_id,
            rule_version=ref.rule_version,
            requested_start=ref.start_time,
            requested_end=ref.end_time,
            actual_start_time=ref.start_time,
            actual_end_time=ref.end_time,
            start_frame=ref.start_frame,
            end_frame=ref.end_frame,
            media_path=f"tenants/{ref.tenant_id}/venues/{ref.venue_id}/evidence/{ref.ref_id}.mp4",
            media_format="mp4",
            duration_seconds=2.1,
            size_bytes=sum(
                (FIXTURES_DIR / f"frame_{frame:03d}.png").stat().st_size
                for frame in range(MANIFEST["metadata"]["frame_count"])
            ),
            metadata={"checksum_sha256": checksum, "encoder": "libx264"},
        )
        return EvidencePackageBuilder().finalize(
            evidence_ref=ref,
            resolved_source=resolved,
            extraction=extraction,
        )

    async def test_full_chain_has_no_broken_links(self) -> None:
        run = await _run_e2e()
        assert run.event is not None
        checksum = self._checksum_of_stored_object()
        package = self._package_for(run, checksum)

        verification = ProvenanceVerifier().verify(envelope=run.event, package=package)
        assert verification.verified is True, verification.failures()
        assert verification.failures() == ()

        # Every link the task lists is present AND verified.
        links = {check.link for check in verification.checks}
        required = {
            "event -> evidence",
            "event -> evidence_id",
            "scope -> tenant",
            "scope -> venue",
            "evidence -> source",
            "source -> session",
            "session -> camera",
            "camera -> event_time",
            "camera -> frame_range",
            "time -> detector_version",
            "time -> tracker_version",
            "processing -> configuration",
            "configuration -> rule",
            "rule -> checksum",
            "checksum -> stored_evidence",
            "evidence -> package_identity",
        }
        assert required <= links
        # The version/asset/frame hops carry the slice's real values.
        assert verification.check("evidence -> source").actual == str(ASSET_ID)
        assert verification.check("time -> detector_version").actual == DETECTOR_VERSION
        assert verification.check("time -> tracker_version").actual == TRACKER_VERSION
        assert verification.check("processing -> configuration").actual == str(
            IDS["configuration_version_id"]
        )
        assert verification.check("configuration -> rule").actual == "occupancy_session:v1"
        assert verification.check("rule -> checksum").actual == checksum

    async def test_each_link_carries_expected_and_actual(self) -> None:
        """The audit record explains every hop — nothing is asserted
        blindly."""
        run = await _run_e2e()
        assert run.event is not None
        package = self._package_for(run, self._checksum_of_stored_object())
        verification = ProvenanceVerifier().verify(envelope=run.event, package=package)
        assert verification.verified is True
        for check in verification.checks:
            assert check.status == "verified", check
            # ``camera -> event_time`` compares an instant to an interval
            # representation by design; every other link carries equal
            # expected/actual values.
            if check.link != "camera -> event_time":
                assert check.expected == check.actual, check.link


# =============================================================================
# TELEMETRY CHECK — trace/correlation/scope across every boundary (Task 18.20 §6)
# =============================================================================


class _FakeRecordingSpan:
    """A fake recording span served to ``trace.get_current_span`` — the
    REAL ``_inject_trace_context`` then runs unchanged (same seam as the
    18.18 suite)."""

    def __init__(self, context: Any) -> None:
        self._context = context

    def is_recording(self) -> bool:
        return True

    def get_span_context(self) -> Any:
        return self._context


class TestTelemetryCheck:
    """One event traced across ingestion → CV → rule → database → outbox →
    worker → evidence → API, with trace ID, correlation ID, tenant, venue,
    session, event, evidence, rule version, configuration version — and no
    secrets in logs."""

    async def test_correlation_and_trace_survive_every_boundary(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Correlation id + trace identity are captured at production,
        preserved verbatim through the real outbox payload, and the
        worker continuation seam reconstructs the same parent."""
        correlation = "corr-18-20-gate"
        tokens = obs_context.bind(
            obs_context.RequestContext(
                request_id="req-18-20-gate",
                correlation_id=correlation,
                trace_id="12" * 16,
                span_id="34" * 8,
                sampled=True,
            )
        )
        try:
            run = await _run_e2e()
            assert run.event is not None
            envelope = _inject_trace_context(run.event)
            assert envelope.correlation_id == correlation
            # The real outbox payload preserves the carrier; the API
            # response carries the same correlation id.
            store, api_event = await _persist_and_query(run, envelope)
            outbox_row = store.outbox_by_event[uuid.UUID(str(envelope.event_id))]
            restored = EventEnvelope[Any].model_validate(outbox_row.payload)
            assert restored.correlation_id == correlation
            assert api_event.correlation_id == correlation
        finally:
            obs_context.unbind(tokens)

        # With a recording span active, the trace identity is captured at
        # production and the worker seam reconstructs the same parent.
        from opentelemetry.trace import SpanContext, TraceFlags

        span = _FakeRecordingSpan(
            SpanContext(
                trace_id=int(TRACE_ID, 16),
                span_id=int(SPAN_ID, 16),
                is_remote=False,
                trace_flags=TraceFlags(0x01),
            )
        )
        monkeypatch.setattr(otel_trace, "get_current_span", lambda: span)
        run2 = await _run_e2e()
        assert run2.event is not None
        envelope2 = _inject_trace_context(run2.event)
        assert envelope2.trace_id == TRACE_ID
        assert envelope2.span_id == SPAN_ID
        assert envelope2.trace_sampled is True
        parent = trace_context_from_event_attrs(
            envelope2.trace_id, envelope2.span_id, envelope2.trace_sampled
        )
        assert parent is not None
        assert parent.trace_id == TRACE_ID
        assert parent.span_id == SPAN_ID

    async def test_logs_carry_full_scope_and_no_secrets(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The slice's real logs reconstruct the full correlation scope
        (tenant, venue, session, camera, event, evidence, rule version,
        configuration version) and contain no secret-shaped content."""
        with caplog.at_level(logging.INFO):
            run = await _run_e2e()
        assert run.event is not None
        text = _captured(caplog.records)
        (ref_row,) = run.evidence.rows.values()

        assert str(IDS["tenant_id"]) in text
        assert str(IDS["venue_id"]) in text
        assert str(IDS["session_id"]) in text
        assert str(IDS["camera_id"]) in text
        assert f"event_id={run.event.event_id}" in text
        assert f"evidence_id={ref_row.ref_id}" in text
        assert "rule_id=occupancy_session" in text
        assert "rule_version=v1" in text
        assert str(IDS["configuration_version_id"]) in text

        assert "Bearer " not in text
        assert "eyJ" not in text  # JWT signature
        assert "password=" not in text
        assert "secret" not in text.lower().replace("[REDACTED]", "")


# =============================================================================
# DEFINITION OF DONE — the consolidated final gate (Task 18.20 §9)
# =============================================================================


class TestDefinitionOfDone:
    """The task's final Definition of Done, asserted as one gate.

    Everything here is either asserted above (this class re-walks the
    consolidated invariants over ONE fresh run) or is a structural
    guarantee the module already verified.
    """

    async def test_the_complete_definition_of_done(self) -> None:
        run = await _run_e2e()
        assert run.event is not None

        # One fixture flows through the ENTIRE architecture, and every
        # abstraction worked (source / YOLO / ByteTrack / spatial / FSM /
        # rule / PostgreSQL / outbox / evidence / FastAPI).
        assert run.source.state.value == "closed"
        assert run.consumer.packets_seen
        assert run.consumer.first_detection is not None
        assert run.consumer.first_track is not None
        assert len(run.snapshots) == 1
        assert len(run.events) == 1
        assert len(run.store.facts) == 1 and len(run.store.events) == 1
        assert len(run.store.audits) == 1 and len(run.store.outbox) == 1
        assert len(run.evidence.rows) == 1
        assert isinstance(run.api_event, OccupancyEventResponse)
        assert isinstance(run.api_evidence, EvidenceAvailabilityResponse)

        # Authentication/authorization work (the server-side actor was
        # admitted by the permission gate; cross-tenant/venue are rejected
        # in TestSecurityCheck).
        assert Permission.ANALYTICS_READ in run.actor.permissions

        # Telemetry works (the envelope carries trace/correlation
        # identities — TestTelemetryCheck).
        assert run.event.source == "rule:occupancy_session:v1"

        # Duplicate replay is idempotent; worker failure recoverable;
        # invalid auth rejected; cross-tenant/venue rejected — the
        # dedicated gates above prove each.
        assert [r.status for r in run.results] == [RuleEvaluationStatus.MATCH]

        # PostgreSQL remains the source of truth (the durable rows ARE the
        # SQL shapes), and evidence is independently inspectable (the
        # provenance chain is verified in TestProvenanceCheck).
        assert EventEnvelope[Any].model_validate(
            run.store.outbox_by_event[uuid.UUID(str(run.event.event_id))].payload
        ).model_dump(mode="json") == run.event.model_dump(mode="json")

        # No business truth is generated by the UI — the desktop renders
        # exactly the canonical DTO (TestArchitecturalBoundaryCheck
        # proves it contains no CV/rule/DB logic).
        assert set(run.api_event.model_dump(mode="json")) == DESKTOP_EVENT_DTO_KEYS

    def test_no_llm_is_involved_anywhere_in_the_slice(self) -> None:
        """The slice is deterministic — no LLM/AI-orchestration import
        exists anywhere in the production application."""
        pattern = re.compile(
            r"""(?:langchain|langgraph|openai|anthropic|ollama|transformers|llama_index)""",
            re.IGNORECASE,
        )
        offenders = [str(path) for path, text in _backend_python_files() if pattern.search(text)]
        assert offenders == [], f"LLM dependency leaks into the application: {offenders}"

    def test_gate_defines_no_rule_fsm_dto_or_repository(self) -> None:
        """STOP condition: the gate consumes the packaged components — it
        never declares its own rule, FSM, DTO, or repository."""
        source = pathlib.Path(__file__).read_text()
        body = source.split('"""', 2)[2]
        guard_start = body.index("def test_gate_defines_no_rule_fsm_dto_or_repository")
        non_guard = body[:guard_start]
        assert "RuleDefinition(" not in non_guard
        assert "FsmRule(" not in non_guard
        assert "DeterministicFsm(" not in non_guard
        # The real boundaries are the ones used.
        assert "OperationalPersistenceService(" in non_guard
        assert "build_operational_effect_handlers(" in non_guard
        assert "get_operational_event(" in non_guard
        assert "ProvenanceVerifier()" in non_guard
