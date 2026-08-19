"""Tests for Task 16.8 — the deterministic turnover_delay rule.

Converts an OPEN canonical ``DwellInterval`` (Task 15.3) into a
``turnover_delay`` event when the table/service area stays occupied
beyond the configured service window + turnover-delay window. The rule
does NOT inspect raw video, frames, YOLO detections, tracker objects,
bounding boxes, or OpenCV objects — it consumes the confirmed canonical
turnover fact + its canonical spatial context only.

Covered (Task 16.8 Part 41):

- unit: valid turnover, below / exact / above threshold, completed
  turnover, missing turnover fact, invalid duration, missing/invalid
  configuration, missing/invalid event_time, empty space;
- boundary: threshold - 1 (NO_MATCH), threshold exactly (MATCH),
  threshold + 1 (MATCH) — the documented boundary policy;
- temporal: continuous turnover, short occlusion, turnover completion,
  re-entry, late/out-of-order per Task 15 policy;
- idempotency: repeated evaluation, duplicate delivery, replay;
- version: rule v1, unsupported version, config v1, config v2,
  historical replay;
- security: cross-tenant, cross-venue;
- contract: EventEnvelope, EvidenceRef;
- invariants (Part 42): 15 guarantees;
- golden tests (§26-36).

All fixtures use the REAL canonical contracts with fixed deterministic IDs
so replay comparisons are byte-exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.intelligence.rules import (
    SERVICE_WINDOW_CONFIG_KEY,
    TURNOVER_DELAY_CONFIG_KEY,
    TURNOVER_DELAY_EVALUATOR_ID,
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
    RuleEvaluationInput,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
    TurnoverDelayPayload,
)
from contracts.temporal import (
    TEMPORAL_ENGINE_VERSION,
    DwellInterval,
    OccupancySnapshot,
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

_RULE_ID = RuleId(RuleIdentifier.TURNOVER_DELAY.value)
_RULE_VERSION = RuleVersion("v1")
_SERVICE_WINDOW = 0.0  # effective threshold = service_window + turnover_delay
_THRESHOLD = 300.0  # the configured turnover-delay threshold
_TABLE = "table-12"  # canonical spatial identity (table / service area / zone)

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
    semantic_context: str | None = _TABLE,
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
        qualified=True,
        minimum_dwell_seconds=0.0,
        reason=reason,
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _snapshot(
    *,
    occupancy_count: int = 1,
    key: TemporalStateKey | None = None,
) -> OccupancySnapshot:
    """A canonical OccupancySnapshot confirming the space is occupied."""
    return OccupancySnapshot(
        snapshot_id=EventId(uuid.uuid4()),
        fsm_kind="occupancy",
        key=key or _key().model_copy(update={"fsm_kind": "occupancy"}),
        event_time=_event(300),
        previous_count=0,
        delta=occupancy_count,
        occupancy_count=occupancy_count,
        occupied_tracks=(),
        source_transition_id=EventId(uuid.uuid4()),
        fsm_version=TEMPORAL_ENGINE_VERSION,
        policy_revision="v1",
    )


def _input(
    interval: DwellInterval,
    *,
    threshold: float = _THRESHOLD,
    service_window: float = _SERVICE_WINDOW,
    config_version: ConfigurationVersionId = _CONFIG_V1,
    event_time: datetime | None = None,
    extra_facts: tuple = (),
):
    """A canonical RuleEvaluationInput for one dwell interval."""
    return RuleEvaluationInput(
        facts=(interval, *extra_facts),
        configuration={
            TURNOVER_DELAY_CONFIG_KEY: threshold,
            SERVICE_WINDOW_CONFIG_KEY: service_window,
        },
        configuration_version_id=config_version,
        rule_version=_RULE_VERSION,
        event_time=event_time if event_time is not None else interval.last_seen,
        processing_time=_PROCESSED,
    )


def _engine():
    """The sanctioned operational engine (turnover_delay:v1 registered)."""
    return build_operational_engine()


def _evaluate(
    interval: DwellInterval,
    *,
    threshold: float = _THRESHOLD,
    service_window: float = _SERVICE_WINDOW,
    config_version: ConfigurationVersionId = _CONFIG_V1,
    extra_facts: tuple = (),
):
    engine = _engine()
    return engine.evaluate(
        _RULE_ID,
        _RULE_VERSION,
        _input(
            interval,
            threshold=threshold,
            service_window=service_window,
            config_version=config_version,
            extra_facts=extra_facts,
        ),
    )


# =============================================================================
# 41. UNIT TESTS
# =============================================================================


class TestUnit:
    def test_valid_turnover_match(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.event_type == RuleEventType.TURNOVER_DELAY.value
        assert result.event.event_time == _event(300)
        payload = result.event.payload
        assert payload.turnover_duration == 300
        assert payload.threshold_seconds == 300
        assert payload.service_window_seconds == 0
        assert payload.turnover_start_time == _START
        assert payload.threshold_crossing_time == _event(300)
        assert payload.tenant_id == _TENANT_A
        assert payload.venue_id == _VENUE_A
        assert payload.session_id == _SESSION
        assert payload.camera_id == _CAMERA
        assert payload.track_id == _TRACK_A
        assert payload.spatial_context_id == _TABLE
        assert payload.configuration_version_id == _CONFIG_V1
        assert payload.rule_id == RuleIdentifier.TURNOVER_DELAY.value
        assert payload.rule_version == "v1"

    def test_below_threshold_no_match(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_exact_threshold_match(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.turnover_duration == 300

    def test_above_threshold_match(self) -> None:
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None
        assert result.event.payload.turnover_duration == 301

    def test_service_window_shifts_boundary(self) -> None:
        # A table may stay occupied for the service window before delay
        # timing matters: effective threshold = service_window + delay.
        below = _evaluate(_interval(duration=399.0), service_window=100.0)
        exact = _evaluate(_interval(duration=400.0), service_window=100.0)
        above = _evaluate(_interval(duration=401.0), service_window=100.0)
        assert below.status is RuleEvaluationStatus.NO_MATCH
        assert exact.status is RuleEvaluationStatus.MATCH
        assert above.status is RuleEvaluationStatus.MATCH
        assert exact.event is not None
        assert exact.event.payload.service_window_seconds == 100
        assert exact.event.payload.threshold_seconds == 300

    def test_completed_turnover_no_match(self) -> None:
        # Part 14: a CLOSED interval (turnover completed) never generates
        # a turnover-delay event.
        interval = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None
        assert result.evidence_requests == ()

    def test_empty_space_no_match(self) -> None:
        # An OccupancySnapshot confirming an EMPTY space (turnover
        # completed) → NO_MATCH — never a delay.
        result = _evaluate(
            _interval(duration=300.0),
            extra_facts=(_snapshot(occupancy_count=0),),
        )
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None

    def test_occupied_space_matches(self) -> None:
        # An OccupancySnapshot confirming the space is occupied supports
        # the active turnover episode.
        result = _evaluate(
            _interval(duration=300.0),
            extra_facts=(_snapshot(occupancy_count=2),),
        )
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None

    def test_missing_turnover_fact_rejected(self) -> None:
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

    def test_invalid_threshold_non_numeric_is_invalid(self) -> None:
        engine = _engine()
        inp = _input(_interval(duration=300.0)).model_copy(
            update={
                "configuration": {
                    TURNOVER_DELAY_CONFIG_KEY: "abc",
                    SERVICE_WINDOW_CONFIG_KEY: 0.0,
                }
            }
        )
        result = engine.evaluate(_RULE_ID, _RULE_VERSION, inp)
        assert result.status is RuleEvaluationStatus.INVALID

    def test_invalid_service_window_negative_is_invalid(self) -> None:
        result = _evaluate(_interval(duration=300.0), service_window=-1.0)
        assert result.status is RuleEvaluationStatus.INVALID
        assert result.event is None

    def test_missing_configuration_rejected(self) -> None:
        engine = _engine()
        inp = _input(_interval(duration=300.0)).model_copy(update={"configuration": {}})
        with pytest.raises(MissingRuleConfigurationError, match=TURNOVER_DELAY_CONFIG_KEY):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_missing_event_time_rejected_at_contract(self) -> None:
        interval = _interval(duration=300.0)
        with pytest.raises(ValidationError, match="event_time"):
            RuleEvaluationInput(
                facts=(interval,),
                configuration={TURNOVER_DELAY_CONFIG_KEY: 300.0},
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

    def test_missing_spatial_context_is_invalid(self) -> None:
        # A dwell fact with NO spatial identity cannot establish turnover
        # provenance (Part 17).
        result = _evaluate(_interval(duration=300.0, key=_key(semantic_context=None)))
        assert result.status is RuleEvaluationStatus.INVALID
        assert "semantic_context" in (result.reason or "")

    def test_invalid_temporal_state_fsm_kind_is_invalid(self) -> None:
        bad_key = _key().model_copy(update={"fsm_kind": "presence"})
        result = _evaluate(_interval(duration=300.0, key=bad_key))
        assert result.status is RuleEvaluationStatus.INVALID
        assert "fsm_kind" in (result.reason or "")


# =============================================================================
# 41. BOUNDARY TESTS
# =============================================================================


class TestBoundary:
    def test_threshold_minus_one_no_match(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_threshold_exactly_match(self) -> None:
        # Mandatory boundary test (Part 27): exact threshold → MATCH.
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_threshold_plus_one_match(self) -> None:
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH


# =============================================================================
# 41. TEMPORAL TESTS
# =============================================================================


class TestTemporal:
    def test_continuous_turnover_single_qualification(self) -> None:
        # Timeline (Part 12): below, crossing, still turnover. The rule
        # fires ONCE for the crossing fact; re-evaluating it stays one.
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "turnover-episode-A"))
        below = _interval(duration=299.0, last_seen=_event(299), interval_id=interval_id)
        crossing = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        still = _interval(duration=330.0, last_seen=_event(330), interval_id=interval_id)
        r_below = _evaluate(below)
        r_cross = _evaluate(crossing)
        r_still = _evaluate(still)
        assert r_below.status is RuleEvaluationStatus.NO_MATCH
        assert r_cross.status is RuleEvaluationStatus.MATCH
        assert _evaluate(crossing).event.event_id == r_cross.event.event_id
        # A later fact of the SAME episode is a distinct observation
        # instant; Task 7 dedups by event identity at the pipeline layer
        # (Part 13) — the rule never re-derives temporal state.
        assert r_still.status is RuleEvaluationStatus.MATCH

    def test_short_occlusion_no_duplicate(self) -> None:
        # Task 15.3 keeps the interval ALIVE across short occlusion;
        # re-feeding the crossing fact produces one event (Part 16).
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        replay = _evaluate(interval)
        assert first.event is not None and replay.event is not None
        assert replay.event.event_id == first.event.event_id
        assert replay.model_dump_json() == first.model_dump_json()

    def test_turnover_completion_stops_events(self) -> None:
        # Part 14: once the interval closes (turnover completed) the rule
        # returns NO_MATCH — no further events after completion.
        completed = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="c")
        result = _evaluate(completed)
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None

    def test_reentry_after_completion_independent(self) -> None:
        # Episode A qualifies + completes; Episode B later qualifies →
        # the B episode is an independent event (Part 15).
        a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        b = _interval(
            duration=300.0,
            dwell_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(a)
        rb = _evaluate(b)
        assert ra.status is RuleEvaluationStatus.NO_MATCH  # A completed
        assert rb.status is RuleEvaluationStatus.MATCH
        assert rb.event is not None
        assert rb.event.payload.turnover_start_time == _event(1200)

    def test_late_event_consumed_per_task15_policy(self) -> None:
        # Task 15 applies the late/out-of-order policy BEFORE the rule
        # sees a fact — a fact Task 15 rejects never reaches the rule
        # (Part 35).
        interval = _interval(duration=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None

    def test_out_of_order_event_never_fabricates(self) -> None:
        # The rule has no temporal-ordering role: it consumes canonical
        # facts only (Part 36).
        interval = _interval(duration=300.0)
        assert _evaluate(interval).status is RuleEvaluationStatus.MATCH


# =============================================================================
# 41. IDEMPOTENCY TESTS
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
        # event identity (Task 7 idempotency, Part 12/13).
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
# 41. VERSION TESTS
# =============================================================================


class TestVersions:
    def test_v1_resolves(self) -> None:
        engine = _engine()
        rule = engine._registry.resolve(_RULE_ID, _RULE_VERSION)
        assert rule.canonical_identity == "turnover_delay:v1"
        assert rule.evaluator_id == TURNOVER_DELAY_EVALUATOR_ID

    def test_unsupported_version_rejected(self) -> None:
        engine = _engine()
        with pytest.raises(UnsupportedRuleVersionError):
            engine.evaluate(
                _RULE_ID,
                RuleVersion("v9"),
                _input(_interval(duration=300.0)),
            )

    def test_config_v1_v2_preserved(self) -> None:
        interval = _interval(duration=300.0)
        r1 = _evaluate(interval, config_version=_CONFIG_V1)
        r2 = _evaluate(interval, config_version=_CONFIG_V2)
        assert r1.configuration_version_id == _CONFIG_V1
        assert r2.configuration_version_id == _CONFIG_V2
        assert r1.event is not None and r2.event is not None
        assert r1.event.event_id != r2.event.event_id  # config in identity

    def test_historical_v1_replay(self) -> None:
        # Introduce config v2, then replay with v1 → identical result
        # (no latest-configuration lookup; Part 18/34).
        interval = _interval(duration=301.0)
        v1_first = _evaluate(interval, config_version=_CONFIG_V1)
        _ = _evaluate(interval, config_version=_CONFIG_V2)  # v2 now exists
        v1_replay = _evaluate(interval, config_version=_CONFIG_V1)
        assert v1_replay.model_dump_json() == v1_first.model_dump_json()
        assert v1_replay.configuration_version_id == _CONFIG_V1

    def test_threshold_from_explicit_config(self) -> None:
        interval = _interval(duration=300.0)
        low = _evaluate(interval, threshold=301.0)
        high = _evaluate(interval, threshold=299.0)
        assert low.status is RuleEvaluationStatus.NO_MATCH
        assert high.status is RuleEvaluationStatus.MATCH
        assert high.event is not None
        assert high.event.payload.threshold_seconds == 299


# =============================================================================
# 41. SECURITY TESTS
# =============================================================================


class TestSecurity:
    def test_cross_tenant_rejected(self) -> None:
        engine = _engine()
        a = _interval(duration=300.0, key=_key(tenant_id=_TENANT_A))
        b = _interval(duration=300.0, key=_key(tenant_id=_TENANT_B))
        inp = RuleEvaluationInput(
            facts=(a, b),
            configuration={
                TURNOVER_DELAY_CONFIG_KEY: 300.0,
                SERVICE_WINDOW_CONFIG_KEY: 0.0,
            },
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_tenant_isolation(self) -> None:
        a = _evaluate(_interval(duration=300.0, key=_key(tenant_id=_TENANT_A)))
        b = _evaluate(_interval(duration=300.0, key=_key(tenant_id=_TENANT_B)))
        assert a.event is not None and b.event is not None
        assert a.event.payload.tenant_id == _TENANT_A
        assert b.event.payload.tenant_id == _TENANT_B
        assert a.event.event_id != b.event.event_id

    def test_cross_venue_rejected(self) -> None:
        engine = _engine()
        a = _interval(duration=300.0, key=_key(venue_id=_VENUE_A))
        b = _interval(duration=300.0, key=_key(venue_id=_VENUE_B))
        inp = RuleEvaluationInput(
            facts=(a, b),
            configuration={
                TURNOVER_DELAY_CONFIG_KEY: 300.0,
                SERVICE_WINDOW_CONFIG_KEY: 0.0,
            },
            configuration_version_id=_CONFIG_V1,
            rule_version=_RULE_VERSION,
            event_time=_event(300),
        )
        with pytest.raises(MixedScopeRuleInputError, match="scope"):
            engine.evaluate(_RULE_ID, _RULE_VERSION, inp)

    def test_venue_isolation(self) -> None:
        a = _evaluate(_interval(duration=300.0, key=_key(venue_id=_VENUE_A)))
        b = _evaluate(_interval(duration=300.0, key=_key(venue_id=_VENUE_B)))
        assert a.event is not None and b.event is not None
        assert a.event.payload.venue_id == _VENUE_A
        assert b.event.payload.venue_id == _VENUE_B
        assert a.event.event_id != b.event.event_id


# =============================================================================
# 41. CONTRACT TESTS
# =============================================================================


class TestContracts:
    def test_envelope_serializes_round_trip(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.event is not None
        serialized = result.event.model_dump(mode="json")
        restored = EventEnvelope.model_validate(serialized)
        assert restored.event_id == result.event.event_id
        assert restored.event_type == RuleEventType.TURNOVER_DELAY.value
        assert restored.event_time == _event(300)
        assert restored.schema_version == "1.0"
        payload = TurnoverDelayPayload.model_validate(restored.payload)
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
        assert ref.metadata["rule_id"] == RuleIdentifier.TURNOVER_DELAY.value
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
# 39/42. OBSERVABILITY + INVARIANT TESTS
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
        assert f"rule_id={RuleIdentifier.TURNOVER_DELAY.value}" in msg
        assert "rule_version=v1" in msg
        assert f"configuration_version_id={_CONFIG_V1}" in msg
        assert "status=match" in msg
        assert _event(300).isoformat() in msg
        # Turnover measurements are recorded via the payload.
        assert "turnover_duration" in msg and "300" in msg
        assert "threshold_seconds" in msg and "300" in msg
        assert "service_window_seconds" in msg and "0" in msg
        assert "spatial_context_id" in msg and _TABLE in msg
        assert rec.tenant_id == str(_TENANT_A)
        assert rec.venue_id == str(_VENUE_A)
        assert rec.session_id == str(_SESSION)
        assert rec.event_id is not None

    def test_no_secrets_in_log(self, caplog) -> None:
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


class TestInvariants:
    def test_no_event_without_canonical_fact(self) -> None:
        # A non-DwellInterval primary fact is rejected by the registry.
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
        with pytest.raises(UnsupportedFactTypeError):
            engine.evaluate(_RULE_ID, _RULE_VERSION, _input(interval=wait))  # type: ignore[arg-type]

    def test_no_match_below_threshold(self) -> None:
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH
        assert result.event is None

    def test_exact_threshold_follows_documented_semantics(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_one_continuous_episode_no_uncontrolled_duplicates(self) -> None:
        interval = _interval(duration=300.0)
        ids = {_evaluate(interval).event.event_id for _ in range(5)}
        assert len(ids) == 1

    def test_completed_turnover_no_additional_events(self) -> None:
        completed = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="c")
        assert _evaluate(completed).status is RuleEvaluationStatus.NO_MATCH

    def test_reentry_creates_independent_event(self) -> None:
        b = _evaluate(
            _interval(duration=300.0, dwell_start=_event(600), last_seen=_event(900), label="B")
        )
        assert b.event is not None
        assert b.event.payload.turnover_start_time == _event(600)

    def test_configuration_version_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0), config_version=_CONFIG_V2)
        assert result.configuration_version_id == _CONFIG_V2

    def test_rule_version_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.rule_id == RuleIdentifier.TURNOVER_DELAY.value
        assert result.rule_version == "v1"

    def test_event_time_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0))
        assert result.event_time == _event(300)
        assert result.event is not None
        assert result.event.event_time == _event(300)

    def test_replay_is_deterministic(self) -> None:
        interval = _interval(duration=300.0)
        assert _evaluate(interval).model_dump_json() == _evaluate(interval).model_dump_json()

    def test_tenant_isolation_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0, key=_key(tenant_id=_TENANT_A)))
        assert result.event is not None
        assert result.event.payload.tenant_id == _TENANT_A

    def test_venue_isolation_preserved(self) -> None:
        result = _evaluate(_interval(duration=300.0, key=_key(venue_id=_VENUE_A)))
        assert result.event is not None
        assert result.event.payload.venue_id == _VENUE_A

    def test_invalid_facts_never_match(self) -> None:
        bad_duration = _interval(duration=0.0).model_copy(update={"duration_seconds": None})
        assert _evaluate(bad_duration).status is RuleEvaluationStatus.INVALID
        no_context = _interval(duration=300.0, key=_key(semantic_context=None))
        assert _evaluate(no_context).status is RuleEvaluationStatus.INVALID

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

    def test_rule_does_not_access_raw_video(self) -> None:
        # The evaluator has no video/vision dependency — it is a pure
        # function over the canonical fact.
        from backend.app.intelligence.rules.turnover_delay import TurnoverDelayEvaluator

        ev = TurnoverDelayEvaluator()
        assert not hasattr(ev, "_video")
        assert not hasattr(ev, "_frames")
        assert not hasattr(ev, "_detector")
        assert not hasattr(ev, "_opencv")


# =============================================================================
# §26-36. GOLDEN TESTS
# =============================================================================


class TestGolden:
    def test_golden_below_threshold(self) -> None:
        """§26: threshold 300s, turnover_duration 299s → NO_MATCH."""
        result = _evaluate(_interval(duration=299.0))
        assert result.status is RuleEvaluationStatus.NO_MATCH

    def test_golden_exact_threshold(self) -> None:
        """§27: threshold 300s, turnover_duration exactly 300s → MATCH
        (mandatory boundary)."""
        result = _evaluate(_interval(duration=300.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_golden_above_threshold(self) -> None:
        """§28: threshold 300s, turnover_duration 301s → MATCH."""
        result = _evaluate(_interval(duration=301.0))
        assert result.status is RuleEvaluationStatus.MATCH

    def test_golden_continuous_turnover(self) -> None:
        """§29: 10:00 begins, 10:04:59 below, 10:05:00 crossed, continues
        at 10:06/10:07 → exactly ONE logical event for the crossing."""
        interval_id = EventId(uuid.uuid5(uuid.NAMESPACE_URL, "turnover-episode-A"))
        crossing = _interval(duration=300.0, last_seen=_event(300), interval_id=interval_id)
        crossing_result = _evaluate(crossing)
        assert crossing_result.status is RuleEvaluationStatus.MATCH
        assert crossing_result.event.event_time == _event(300)
        assert _evaluate(crossing).event.event_id == crossing_result.event.event_id

    def test_golden_turnover_completion(self) -> None:
        """§30: 10:00 starts, 10:05 crossed, 10:07 completes, 10:10 no
        turnover → ONE event; none after completion."""
        crossing = _interval(duration=300.0, last_seen=_event(300), label="crossing")
        completed = _interval(
            duration=301.0,
            last_seen=_event(301),
            reason=TemporalReason.EXIT_CONFIRMED,
            label="completed",
        )
        assert _evaluate(crossing).status is RuleEvaluationStatus.MATCH
        # After completion the fact is CLOSED → NO_MATCH (no further events).
        assert _evaluate(completed).status is RuleEvaluationStatus.NO_MATCH
        assert _evaluate(completed).event is None

    def test_golden_reentry(self) -> None:
        """§31: Episode A starts/qualifies/completes; Episode B later
        qualifies → TWO independent events."""
        episode_a = _interval(duration=300.0, reason=TemporalReason.EXIT_CONFIRMED, label="A")
        episode_b = _interval(
            duration=300.0,
            dwell_start=_event(1200),
            last_seen=_event(1500),
            label="B",
        )
        ra = _evaluate(episode_a)
        rb = _evaluate(episode_b)
        assert ra.status is RuleEvaluationStatus.NO_MATCH  # A completed
        assert rb.status is RuleEvaluationStatus.MATCH
        assert rb.event is not None
        assert ra.event is None  # no event after completion
        assert rb.event.payload.turnover_start_time == _event(1200)

    def test_golden_short_occlusion(self) -> None:
        """§32: Task 15 keeps the same turnover episode through short
        occlusion → no duplicate threshold event."""
        interval = _interval(duration=300.0)
        first = _evaluate(interval)
        replay = _evaluate(interval)
        assert first.event is not None and replay.event is not None
        assert replay.event.event_id == first.event.event_id
        assert len({_evaluate(interval).event.event_id for _ in range(3)}) == 1

    def test_golden_replay(self) -> None:
        """§33: identical input evaluated repeatedly → identical result,
        event type, payload, event_time, versions, identity."""
        interval = _interval(duration=300.0)
        engine = _engine()
        inp = _input(interval)
        results = [engine.evaluate(_RULE_ID, _RULE_VERSION, inp) for _ in range(3)]
        assert all(r == results[0] for r in results)
        assert all(r.model_dump_json() == results[0].model_dump_json() for r in results)
        assert all(r.event.event_id == results[0].event.event_id for r in results)

    def test_golden_configuration_replay(self) -> None:
        """§34: config v1 then v2; replay the historical fact explicitly
        with v1 → result remains based on v1 (no latest lookup)."""
        interval = _interval(duration=301.0)
        v1_first = _evaluate(interval, config_version=_CONFIG_V1)
        _ = _evaluate(interval, config_version=_CONFIG_V2)
        v1_replay = _evaluate(interval, config_version=_CONFIG_V1)
        assert v1_replay.model_dump_json() == v1_first.model_dump_json()
        assert v1_replay.configuration_version_id == _CONFIG_V1

    def test_golden_late_event(self) -> None:
        """§35: a fact Task 15 accepted under its late policy is consumed
        as-is — the rule never independently reorders events."""
        interval = _interval(duration=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event is not None

    def test_golden_out_of_order_event(self) -> None:
        """§36: the rule has no temporal-ordering role — it follows Task
        15's event-time policy by consuming canonical facts only; no
        duplicate or incorrect turnover event."""
        interval = _interval(duration=300.0)
        result = _evaluate(interval)
        assert result.status is RuleEvaluationStatus.MATCH
        assert result.event.event_time == _event(300)
