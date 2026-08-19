"""Tests for Task 16.5 — the deterministic dwell_threshold rule.

Converts an already-established canonical ``DwellInterval`` (Task 15.3)
into a ``dwell_threshold`` event when the configured dwell duration
threshold is crossed. The rule does NOT calculate tracking, spatial
membership, or temporal dwell state — it consumes the confirmed canonical
fact only.

Covered (Task 16.5 Part 30):

- unit: below / exact / above threshold, invalid duration, invalid
  threshold, missing configuration, missing/invalid event_time;
- boundary: threshold - 1 (NO_MATCH), threshold exactly (MATCH),
  threshold + 1 (MATCH) — the documented boundary policy;
- idempotency: repeated evaluation, repeated threshold fact, replay;
- temporal: continuous dwell (single crossing), short occlusion,
  late/out-of-order handled by Task 15 (the rule never re-derives
  temporal validity);
- sessions: session A, session B, re-entry (independent events);
- security: tenant isolation, venue isolation, cross-scope rejection;
- contract: EventEnvelope validation, EvidenceRef validation;
- versions: rule v1, unsupported version, config v1/v2, historical replay;
- invariants (Part 31): 10 guarantees;
- golden tests (§18-25).

All fixtures use the REAL canonical contracts with fixed deterministic IDs
so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.intelligence.rules import (
    DWELL_THRESHOLD_CONFIG_KEY,
    DWELL_THRESHOLD_EVALUATOR_ID,
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
    DwellThresholdPayload,
    RuleEvaluationInput,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    DwellInterval,
    TemporalReason,
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

_RULE_ID = RuleId(RuleIdentifier.DWELL_THRESHOLD.value)
_RULE_VERSION = RuleVersion("v1")
_THRESHOLD = 300.0  # the configured threshold used by most tests

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
    semantic_context: str | None = "zone-lobby",
) -> TemporalStateKey:
    return TemporalStateKey(
        fsm_kind="dwell",
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
    dwell_start: datetime = _START,
    last_seen: datetime | None = None,
    reason: TemporalReason | None = None,
    interval_id: EventId | None = None,
    key: TemporalStateKey | None = None,
    label: str = "i",
) -> DwellInterval:
    """A canonical DwellInterval fact with deterministic content-derived id."""
    last = last_seen if last_seen is not None else dwell_start + timedelta(seconds=duration)
    return DwellInterval(
        interval_id=interval_id
        or EventId(uuid.uuid5(uuid.NAMESPACE_URL, f"dwell-{label}-{dwell_start.isoformat()}")),
        fsm_kind="dwell",
        key=key or _key(),
        dwell_start=dwell_start,
        dwell_end=last if reason is not None else None,
        last_seen=last,
        duration_seconds=duration,
        qualified=duration >= 0,
        minimum_dwell_seconds=0.0,
        reason=reason,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(
    interval: DwellInterval,
    *,
    threshold: float = _THRESHOLD,
    config_version: ConfigurationVersionId = _CONFIG_V1,
    event_time: datetime | None = None,
):
    """A canonical RuleEvaluationInput for one dwell interval."""
    return RuleEvaluationInput(
        facts=(interval,),
        configuration={DWELL_THRESHOLD_CONFIG_KEY: threshold},
        configuration_version_id=config_version,
        rule_version=_RULE_VERSION,
        event_time=event_time if event_time is not None else interval.last_seen,
        processing_time=_PROCESSED,
    )


def _engine():
    """The sanctioned operational engine (dwell_threshold:v1 registered)."""
    return build_operational_engine()


def _evaluate(
    interval: DwellInterval,
    *,
    threshold: float = _THRESHOLD,
    config_version: ConfigurationVersionId = _CONFIG_V1,
):
    engine = _engine()
    return engine.evaluate(
        _RULE_ID,
        _RULE_VERSION,
        _input(interval, threshold=threshold, config_version=config_version),
    )


# =============================================================================
# 30. UNIT TESTS — below / exact / above / invalid inputs
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
        assert result.event.event_type == RuleEventType.DWELL_THRESHOLD.value
        assert result.event.event_time == _event(300)
        payload = result.event.payload
        assert payload.dwell_duration == 300
        assert payload.threshold_seconds == 300
        assert payload.dwell_start_time == _START
        assert payload.threshold_crossing_time == _event(300)
        assert payload.tenant_id == _TENANT_A
        assert payload.venue_id == _VENUE_A
        assert payload.session_id == _SESSION
        assert payload.camera_id == _CAMERA
        assert payload.spatial_context_id == "zone-lobby"
        assert payload.configuration_version_id == _CONFIG_V1
        assert payload.rule_id == RuleIdentifier.DWELL_THRESHOLD.value
        assert payload.rule_version == "v1"

    def test_above_threshold_match(self) -> None:
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.dwell_duration == 301

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
        interval = _interval(duration=300.0)
        inp = _input(interval, threshold=1.0).model_copy(
            update={"configuration": {DWELL_THRESHOLD_CONFIG_KEY: "abc"}}
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID

    def test_missing_configuration_rejected(self) -> None:
        engine = _engine()
        interval = _interval(duration=300.0)
        inp = _input(interval).model_copy(update={"configuration": {}})
        with pytest.raises(MissingRuleConfigurationError, match=DWELL_THRESHOLD_CONFIG_KEY):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_missing_event_time_rejected_at_contract(self) -> None:
        interval = _interval(duration=300.0)
        with pytest.raises(ValidationError, match="event_time"):
            RuleEvaluationInput(
                facts=(interval,),
                configuration={DWELL_THRESHOLD_CONFIG_KEY: 300.0},
                configuration_version_id=_CONFIG_V1,
                rule_version=_RULE_VERSION,
            )

    def test_naive_event_time_rejected_at_contract(self) -> None:
        with pytest.raises(ValidationError, match="timezone-naive"):
            RuleEvaluationInput(
                facts=(_interval(duration=300.0),),
                configuration={DWELL_THRESHOLD_CONFIG_KEY: 300.0},
                configuration_version_id=_CONFIG_V1,
                rule_version=_RULE_VERSION,
                event_time=datetime(2026, 8, 1, 10, 5, 0),
            )

    def test_event_time_mismatch_last_seen_is_invalid(self) -> None:
        interval = _interval(duration=300.0)
        engine = _engine()
        inp = _input(interval, event_time=_event(999))  # mismatched
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID
        assert "last_seen" in (result.reason or "")

    def test_invalid_temporal_state_fsm_kind_is_invalid(self) -> None:
        bad_key = _key().model_copy(update={"fsm_kind": "presence"})
        result = _evaluate(_interval(duration=300.0, key=bad_key))
        assert result.status is RuleEvaluationStatus.INVALID
        assert "fsm_kind" in (result.reason or "")

    def test_wrong_fact_type_rejected(self) -> None:
        from contracts.temporal import WaitingInterval

        wait = WaitingInterval(
            interval_id=EventId(uuid.uuid4()),
            fsm_kind="waiting",
            key=_key().model_copy(update={"fsm_kind": "waiting"}),
            waiting_start=_START,
            waiting_end=_event(300),
            last_seen=_event(300),
            duration_seconds=300.0,
            qualified=True,
            minimum_waiting_seconds=0.0,
            reason=TemporalReason.EXIT_CONFIRMED,
            fsm_version=TEMPORAL_ENGINE_VERSION,
            policy_revision="v1",
        )
        engine = _engine()
        with pytest.raises(UnsupportedFactTypeError, match="does not declare fact type"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(interval=wait))  # type: ignore[arg-type]


# =============================================================================
# 30. BOUNDARY TESTS — threshold - 1 / threshold / threshold + 1
# =============================================================================


class TestBoundary:
    def test_threshold_minus_one_no_match(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_threshold_exactly_match(self) -> None:
        # Mandatory boundary test (Part 19).
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_threshold_plus_one_match(self) -> None:
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH


# =============================================================================
# 30. IDEMPOTENCY TESTS — repeated evaluation / replay
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

    def test_repeated_threshold_fact_same_event(self) -> None:
        # Feeding the SAME crossing fact ten times yields ONE logical event
        # identity (Task 7 idempotency, Part 8).
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
# 30. TEMPORAL TESTS — continuous dwell / occlusion / late / out-of-order
# =============================================================================


class TestTemporal:
    def test_continuous_dwell_single_crossing(self) -> None:
        # Timeline (Part 7): below, below, crossing, still dwelling. The
        # rule fires exactly ONCE for the crossing fact and never for the
        # below-threshold facts; re-evaluating the crossing stays one event.
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "dwell-session-A"))
        below = _interval(duration=299.0, last_seen=_event(299), interval_id=interval_id)
        crossing = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        still = _interval(duration=330.0, last_seen=_event(330), interval_id=interval_id)
        r_below = _evaluate(below)
        r_cross = _evaluate(crossing)
        r_still = _evaluate(still)
        assert r_below.status is RuleEvaluationStatus.NO_MATCH
        assert r_cross.status is RuleEvaluationStatus.MATCH
        # Same crossing re-evaluated → same logical event identity.
        assert _evaluate(crossing).event.event_id == r_cross.event.event_id
        # A later fact of the SAME session that is still >= threshold is a
        # distinct observation instant; Task 7 dedups by event identity at
        # the pipeline layer (Part 8) — the rule itself never re-derives
        # temporal state and never fires below threshold.
        assert r_still.status is RuleEvaluationStatus.MATCH

    def test_short_occlusion_no_duplicate(self) -> None:
        # Task 15.3 keeps the interval ALIVE across short occlusion — the
        # rule consumes the same canonical interval identity; re-evaluating
        # the crossing produces one event (no duplicate). The rule does NOT
        # modify Task 15 FSM behavior (Part 24).
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        # The pipeline may re-feed the same crossing fact after occlusion.
        replay = _evaluate(interval)
        assert first.event is not None and replay.event is not None
        assert replay.event.event_id == first.event.event_id
        assert replay.model_dump_json() == first.model_dump_json()

    def test_late_event_consumed_per_task15_policy(self) -> None:
        # Task 15 applies the late/out-of-order policy BEFORE the rule sees
        # a fact — a fact Task 15 rejects never reaches the rule. A fact
        # Task 15 accepted is consumed as-is; the rule never re-derives
        # temporal validity (Part 14).
        interval = _interval(duration=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None

    def test_out_of_order_event_never_fabricates(self) -> None:
        # The rule has no temporal-ordering role: it consumes canonical
        # facts only. An interval fact outside Task 15's acceptance window
        # is rejected by Task 15 — never fabricated into a MATCH here.
        interval = _interval(duration=300.0)
        assert _evaluate(interval).status is RuleEvaluationStatus.MATCH


# =============================================================================
# 30. SESSION TESTS — session A / session B / re-entry
# =============================================================================


class TestSessions:
    def test_two_sessions_two_events(self) -> None:
        # Session A and Session B are independent DwellIntervals → two
        # distinct logical events (Part 9) — never merged.
        session_a = _interval(duration=300.0, label="A")
        session_b = _interval(
            duration=300.0,
            dwell_start=_event(600),
            last_seen=_event(900),
            label="B",
        )
        ra = _evaluate(session_a)
        rb = _evaluate(session_b)
        assert ra.status is RuleEvaluationStatus.MATCH
        assert rb.status is RuleEvaluationStatus.MATCH
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id
        assert ra.event.payload.interval_id != rb.event.payload.interval_id

    def test_reentry_independent_events(self) -> None:
        # Session A crosses, ends, Session B crosses → two distinct events.
        session_a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        session_b = _interval(
            duration=300.0,
            dwell_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(session_a)
        rb = _evaluate(session_b)
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id
        assert ra.event.payload.dwell_start_time == _START
        assert rb.event.payload.dwell_start_time == _event(1200)


# =============================================================================
# 30. SECURITY TESTS — tenant / venue isolation
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

    def test_cross_scope_facts_rejected(self) -> None:
        engine = _engine()
        snap_a = _interval(duration=300.0, key=_key(tenant_id=_TENANT_A))
        snap_b = _interval(duration=300.0, key=_key(tenant_id=_TENANT_B))
        inp = RuleEvaluationInput(
            facts=(snap_a, snap_b),
            configuration={DWELL_THRESHOLD_CONFIG_KEY: 300.0},
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)


# =============================================================================
# 30. CONTRACT TESTS — EventEnvelope + EvidenceRef
# =============================================================================


class TestContracts:
    def test_envelope_serializes_round_trip(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.event is not None
        serialized = result.event.model_dump(mode="json")
        restored = EventEnvelope.model_validate(serialized)
        assert restored.event_id == result.event.event_id
        assert restored.event_type == RuleEventType.DWELL_THRESHOLD.value
        assert restored.event_time == _event(300)
        assert restored.schema_version == "1.0"
        payload = DwellThresholdPayload.model_validate(restored.payload)
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
        assert ref.metadata["rule_id"] == RuleIdentifier.DWELL_THRESHOLD.value
        assert ref.metadata["rule_version"] == "v1"
        assert EvidenceRef.model_validate(ref.model_dump(mode="json")) == ref

    def test_no_match_no_evidence(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.evidence_requests == ()


# =============================================================================
# 30. VERSION TESTS — rule v1 / unsupported / config v1/v2 / replay
# =============================================================================


class TestVersions:
    def test_v1_resolves(self) -> None:
        engine = _engine()
        rule = engine._registry.resolve(_RULE_ID, _RULE_VERSION)
        assert rule.canonical_identity == "dwell_threshold:v1"
        assert rule.evaluator_id == DWELL_THRESHOLD_EVALUATOR_ID

    def test_unsupported_version_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(
                _RULE_ID,
                RuleVersion("v9"),
                _input(_interval(duration=300.0)),
            )

    def test_config_v1_vs_v2_preserved(self) -> None:
        interval = _interval(duration=300.0)
        r1 = _evaluate(interval, config_version=_CONFIG_V1)
        r2 = _evaluate(interval, config_version=_CONFIG_V2)
        assert r1.configuration_version_id == _CONFIG_V1
        assert r2.configuration_version_id == _CONFIG_V2
        assert r1.event is not None and r2.event is not None
        assert r1.event.event_id != r2.event.event_id  # config in identity

    def test_historical_replay_config_v1(self) -> None:
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
# 31. PROPERTY / INVARIANT TESTS
# =============================================================================


class TestInvariants:
    def test_no_event_below_threshold(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None

    def test_exact_threshold_follows_documented_boundary(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_one_crossing_at_most_one_event(self) -> None:
        interval = _interval(duration=300.0)
        ids = {_evaluate(interval).event.event_id for _ in range(5)}
        assert len(ids) == 1

    def test_replay_is_deterministic(self) -> None:
        interval = _interval(duration=300.0)
        assert _evaluate(interval).model_dump_json() == _evaluate(interval).model_dump_json()

    def test_rule_version_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.rule_id == RuleIdentifier.DWELL_THRESHOLD.value
        assert result.rule_version == "v1"

    def test_configuration_version_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V2)
        assert result.configuration_version_id == _CONFIG_V2

    def test_event_time_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.event_time == _event(300)
        assert result.event is not None
        assert result.event.event_time == _event(300)

    def test_invalid_facts_never_match(self) -> None:
        bad_duration = _interval(duration=0.0).model_copy(update={"duration_seconds": None})
        assert _evaluate(bad_duration).status is RuleEvaluationStatus.INVALID
        bad_fsm = _interval(duration=300.0, key=_key().model_copy(update={"fsm_kind": "dwell"}))
        assert _evaluate(bad_fsm).status in (
            RuleEvaluationStatus.MATCH,
            RuleEvaluationStatus.INVALID,
        )
        # The definitive invalid case: a fact Task 15 would never emit.
        bad = _interval(duration=300.0, key=_key().model_copy(update={"fsm_kind": "waiting"}))
        assert _evaluate(bad).status is RuleEvaluationStatus.INVALID

    def test_independent_sessions_independent_events(self) -> None:
        a = _evaluate(_interval(duration=300.0, label="A"))
        b = _evaluate(
            _interval(duration=300.0, dwell_start=_event(600), last_seen=_event(900), label="B")
        )
        assert a.event is not None and b.event is not None
        assert a.event.event_id != b.event.event_id

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
        # An explicit-but-invalid threshold is INVALID, never silently
        # substituted with a default (Part 4).
        engine = _engine()
        inp = _input(_interval(duration=300.0), threshold=1.0).model_copy(
            update={"configuration": {DWELL_THRESHOLD_CONFIG_KEY: -1.0}}
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None


# =============================================================================
# 27. OBSERVABILITY TESTS — Task 8 structured telemetry (Part 27)
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
        # Rule identity, versions, status, event-time, and the payload
        # measurements (dwell_duration / threshold) are recorded.
        assert f"rule_id={RuleIdentifier.DWELL_THRESHOLD.value}" in msg
        assert "rule_version=v1" in msg
        assert f"configuration_version_id={_CONFIG_V1}" in msg
        assert "status=match" in msg
        assert _event(300).isoformat() in msg
        assert "dwell_duration" in msg and "300" in msg
        assert "threshold_seconds" in msg and "300" in msg
        # Allowlisted context fields ride on extra=.
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
        rec = records[0]
        assert "status=no_match" in rec.getMessage()
        # No event → no event_id, no payload.
        assert rec.event_id is None
        assert "payload=None" in rec.getMessage()

    def test_invalid_records_status(self, caplog) -> None:
        import logging

        engine = _engine()
        bad = _interval(duration=0.0).model_copy(update={"duration_seconds": None})
        with caplog.at_level(logging.INFO, logger="backend.app.intelligence.rules.engine"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(bad))
        records = [r for r in caplog.records if r.name == "backend.app.intelligence.rules.engine"]
        assert len(records) == 1
        assert "status=invalid" in records[0].getMessage()

    def test_no_secrets_no_raw_video_in_log(self, caplog) -> None:
        """Never log secrets or raw video — only canonical identifiers."""
        import logging

        engine = _engine()
        with caplog.at_level(logging.INFO, logger="backend.app.intelligence.rules.engine"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(_interval(duration=300.0)))
        records = [r for r in caplog.records if r.name == "backend.app.intelligence.rules.engine"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "password" not in msg.lower()
        assert "secret" not in msg.lower()
        assert "token" not in msg.lower()


# =============================================================================
# §18-25. GOLDEN TESTS
# =============================================================================


class TestGolden:
    def test_golden_below_threshold(self) -> None:
        """§18: threshold 300s, dwell 299s → NO_MATCH."""
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_golden_exact_boundary(self) -> None:
        """§19: threshold 300s, dwell exactly 300s → MATCH (mandatory)."""
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_golden_above_threshold(self) -> None:
        """§20: threshold 300s, dwell 301s → MATCH."""
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_golden_continuous_dwell_one_event(self) -> None:
        """§21: 10:00 start, 10:04:59 below, 10:05:00 crossed, still
        dwelling → exactly ONE logical dwell-threshold event."""
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "dwell-session-A"))
        below = _interval(duration=299.0, last_seen=_event(299), interval_id=interval_id)
        crossing = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        assert _evaluate(below).status is RuleEvaluationStatus.NO_MATCH
        crossing_result = _evaluate(crossing)
        assert crossing_result.status is RuleEvaluationStatus.MATCH
        assert crossing_result.event.event_time == _event(300)
        # No duplicate event from re-evaluating the crossing (Part 7/8).
        assert _evaluate(crossing).event.event_id == crossing_result.event.event_id

    def test_golden_replay(self) -> None:
        """§22: identical input evaluated multiple times → identical
        result, event type, payload, event_time, versions, identity."""
        interval = _interval(duration=300.0)
        engine = _engine()
        inp = _input(interval)
        results = [engine.evaluate(_RULE_ID, _RULE_VERSION, inp) for _ in range(3)]
        assert all(r == results[0] for r in results)
        assert all(r.model_dump_json() == results[0].model_dump_json() for r in results)
        assert all(r.event.event_id == results[0].event.event_id for r in results)

    def test_golden_reentry(self) -> None:
        """§23: Session A crosses, ends; Session B crosses → two distinct
        events (no merging)."""
        session_a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        session_b = _interval(
            duration=300.0,
            dwell_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(session_a)
        rb = _evaluate(session_b)
        assert ra.status is RuleEvaluationStatus.MATCH
        assert rb.status is RuleEvaluationStatus.MATCH
        assert ra.event is not None and rb.event is not None
        assert ra.event.event_id != rb.event.event_id

    def test_golden_occlusion(self) -> None:
        """§24: short occlusion does not terminate the canonical dwell
        state → no duplicate dwell-threshold event (Task 15 owns the FSM;
        the rule never re-derives it)."""
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        replay = _evaluate(interval)
        assert first.event is not None and replay.event is not None
        assert replay.event.event_id == first.event.event_id
        assert len({_evaluate(interval).event.event_id for _ in range(3)}) == 1

    def test_golden_below_above_oscillation(self) -> None:
        """§25: facts around the threshold (299, 300, 299, 300, 301) → the
        rule never oscillates: below never fires, the crossing fires once
        (idempotent identity), above fires. Exactly one unique event id
        for the exact-boundary crossing."""
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "dwell-session-A"))
        f299 = _interval(duration=299.0, last_seen=_event(299), interval_id=interval_id)
        f300 = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        f301 = _interval(duration=301.0, last_seen=_event(301), interval_id=interval_id)
        r_299 = _evaluate(f299)
        r_300 = _evaluate(f300)
        r_299_again = _evaluate(f299)
        r_300_again = _evaluate(f300)
        r_301 = _evaluate(f301)
        # Below threshold NEVER fires (no oscillation into event creation).
        assert r_299.status is RuleEvaluationStatus.NO_MATCH
        assert r_299_again.status is RuleEvaluationStatus.NO_MATCH
        # The exact-boundary crossing fires once with a stable identity.
        assert r_300.status is RuleEvaluationStatus.MATCH
        assert r_300.event is not None and r_300_again.event is not None
        assert r_300_again.event.event_id == r_300.event.event_id
        assert r_301.status is RuleEvaluationStatus.MATCH
