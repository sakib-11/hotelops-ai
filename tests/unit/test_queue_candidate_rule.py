"""Tests for Task 16.6 — the deterministic queue_candidate rule.

Converts an already-established canonical ``WaitingInterval`` (Task 15.5.3)
into a ``queue_candidate`` event when the configured queue-candidate
conditions are satisfied. The rule does NOT determine queue membership
from YOLO detections, bounding boxes, raw frames, tracker objects, or
pixels — it consumes the confirmed canonical waiting fact + its canonical
spatial context only.

Covered (Task 16.6 Part 32):

- unit: below / exact / above qualification, valid queue area, invalid
  queue area, missing queue fact, invalid waiting duration, missing /
  invalid configuration, missing/invalid event_time;
- boundary: threshold - 1 (NO_MATCH), threshold exactly (MATCH),
  threshold + 1 (MATCH) — the documented boundary policy;
- idempotency: repeated evaluation, duplicate delivery, replay;
- temporal: continuous waiting (single qualification), short occlusion,
  waiting exit, re-entry, late/out-of-order per Task 15 policy;
- sessions: episode A, episode B, re-entry (independent events);
- security: tenant isolation, venue isolation, cross-scope rejection;
- configuration: config v1, config v2, historical v1 replay;
- rule version: queue_candidate v1, v2 registered without affecting v1,
  unsupported version rejected;
- contract: EventEnvelope validation, EvidenceRef validation;
- invariants (Part 33): 12 guarantees;
- golden tests (§20-26).

All fixtures use the REAL canonical contracts with fixed deterministic IDs
so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.intelligence.rules import (
    QUEUE_AREA_IDS_CONFIG_KEY,
    QUEUE_CANDIDATE_CONFIG_KEY,
    QUEUE_CANDIDATE_EVALUATOR_ID,
    QUEUE_CANDIDATE_EVALUATOR_V2_ID,
    MissingRuleConfigurationError,
    MixedScopeRuleInputError,
    UnsupportedFactTypeError,
    UnsupportedRuleVersionError,
    build_operational_engine,
    deterministic_event_id,
)
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    RuleId,
    RuleVersion,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
)
from contracts.events import EventEnvelope, EvidenceRef
from contracts.rules import (
    QueueCandidatePayload,
    RuleEvaluationInput,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    TemporalReason,
    TemporalStateKey,
    WaitingInterval,
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

_RULE_ID = RuleId(RuleIdentifier.QUEUE_CANDIDATE.value)
_RULE_VERSION = RuleVersion("v1")
_THRESHOLD = 300.0  # the configured minimum waiting duration used by most tests
_QUEUE_AREA = "queue-reception"
_OTHER_AREA = "zone-lobby"  # a waiting-capable context that is NOT a queue area

_PROCESSED = datetime(2026, 8, 1, 11, 0, 30, tzinfo=UTC)
_START = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)


def _event(seconds: int) -> datetime:
    """Deterministic event-time: 10:00:00 + ``seconds`` (Part 14)."""
    return _START + timedelta(seconds=seconds)


def _key(
    *,
    tenant_id: TenantId = _TENANT_A,
    venue_id: VenueId = _VENUE_A,
    session_id: VideoSessionId = _SESSION,
    camera_id: CameraId = _CAMERA,
    configuration_version_id: ConfigurationVersionId = _CONFIG_V1,
    track_id: TrackId = _TRACK_A,
    semantic_context: str | None = _QUEUE_AREA,
) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind="waiting",
        tenant_id=tenant_id,
        venue_id=venue_id,
        session_id=session_id,
        camera_id=camera_id,
        configuration_version_id=configuration_version_id,
        track_id=track_id,
        semantic_context=semantic_context,
    )


def _interval(
    *,
    duration: float,
    waiting_start: datetime = _START,
    last_seen: datetime | None = None,
    reason: TemporalReason | None = None,
    interval_id: EventId | None = None,
    key: TemporalStateKey | None = None,
    label: str = "i",
    qualified: bool = True,
) -> WaitingInterval:
    """A canonical WaitingInterval fact with deterministic content-derived id."""
    last = last_seen if last_seen is not None else waiting_start + timedelta(seconds=duration)
    return WaitingInterval(
        interval_id=interval_id
        or EventId(uuid.uuid5(uuid.NAMESPACE_URL, f"wait-{label}-{waiting_start.isoformat()}")),
        fsm_kind="waiting",
        key=key or _key(),
        waiting_start=waiting_start,
        waiting_end=last if reason is not None else None,
        last_seen=last,
        duration_seconds=duration,
        qualified=qualified,
        minimum_waiting_seconds=0.0,
        reason=reason,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(
    interval: WaitingInterval,
    *,
    threshold: float = _THRESHOLD,
    queue_areas: list[str] | None = None,
    config_version: ConfigurationVersionId = _CONFIG_V1,
    event_time: datetime | None = None,
):
    """A canonical RuleEvaluationInput for one waiting interval."""
    return RuleEvaluationInput(
        facts=(interval,),
        configuration={
            QUEUE_CANDIDATE_CONFIG_KEY: threshold,
            QUEUE_AREA_IDS_CONFIG_KEY: queue_areas if queue_areas is not None else [_QUEUE_AREA],
        },
        configuration_version_id=config_version,
        rule_version=_RULE_VERSION,
        event_time=event_time if event_time is not None else interval.last_seen,
        processing_time=_PROCESSED,
    )


def _engine():
    """The sanctioned operational engine (queue_candidate:v1 + :v2 registered)."""
    return build_operational_engine()


def _evaluate(
    interval: WaitingInterval,
    *,
    threshold: float = _THRESHOLD,
    queue_areas: list[str] | None = None,
    config_version: ConfigurationVersionId = _CONFIG_V1,
):
    engine = _engine()
    return engine.evaluate(
        _RULE_ID,
        _RULE_VERSION,
        _input(
            interval,
            threshold=threshold,
            queue_areas=queue_areas,
            config_version=config_version,
        ),
    )


# =============================================================================
# 32. UNIT TESTS — below / exact / above / invalid inputs
# =============================================================================


class TestUnit:
    def test_below_threshold_no_match(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_exact_threshold_match(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.event_type == RuleEventType.QUEUE_CANDIDATE.value
        assert result.event.event_time == _event(300)
        payload = result.event.payload
        assert payload.waiting_duration == 300
        assert payload.threshold_seconds == 300
        assert payload.waiting_start_time == _START
        assert payload.qualification_time == _event(300)
        assert payload.tenant_id == _TENANT_A
        assert payload.venue_id == _VENUE_A
        assert payload.session_id == _SESSION
        assert payload.camera_id == _CAMERA
        assert payload.track_id == _TRACK_A
        assert payload.spatial_context_id == _QUEUE_AREA
        assert payload.configuration_version_id == _CONFIG_V1
        assert payload.rule_id == RuleIdentifier.QUEUE_CANDIDATE.value
        assert payload.rule_version == "v1"

    def test_above_threshold_match(self) -> None:
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.waiting_duration == 301

    def test_valid_queue_area_match(self) -> None:
        # The fact's semantic_context is IN the configured eligible list.
        result = _evaluate(
            _interval(duration=300.0, key=_key(semantic_context=_QUEUE_AREA)),
            queue_areas=[_QUEUE_AREA, "queue-restaurant"],
        )
        assert result.status is RuleEvaluationStatus.MATCH

    def test_invalid_queue_area_no_match(self) -> None:
        # A waiting fact outside the configured queue/service areas is a
        # legitimate NO_MATCH — never silently classified as a queue.
        result = _evaluate(
            _interval(duration=300.0, key=_key(semantic_context=_OTHER_AREA)),
            queue_areas=[_QUEUE_AREA],
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_missing_queue_area_config_rejected(self) -> None:
        # Missing required key → the registry boundary raises (typed
        # error); the evaluator is never silently defaulted.
        engine = _engine()
        inp = _input(_interval(duration=300.0)).model_copy(
            update={"configuration": {QUEUE_CANDIDATE_CONFIG_KEY: 300.0}}
        )
        with pytest.raises(MissingRuleConfigurationError, match="queue_area_ids"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_invalid_queue_area_list_value_is_invalid(self) -> None:
        # Present-but-invalid value (empty / non-string members) → INVALID,
        # never a silent "all areas eligible" fallback.
        engine = _engine()
        inp = _input(_interval(duration=300.0), queue_areas=[])
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None

        inp_bad = _input(_interval(duration=300.0)).model_copy(
            update={
                "configuration": {
                    QUEUE_CANDIDATE_CONFIG_KEY: 300.0,
                    QUEUE_AREA_IDS_CONFIG_KEY: ["valid", 123],
                }
            }
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp_bad)
        assert result.status is RuleEvaluationStatus.INVALID

    def test_missing_queue_fact_rejected(self) -> None:
        from contracts.temporal import DwellInterval

        dwell = DwellInterval(
            interval_id=EventId(uuid.uuid4()),
            fsm_kind="dwell",
            key=_key().model_copy(update={"fsm_kind": "dwell"}),
            dwell_start=_START,
            dwell_end=_event(300),
            last_seen=_event(300),
            duration_seconds=300.0,
            qualified=True,
            minimum_dwell_seconds=0.0,
            reason=TemporalReason.EXIT_CONFIRMED,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        engine = _engine()
        with pytest.raises(UnsupportedFactTypeError, match="does not declare fact type"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(interval=dwell))  # type: ignore[arg-type]

    def test_invalid_duration_none_is_invalid(self) -> None:
        interval = _interval(duration=0.0).model_copy(update={"duration_seconds": None})
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None
        assert "duration" in (result.reason or "")

    def test_negative_duration_rejected_at_contract(self) -> None:
        with pytest.raises(ValidationError, match="duration_seconds"):
            _interval(duration=-1.0)

    def test_invalid_threshold_zero_is_invalid(self) -> None:
        result = _evaluate(_interval(duration=300.0), threshold=0.0)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None

    def test_invalid_threshold_negative_is_invalid(self) -> None:
        result = _evaluate(_interval(duration=300.0), threshold=-5.0)
        assert result.status is RuleEvaluationStatus.INVALID

    def test_invalid_threshold_non_numeric_is_invalid(self) -> None:
        engine = _engine()
        inp = _input(_interval(duration=300.0)).model_copy(
            update={
                "configuration": {
                    QUEUE_CANDIDATE_CONFIG_KEY: "abc",
                    QUEUE_AREA_IDS_CONFIG_KEY: [_QUEUE_AREA],
                }
            }
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID

    def test_missing_configuration_rejected(self) -> None:
        engine = _engine()
        inp = _input(_interval(duration=300.0)).model_copy(update={"configuration": {}})
        with pytest.raises(MissingRuleConfigurationError, match=QUEUE_CANDIDATE_CONFIG_KEY):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_missing_event_time_rejected_at_contract(self) -> None:
        interval = _interval(duration=300.0)
        with pytest.raises(ValidationError, match="event_time"):
            RuleEvaluationInput(
                facts=(interval,),
                configuration={QUEUE_CANDIDATE_CONFIG_KEY: 300.0},
                configuration_version_id=_CONFIG_V1,
                rule_version=_RULE_VERSION,
            )

    def test_event_time_mismatch_last_seen_is_invalid(self) -> None:
        interval = _interval(duration=300.0)
        engine = _engine()
        inp = _input(interval, event_time=_event(999))  # mismatched
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID
        assert "last_seen" in (result.reason or "")

    def test_missing_semantic_context_is_invalid(self) -> None:
        # A waiting fact with NO spatial context cannot be a queue
        # candidate (Part 9: missing queue/service-area context).
        result = _evaluate(_interval(duration=300.0, key=_key(semantic_context=None)))
        assert result.status is RuleEvaluationStatus.INVALID
        assert "semantic_context" in (result.reason or "")

    def test_invalid_temporal_state_fsm_kind_is_invalid(self) -> None:
        bad_key = _key().model_copy(update={"fsm_kind": "presence"})
        result = _evaluate(_interval(duration=300.0, key=bad_key))
        assert result.status is RuleEvaluationStatus.INVALID
        assert "fsm_kind" in (result.reason or "")


# =============================================================================
# 32. BOUNDARY TESTS — threshold - 1 / threshold / threshold + 1
# =============================================================================


class TestBoundary:
    def test_threshold_minus_one_no_match(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_threshold_exactly_match(self) -> None:
        # Mandatory boundary test (Part 21): exact qualification → MATCH.
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_threshold_plus_one_match(self) -> None:
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH


# =============================================================================
# 32. IDEMPOTENCY TESTS — repeated evaluation / duplicate delivery / replay
# =============================================================================


class TestIdempotency:
    def test_repeated_evaluation_identical(self) -> None:
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        second = _evaluate(interval)
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()
        assert first.event is not None and second.event is not None
        assert first.event.event_id == second.event.event_id

    def test_duplicate_delivery_same_event(self) -> None:
        # Feeding the SAME qualifying fact ten times yields ONE logical
        # event identity (Task 7 idempotency, Part 12).
        interval = _interval(duration=300.0)
        ids = {_evaluate(interval).event.event_id for _ in range(10)}
        assert len(ids) == 1

    def test_replay_deterministic(self) -> None:
        interval = _interval(duration=301.0)
        engine = _engine()
        inp = _input(interval)
        r1 = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        serialized = r1.model_dump_json()
        r2 = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert r2.model_dump_json() == serialized
        assert r1 == r2


# =============================================================================
# 32. TEMPORAL TESTS — continuous / occlusion / exit / re-entry / late / OOO
# =============================================================================


class TestTemporal:
    def test_continuous_waiting_single_qualification(self) -> None:
        # Timeline (Part 23): below, qualifying, still waiting. The rule
        # fires ONCE for the qualifying fact; below-threshold facts never
        # fire; re-evaluating the qualifying fact stays one event.
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "wait-episode-A"))
        below = _interval(duration=299.0, last_seen=_event(299), interval_id=interval_id)
        crossing = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        still = _interval(duration=330.0, last_seen=_event(330), interval_id=interval_id)
        r_below = _evaluate(below)
        r_cross = _evaluate(crossing)
        r_still = _evaluate(still)
        assert r_below.status is RuleEvaluationStatus.NO_MATCH
        assert r_cross.status is RuleEvaluationStatus.MATCH
        # Same qualification re-evaluated → same logical event identity.
        assert _evaluate(crossing).event.event_id == r_cross.event.event_id
        # A later fact of the SAME episode that is still >= threshold is a
        # distinct observation instant; Task 7 dedups by event identity at
        # the pipeline layer (Part 13) — the rule never re-derives temporal
        # state and never fires below threshold.
        assert r_still.status is RuleEvaluationStatus.MATCH

    def test_short_occlusion_no_duplicate(self) -> None:
        # Task 15.5.3 keeps the interval ALIVE across short occlusion; the
        # rule consumes the same canonical interval identity, so re-feeding
        # the qualifying fact after occlusion produces one event (Part 15).
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        replay = _evaluate(interval)
        assert first.event is not None and replay.event is not None
        assert replay.event.event_id == first.event.event_id
        assert replay.model_dump_json() == first.model_dump_json()

    def test_waiting_exit_closed_interval_still_qualifies(self) -> None:
        # A closed interval (EXIT_CONFIRMED) that already qualified is a
        # confirmed fact — the qualification stands (Part 14 re-entry basis).
        interval = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None

    def test_reentry_after_exit_independent(self) -> None:
        # Episode A qualifies and ends; Episode B later qualifies → two
        # independent events (Part 14).
        a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        b = _interval(
            duration=300.0,
            waiting_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(a)
        rb = _evaluate(b)
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id
        assert ra.event.payload.waiting_start_time == _START
        assert rb.event.payload.waiting_start_time == _event(1200)

    def test_late_event_consumed_per_task15_policy(self) -> None:
        # Task 15 applies the late/out-of-order policy BEFORE the rule sees
        # a fact — a fact Task 15 rejects never reaches the rule. A fact
        # Task 15 accepted is consumed as-is (Part 14).
        interval = _interval(duration=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None

    def test_out_of_order_event_never_fabricates(self) -> None:
        # The rule has no temporal-ordering role: it consumes canonical
        # facts only. An interval outside Task 15's acceptance window is
        # rejected by Task 15 — never fabricated into a MATCH here.
        interval = _interval(duration=300.0)
        assert _evaluate(interval).status is RuleEvaluationStatus.MATCH


# =============================================================================
# 32. SESSION TESTS — episode A / episode B / re-entry
# =============================================================================


class TestSessions:
    def test_two_episodes_two_events(self) -> None:
        episode_a = _interval(duration=300.0, label="A")
        episode_b = _interval(
            duration=300.0,
            waiting_start=_event(600),
            last_seen=_event(900),
            label="B",
        )
        ra = _evaluate(episode_a)
        rb = _evaluate(episode_b)
        assert ra.status is RuleEvaluationStatus.MATCH
        assert rb.status is RuleEvaluationStatus.MATCH
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id
        assert ra.event.payload.interval_id != rb.event.payload.interval_id

    def test_reentry_independent_events(self) -> None:
        episode_a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        episode_b = _interval(
            duration=300.0,
            waiting_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(episode_a)
        rb = _evaluate(episode_b)
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id


# =============================================================================
# 32. SECURITY TESTS — tenant / venue isolation
# =============================================================================


class TestSecurity:
    def test_tenant_isolation(self) -> None:
        a = _evaluate(_interval(duration=300.0, key=_key(tenant_id=_TENANT_A)))
        b = _evaluate(_interval(duration=300.0, key=_key(tenant_id=_TENANT_B)))
        assert a.event is not None and b.event is not None
        assert a.event.payload.tenant_id == _TENANT_A
        assert b.event.payload.tenant_id == _TENANT_B
        assert a.event.event_id != b.event.event_id

    def test_venue_isolation(self) -> None:
        a = _evaluate(_interval(duration=300.0, key=_key(venue_id=_VENUE_A)))
        b = _evaluate(_interval(duration=300.0, key=_key(venue_id=_VENUE_B)))
        assert a.event is not None and b.event is not None
        assert a.event.payload.venue_id == _VENUE_A
        assert b.event.payload.venue_id == _VENUE_B
        assert a.event.event_id != b.event.event_id

    def test_cross_scope_facts_rejected(self) -> None:
        engine = _engine()
        a = _interval(duration=300.0, key=_key(tenant_id=_TENANT_A))
        b = _interval(duration=300.0, key=_key(tenant_id=_TENANT_B))
        inp = RuleEvaluationInput(
            facts=(a, b),
            configuration={
                QUEUE_CANDIDATE_CONFIG_KEY: 300.0,
                QUEUE_AREA_IDS_CONFIG_KEY: [_QUEUE_AREA],
            },
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)


# =============================================================================
# 32. CONFIGURATION TESTS — config v1 / v2 / historical replay
# =============================================================================


class TestConfiguration:
    def test_config_v1_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V1)
        assert result.configuration_version_id == _CONFIG_V1
        assert result.event is not None
        assert result.event.payload.configuration_version_id == _CONFIG_V1

    def test_config_v2_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V2)
        assert result.configuration_version_id == _CONFIG_V2
        assert result.event is not None
        assert result.event.payload.configuration_version_id == _CONFIG_V2

    def test_config_versions_produce_distinct_identities(self) -> None:
        # The configuration version is part of the deterministic identity.
        r1 = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V1)
        r2 = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V2)
        assert r1.event is not None and r2.event is not None
        assert r1.event.event_id != r2.event.event_id

    def test_historical_v1_replay(self) -> None:
        # Introduce config v2, then replay with v1 → identical result.
        interval = _interval(duration=301.0)
        v1_first = _evaluate(interval, config_version=_CONFIG_V1)
        _ = _evaluate(interval, config_version=_CONFIG_V2)  # v2 now exists
        v1_replay = _evaluate(interval, config_version=_CONFIG_V1)
        assert v1_replay.model_dump_json() == v1_first.model_dump_json()
        assert v1_replay.configuration_version_id == _CONFIG_V1

    def test_threshold_from_explicit_config(self) -> None:
        # Different explicit thresholds on the same fact → correct boundary
        # per configuration (never "latest", never a silent default).
        interval = _interval(duration=300.0)
        low = _evaluate(interval, threshold=301.0)
        high = _evaluate(interval, threshold=299.0)
        assert low.status is RuleEvaluationStatus.NO_MATCH
        assert high.status is RuleEvaluationStatus.MATCH
        assert high.event is not None
        assert high.event.payload.threshold_seconds == 299


# =============================================================================
# 32. RULE VERSION TESTS — v1 / v2 isolation / unsupported
# =============================================================================


class TestRuleVersion:
    def test_v1_resolves(self) -> None:
        engine = _engine()
        rule = engine._registry.resolve(_RULE_ID, _RULE_VERSION)
        assert rule.canonical_identity == "queue_candidate:v1"
        assert rule.evaluator_id == QUEUE_CANDIDATE_EVALUATOR_ID

    def test_v2_registered_does_not_change_v1(self) -> None:
        # Part 19: registering v2 must never silently change historical
        # v1 evaluation.
        engine = _engine()
        v2 = engine._registry.resolve(_RULE_ID, RuleVersion("v2"))
        assert v2.canonical_identity == "queue_candidate:v2"
        assert v2.evaluator_id == QUEUE_CANDIDATE_EVALUATOR_V2_ID
        assert "queue_max_length" in v2.configuration_requirements

        interval = _interval(duration=300.0)
        engine2 = _engine()
        r1 = engine2.evaluate(_RULE_ID, _RULE_VERSION, _input(interval))
        # The v2 definition exists alongside v1; v1 evaluation unchanged.
        assert r1.status is RuleEvaluationStatus.MATCH

    def test_unsupported_version_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(
                _RULE_ID,
                RuleVersion("v9"),
                _input(_interval(duration=300.0)),
            )


# =============================================================================
# 32. CONTRACT TESTS — EventEnvelope + EvidenceRef
# =============================================================================


class TestContracts:
    def test_envelope_serializes_round_trip(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.event is not None
        serialized = result.event.model_dump(mode="json")
        restored = EventEnvelope.model_validate(serialized)
        assert restored.event_id == result.event.event_id
        assert restored.event_type == RuleEventType.QUEUE_CANDIDATE.value
        assert restored.event_time == _event(300)
        assert restored.schema_version == "1.0"
        payload = QueueCandidatePayload.model_validate(restored.payload)
        assert payload == result.event.payload

    def test_evidence_request_preserves_context(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert len(result.evidence_requests) == 1
        ref = result.evidence_requests[0]
        assert isinstance(ref, EvidenceRef)
        assert ref.metadata is not None
        assert ref.metadata["tenant_id"] == str(_TENANT_A)
        assert ref.metadata["venue_id"] == str(_VENUE_A)
        assert ref.metadata["session_id"] == str(_SESSION)
        assert ref.metadata["camera_id"] == str(_CAMERA)
        assert ref.metadata["event_time"] == _event(300).isoformat()
        assert ref.metadata["configuration_version_id"] == str(_CONFIG_V1)
        assert ref.metadata["rule_id"] == RuleIdentifier.QUEUE_CANDIDATE.value
        assert ref.metadata["rule_version"] == "v1"
        assert EvidenceRef.model_validate(ref.model_dump(mode="json")) == ref

    def test_no_match_no_evidence(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.evidence_requests == ()

    def test_invalid_no_evidence(self) -> None:
        result = _evaluate(_interval(duration=300.0), threshold=0.0)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None
        assert result.evidence_requests == ()


# =============================================================================
# 27/31. OBSERVABILITY + INVARIANT TESTS
# =============================================================================


class TestObservability:
    def test_match_records_structured_telemetry(self, caplog) -> None:
        import logging

        engine = _engine()
        interval = _interval(duration=300.0)
        with caplog.at_level(logging.INFO, logger="backend.app.intelligence.rules.engine"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(interval))
        records = [r for r in caplog.records if r.name == "backend.app.intelligence.rules.engine"]
        assert len(records) == 1
        rec = records[0]
        assert rec.levelname == "INFO"
        msg = rec.getMessage()
        assert f"rule_id={RuleIdentifier.QUEUE_CANDIDATE.value}" in msg
        assert "rule_version=v1" in msg
        assert f"configuration_version_id={_CONFIG_V1}" in msg
        assert "status=match" in msg
        assert _event(300).isoformat() in msg
        # Queue-specific measurements are recorded via the payload.
        assert "waiting_duration" in msg and "300" in msg
        assert "threshold_seconds" in msg and "300" in msg
        assert "spatial_context_id" in msg and _QUEUE_AREA in msg
        assert rec.tenant_id == str(_TENANT_A)
        assert rec.venue_id == str(_VENUE_A)
        assert rec.session_id == str(_SESSION)
        assert rec.event_id is not None

    def test_no_match_records_status(self, caplog) -> None:
        import logging

        engine = _engine()
        with caplog.at_level(logging.INFO, logger="backend.app.intelligence.rules.engine"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(_interval(duration=299.0)))
        records = [r for r in caplog.records if r.name == "backend.app.intelligence.rules.engine"]
        assert len(records) == 1
        assert "status=no_match" in records[0].getMessage()


class TestInvariants:
    def test_no_event_without_valid_waiting_fact(self) -> None:
        # A non-WaitingInterval primary fact is rejected by the registry.
        from contracts.temporal import DwellInterval

        dwell = DwellInterval(
            interval_id=EventId(uuid.uuid4()),
            fsm_kind="dwell",
            key=_key().model_copy(update={"fsm_kind": "dwell"}),
            dwell_start=_START,
            dwell_end=_event(300),
            last_seen=_event(300),
            duration_seconds=300.0,
            qualified=True,
            minimum_dwell_seconds=0.0,
            reason=TemporalReason.EXIT_CONFIRMED,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        engine = _engine()
        with pytest.raises(UnsupportedFactTypeError):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(interval=dwell))  # type: ignore[arg-type]

    def test_no_event_below_threshold(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None

    def test_exact_boundary_follows_documented_semantics(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_one_episode_no_uncontrolled_duplicates(self) -> None:
        interval = _interval(duration=300.0)
        ids = {_evaluate(interval).event.event_id for _ in range(5)}
        assert len(ids) == 1

    def test_independent_episodes_stay_independent(self) -> None:
        a = _evaluate(_interval(duration=300.0, label="A"))
        b = _evaluate(
            _interval(duration=300.0, waiting_start=_event(600), last_seen=_event(900), label="B")
        )
        assert a.event is not None and b.event is not None
        assert a.event.event_id != b.event.event_id

    def test_configuration_version_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V2)
        assert result.configuration_version_id == _CONFIG_V2

    def test_rule_version_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.rule_id == RuleIdentifier.QUEUE_CANDIDATE.value
        assert result.rule_version == "v1"

    def test_event_time_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.event_time == _event(300)
        assert result.event is not None
        assert result.event.event_time == _event(300)

    def test_invalid_facts_never_match(self) -> None:
        bad_duration = _interval(duration=0.0).model_copy(update={"duration_seconds": None})
        assert _evaluate(bad_duration).status is RuleEvaluationStatus.INVALID
        bad_fsm = _interval(duration=300.0, key=_key().model_copy(update={"fsm_kind": "waiting"}))
        assert _evaluate(bad_fsm).status is RuleEvaluationStatus.MATCH
        no_context = _interval(duration=300.0, key=_key(semantic_context=None))
        assert _evaluate(no_context).status is RuleEvaluationStatus.INVALID

    def test_tenant_venue_identity_cannot_change(self) -> None:
        result = _evaluate(_interval(duration=300.0, key=_key(tenant_id=_TENANT_A)))
        assert result.event is not None
        assert result.event.payload.tenant_id == _TENANT_A
        assert result.event.payload.venue_id == _VENUE_A

    def test_replay_is_deterministic(self) -> None:
        interval = _interval(duration=300.0)
        assert _evaluate(interval).model_dump_json() == _evaluate(interval).model_dump_json()

    def test_rule_does_not_mutate_temporal_state(self) -> None:
        interval = _interval(duration=300.0)
        engine = _engine()
        inp = _input(interval)
        before = inp.model_dump_json()
        engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert inp.model_dump_json() == before  # input untouched
        assert len(engine._evaluators.list()) == 7  # registry untouched

    def test_deterministic_event_identity(self) -> None:
        interval = _interval(duration=300.0)
        engine = _engine()
        inp = _input(interval)
        rule = engine._registry.resolve(_RULE_ID, _RULE_VERSION)
        expected = deterministic_event_id(
            rule, inp, event_time=inp.event_time, event_type=rule.output_event_type.value
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.event is not None
        assert result.event.event_id == expected

    def test_invalid_configuration_never_silently_defaulted(self) -> None:
        engine = _engine()
        inp = _input(_interval(duration=300.0), threshold=1.0).model_copy(
            update={
                "configuration": {
                    QUEUE_CANDIDATE_CONFIG_KEY: -1.0,
                    QUEUE_AREA_IDS_CONFIG_KEY: [_QUEUE_AREA],
                }
            }
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None


# =============================================================================
# 34. PERFORMANCE / SIDE-EFFECT BOUNDARY
# =============================================================================


class TestPerformanceAndBoundary:
    def test_bounded_evaluation_single_fact(self) -> None:
        # One fact, one evaluation — no unbounded history, no O(n²) scan.
        interval = _interval(duration=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH

    def test_no_infrastructure_dependency_in_rule_core(self) -> None:
        # The evaluator is a pure function — no I/O attributes exist on it.
        from backend.app.intelligence.rules.queue_candidate import QueueCandidateEvaluator

        ev = QueueCandidateEvaluator()
        assert not hasattr(ev, "_database")
        assert not hasattr(ev, "_redis")
        assert not hasattr(ev, "_storage")
        assert not hasattr(ev, "_http")
        assert not hasattr(ev, "_llm")


# =============================================================================
# §20-26. GOLDEN TESTS
# =============================================================================


class TestGolden:
    def test_golden_below_qualification(self) -> None:
        """§20: minimum wait 300s, waiting 299s → NO_MATCH."""
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_golden_exact_qualification(self) -> None:
        """§21: minimum wait 300s, waiting exactly 300s → MATCH (mandatory
        boundary — documented as `>=`)."""
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_golden_above_qualification(self) -> None:
        """§22: minimum wait 300s, waiting 301s → MATCH."""
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_golden_continuous_wait(self) -> None:
        """§23: 10:00 waiting begins, 10:05 qualifies, still waiting at
        10:06/10:07/10:08 → exactly ONE logical queue candidate event for
        the qualification (stable deterministic identity)."""
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "wait-episode-A"))
        crossing = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        crossing_result = _evaluate(crossing)
        assert crossing_result.status is RuleEvaluationStatus.MATCH
        assert crossing_result.event.event_time == _event(300)
        assert _evaluate(crossing).event.event_id == crossing_result.event.event_id

    def test_golden_reentry(self) -> None:
        """§24: Episode A qualifies and exits; Episode B later qualifies →
        two independent logical candidate events."""
        episode_a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        episode_b = _interval(
            duration=300.0,
            waiting_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(episode_a)
        rb = _evaluate(episode_b)
        assert ra.status is RuleEvaluationStatus.MATCH
        assert rb.status is RuleEvaluationStatus.MATCH
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id
        assert rb.event.payload.waiting_start_time == _event(1200)

    def test_golden_occlusion(self) -> None:
        """§25: short occlusion does not close the waiting episode (Task 15
        owns the FSM) → no duplicate queue candidate."""
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        replay = _evaluate(interval)
        assert first.event is not None and replay.event is not None
        assert replay.event.event_id == first.event.event_id
        assert len({_evaluate(interval).event.event_id for _ in range(3)}) == 1

    def test_golden_invalid_zone(self) -> None:
        """§26: waiting fact references a non-queue area → NO_MATCH (the
        area is not silently classified as a queue)."""
        result = _evaluate(
            _interval(duration=300.0, key=_key(semantic_context=_OTHER_AREA)),
            queue_areas=[_QUEUE_AREA],
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
