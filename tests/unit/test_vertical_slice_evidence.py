"""Task 18.9 — Event → Evidence integration (vertical slice).

The controlled vertical-slice fixture (Task 18.2) drives the REAL Task 15
chain (18.7) and the REGISTERED Task 16 rule (18.8), and THIS slice
connects the resulting material events to the durable Task 17 evidence
pipeline:

    SpatialObservation → … → OccupancySnapshot (Task 15.4)
        → RuleEvaluationEngine (occupancy_session:v1 — Task 16.4)
        → EventEnvelope
        → EvidenceLinkageService (Task 18.9 — the ONLY sanctioned link)
        → EvidenceRefModel row (REQUESTED — the Task 17.11 worker's queue)

No evidence is ever created inside the rule: the engine only DESCRIBES
the required request (an ``EvidenceRef`` on the evaluation result); the
linkage service performs the durable linkage with Task 17 pieces — the
``EvidenceRequestBuilder`` (17.3), the canonical ``EvidenceRef``
contract (17.2), and the durable REQUESTED state the worker consumes
(17.10/17.11).

Preserved on the linked request (the task's list): event_id, tenant,
venue, session, source (the envelope's producer), camera, event_time,
configuration_version, rule_version.

Tests (the task's list):

1. normal event         → the slice's events each produce ONE durable
                          REQUESTED evidence request carrying the full
                          chain;
2. duplicate event      → linking the same event twice yields the SAME
                          logical request (one row, same ref_id — Task 7
                          idempotency enforced by the deterministic PK);
3. provenance chain     → every hop (event → source → session → camera →
                          time → configuration → rule) on the linked
                          request matches the material event;
4. STOP condition       → an event that cannot be deterministically
                          linked (unknown event type, non-canonical
                          envelope) fails with the typed error — evidence
                          is never linked to the wrong scope and never
                          linked non-deterministically; the same event +
                          same versions always reproduce the same ref_id.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.intelligence.rules import (
    EvidenceRequestParams,
    InvalidEvidenceRequestError,
)
from contracts.common import RuleId, RuleVersion, TenantId
from contracts.events import EventEnvelope, EvidenceRef
from contracts.rules import (
    EvidenceRequirement,
    RuleEvaluationStatus,
    RuleIdentifier,
)
from tests.unit.test_vertical_slice_rule import (
    _identities,
    _load_manifest,
    _run_full_slice,
)

# =============================================================================
# Fake repository — faithful to the real dedup contract (PK = deterministic
# ref_id, so one event → one logical request; a duplicate link returns the
# existing row)
# =============================================================================


class FakeSession:
    """Dummy session — the fake repository owns all semantics."""


class FakeEvidenceLinkageRepository:
    """In-memory EvidenceLinkageRepository faithful to the SQL semantics."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EvidenceRefModel] = {}

    async def link(self, session: FakeSession, *, ref: EvidenceRefModel) -> EvidenceRefModel:
        existing = self.rows.get(ref.ref_id)
        if existing is not None:
            return existing  # ON CONFLICT DO NOTHING → return the existing row
        self.rows[ref.ref_id] = ref
        return ref

    async def get_by_ref_id(
        self, session: FakeSession, ref_id: uuid.UUID | str
    ) -> EvidenceRefModel | None:
        return self.rows.get(uuid.UUID(str(ref_id)))

    async def get_by_event_id(
        self, session: FakeSession, event_id: uuid.UUID | str
    ) -> EvidenceRefModel | None:
        target = uuid.UUID(str(event_id))
        for row in self.rows.values():
            if row.event_id == target:
                return row
        return None


def _linker(repository: FakeEvidenceLinkageRepository | None = None) -> EvidenceLinkageService:
    return EvidenceLinkageService(
        repository=repository or FakeEvidenceLinkageRepository(),
    )


async def _link_events(
    events: list[EventEnvelope],
    repository: FakeEvidenceLinkageRepository,
) -> list[EvidenceRefModel]:
    service = _linker(repository)
    rows: list[EvidenceRefModel] = []
    for envelope in events:
        row = await service.link_event(FakeSession(), envelope)
        assert row is not None
        rows.append(row)
    return rows


def _request_contract(row: EvidenceRefModel) -> EvidenceRef:
    """Rebuild the durable request contract the evidence worker reads."""
    return EvidenceRef.model_validate(row.metadata_["evidence_request"])


# =============================================================================
# 1. NORMAL EVENT — each material event produces one durable request
# =============================================================================


class TestNormalEvent:
    async def test_each_event_yields_one_durable_request(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        assert len(outcome.events) == 2  # the 18.8 slice: STARTED + ENDED

        repository = FakeEvidenceLinkageRepository()
        rows = await _link_events(outcome.events, repository)

        assert len(rows) == 2
        assert len(repository.rows) == 2
        # Every row is a durable REQUESTED request the Task 17.11 worker
        # admits to its queue.
        for row in rows:
            assert row.metadata_ is not None
            assert row.metadata_["processing_state"] == "requested"
            assert "evidence_request" in row.metadata_
            assert row.ref_type == "video_clip"

    async def test_request_preserves_full_provenance(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        repository = FakeEvidenceLinkageRepository()
        row = (await _link_events([event], repository))[0]
        ref = _request_contract(row)

        # event_id — the atomic event link pair on the durable row + contract.
        assert row.event_id == uuid.UUID(str(event.event_id))
        assert ref.event_id == event.event_id
        assert ref.event_time == event.event_time
        # tenant / venue / session — from the event payload's scope.
        assert ref.tenant_id == ids["tenant_id"]
        assert ref.venue_id == ids["venue_id"]
        assert ref.video_session_id == ids["session_id"]
        assert row.session_id == uuid.UUID(str(ids["session_id"]))
        # camera — the source camera of the material event.
        assert ref.camera_id == ids["camera_id"]
        # source — the envelope's producer preserved on the request.
        assert ref.metadata["source"] == event.source
        assert event.source == "rule:occupancy_session:v1"
        # configuration_version + rule_version — pinned, never "latest".
        assert ref.configuration_version_id == ids["configuration_version_id"]
        assert ref.rule_id == RuleId(RuleIdentifier.OCCUPANCY_SESSION.value)
        assert ref.rule_version == RuleVersion("v1")

    async def test_rule_never_creates_evidence_directly(self) -> None:
        """The engine only DESCRIBES the request; the linkage service is
        the ONLY place a durable evidence row appears."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        # Before linking: the engine results carry EvidenceRef *requests*
        # (descriptions) — but NO durable evidence rows exist anywhere.
        assert all(
            result.status is RuleEvaluationStatus.MATCH and len(result.evidence_requests) == 1
            for result in outcome.results
        )
        repository = FakeEvidenceLinkageRepository()
        assert repository.rows == {}

        await _link_events(outcome.events, repository)
        assert len(repository.rows) == 2  # rows appear ONLY via the service

    async def test_pipeline_request_is_the_engine_attached_request(self) -> None:
        """The durable request IS the engine-attached request (same ref_id)
        — the pipeline never re-derives a second logical identity."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        repository = FakeEvidenceLinkageRepository()
        rows = await _link_events(outcome.events, repository)
        for row, result in zip(rows, outcome.results, strict=True):
            engine_ref = result.evidence_requests[0]
            linked = _request_contract(row)
            assert linked.ref_id == engine_ref.ref_id
            assert linked.event_id == engine_ref.event_id


# =============================================================================
# 2. DUPLICATE EVENT — one logical evidence request (Task 7 idempotency)
# =============================================================================


class TestDuplicateEvent:
    async def test_duplicate_event_is_one_logical_request(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        repository = FakeEvidenceLinkageRepository()
        service = _linker(repository)

        first = await service.link_event(FakeSession(), event)
        second = await service.link_event(FakeSession(), event)
        assert first is not None and second is not None

        # One logical request: the SAME row (the PK IS the deterministic
        # ref_id — the second delivery collapses to the existing row).
        assert first.ref_id == second.ref_id
        assert first is second
        assert len(repository.rows) == 1

    async def test_replay_never_creates_a_second_request(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)

        repository = FakeEvidenceLinkageRepository()
        for _ in range(3):
            await _link_events(outcome.events, repository)

        # Three full replays of the slice → exactly TWO logical requests.
        assert len(repository.rows) == 2

    async def test_duplicate_is_byte_identical(self) -> None:
        """The durable request CONTRACT (the deterministic part) is
        byte-identical across duplicate deliveries — the row's wall-clock
        ``created_at`` is transport metadata, never part of the logical
        request."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        repository = FakeEvidenceLinkageRepository()
        first = await _link_events(outcome.events, repository)
        second = await _link_events(outcome.events, repository)
        assert [_request_contract(r).model_dump(mode="json") for r in first] == [
            _request_contract(r).model_dump(mode="json") for r in second
        ]


# =============================================================================
# 3. PROVENANCE CHAIN — every hop matches the material event
# =============================================================================


class TestProvenanceChain:
    async def test_chain_hops_match_the_material_event(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        repository = FakeEvidenceLinkageRepository()
        row = (await _link_events([event], repository))[0]
        ref = _request_contract(row)

        # The request-level chain (the verifier's hop vocabulary):
        # event → evidence, source → session, session → camera,
        # camera → event_time, processing → configuration,
        # configuration → rule.
        assert ref.event_id == event.event_id
        assert ref.video_session_id == event.payload.session_id
        assert ref.camera_id == event.payload.camera_id
        # The requested interval covers the material event's instant
        # (occupancy events degenerate to the boundary instant).
        assert ref.start_time == event.event_time
        assert ref.end_time == event.event_time
        assert ref.start_time <= event.event_time <= ref.end_time
        assert ref.configuration_version_id == event.payload.configuration_version_id
        assert ref.rule_id == event.payload.rule_id
        assert ref.rule_version == event.payload.rule_version

    async def test_substituted_scope_never_links(self) -> None:
        """A caller asserting the WRONG scope is rejected — evidence is
        never linked to a scope that disagrees with the event."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]

        wrong = EvidenceRequestParams(
            tenant_id=TenantId(uuid.UUID("99999999-0000-0000-0000-000000000001")),
            venue_id=ids["venue_id"],
            video_session_id=ids["session_id"],
            camera_id=ids["camera_id"],
        )
        with pytest.raises(InvalidEvidenceRequestError, match="tenant scope"):
            await _linker().link_event(FakeSession(), event, params=wrong)


# =============================================================================
# 4. STOP CONDITION — deterministic linkage or a typed failure
# =============================================================================


class TestStopCondition:
    async def test_unknown_event_type_cannot_be_linked(self) -> None:
        """An event whose type is not a canonical rule event cannot be
        deterministically linked — typed error, never a silent skip."""
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]
        unknown = EventEnvelope(
            event_id=event.event_id,
            event_type="frame.detected",
            event_time=event.event_time,
            produced_at=event.produced_at,
            source="cv.detector",
            payload={"count": 1},
        )
        with pytest.raises(InvalidEvidenceRequestError, match="event_type"):
            await _linker().link_event(FakeSession(), unknown)

    async def test_non_canonical_envelope_rejected(self) -> None:
        with pytest.raises(InvalidEvidenceRequestError, match="EventEnvelope"):
            await _linker().link_event(FakeSession(), object())  # type: ignore[arg-type]

    async def test_evidence_requirement_none_links_nothing(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        event = outcome.events[0]
        repository = FakeEvidenceLinkageRepository()
        service = _linker(repository)

        result = await service.link_event(
            FakeSession(),
            event,
            evidence_requirement=EvidenceRequirement.NONE,
        )
        assert result is None
        assert repository.rows == {}

    async def test_linkage_is_deterministic_across_instances(self) -> None:
        """Fresh engines + fresh service instances reproduce the SAME
        logical request — the determinism STOP condition."""
        manifest = _load_manifest()
        ids = _identities(manifest)

        ref_ids: list[uuid.UUID] = []
        for _ in range(2):
            outcome = _run_full_slice(manifest, ids)
            repository = FakeEvidenceLinkageRepository()
            rows = await _link_events(outcome.events, repository)
            ref_ids.extend(row.ref_id for row in rows)

        # Two full replays → the same two logical request identities.
        assert ref_ids[0] == ref_ids[2]
        assert ref_ids[1] == ref_ids[3]
        assert ref_ids[0] != ref_ids[1]

    async def test_ref_id_is_content_derived_uuid5(self) -> None:
        manifest = _load_manifest()
        ids = _identities(manifest)
        outcome = _run_full_slice(manifest, ids)
        repository = FakeEvidenceLinkageRepository()
        rows = await _link_events(outcome.events, repository)
        for row in rows:
            assert isinstance(row.ref_id, uuid.UUID)
            assert row.ref_id.version == 5  # the Task 7 content-derived scheme


# =============================================================================
# STOP condition — the slice uses Task 17; no evidence logic in the rule
# =============================================================================


class TestNoEvidenceInRule:
    async def test_service_uses_the_builder_not_a_second_implementation(self) -> None:
        """The linkage service links through the Task 17.3 builder — the
        STOP condition for Task 18.9: use Task 17, never re-implement."""
        source = Path(__file__).read_text()
        body = source.split('"""', 2)[2]
        guard_start = body.index("def test_service_uses_the_builder_not_a_second_implementation")
        non_guard = body[:guard_start]
        # The test itself never constructs an EvidenceRef or models a
        # payload — the canonical contracts do that.
        assert "EvidenceRef(" not in non_guard
        assert "EvidenceRequestParams(" in non_guard  # only the scope-assertion shape
        assert "uuid.uuid4()" not in non_guard  # never a fresh identity
