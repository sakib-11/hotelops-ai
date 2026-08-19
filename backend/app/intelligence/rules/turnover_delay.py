"""Task 16.8 — the ``turnover_delay`` operational rule.

Converts an OPEN canonical ``DwellInterval`` (Task 15.3) into a
deterministic ``turnover_delay`` event when the configured turnover
window has been exceeded. The rule does NOT determine when turnover
starts — Task 15 owns the temporal state; the rule only evaluates the
canonical turnover fact (Part 4). It never inspects raw video, frames,
YOLO detections, tracker objects, bounding boxes, or OpenCV objects
(Part 1).

Conditions (Part 4/9), every threshold read from the EXPLICIT
configuration snapshot of the pinned ``configuration_version_id``
(declared via ``configuration_requirements`` — never the latest
configuration, never a silent default; Part 6):

    - the primary fact is a canonical ``DwellInterval`` (a table /
      service area / operational zone confirmed occupied — the
      turnover episode);
    - the episode is OPEN — ``reason is None`` — i.e. turnover has NOT
      completed (Part 14). A closed interval (turnover completed) is
      NO_MATCH, so the rule never generates further turnover-delay
      events after completion;
    - the fact's ``key.semantic_context`` is present (the canonical
      Task 14 spatial identity — table/service-area/zone profile id);
      spatial provenance is preserved, never re-computed (Part 17);
    - an ``OccupancySnapshot`` (a declared input per the Task 16.2
      golden fixture), when present, must confirm the space is still
      occupied (``occupancy_count > 0``) — an empty space is a
      completed turnover, never a delay;
    - ``turnover_duration >= service_window_seconds + turnover_delay_seconds``
      — the qualification boundary (Part 7 / golden §26-28). The event
      fires when the table stays occupied BEYOND the configured service
      window by at least the configured turnover-delay threshold:

        threshold - 1 unit -> NO_MATCH
        threshold exactly  -> MATCH
        threshold + 1 unit -> MATCH

    A missing/invalid threshold, service window, or duration is INVALID,
    never NO_MATCH (Part 11).

Event-time (Part 8): the event uses ``inp.event_time`` which MUST equal
the interval's ``last_seen`` (the canonical current event-time of the
fact — the crossing instant). A mismatch is INVALID, never silently
re-stamped. ``datetime.now()`` / ``time.time()`` are never read. The
rule follows Task 15's late/out-of-order policy by construction: a fact
Task 15 rejected never reaches the rule (Part 35/36).

Idempotency (Part 12/13): the event identity is content-derived via
``deterministic_event_id`` (Task 7/15 UUID5 over scope + rule identity +
event type + event time + configuration version), so repeated evaluation
of the same logical crossing reproduces the same logical event identity
— no second deduplication mechanism. Re-entry / new turnover (Part 15):
a NEW turnover episode is a NEW ``DwellInterval`` with a distinct
``interval_id``/``dwell_start``, producing a distinct logical event.

Cooldown: the rule DECLARES its ``CooldownPolicy`` (enabled, 300s, per
the Task 16.2 golden fixture); cooldown EXECUTION belongs to a later
Task 16 step (the contract's policy-only design) — the deterministic
identity plus the open-interval semantics above bound one episode to one
logical event.

The evaluator is registered via ``build_operational_engine`` (the
sanctioned wiring, Part 37) — never instantiated from unrelated modules.
"""

from __future__ import annotations

from contracts.common import RuleId, RuleVersion
from contracts.events import EventEnvelope
from contracts.rules import (
    CooldownPolicy,
    EvidenceRequirement,
    FactType,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
    TurnoverDelayPayload,
)
from contracts.temporal import TEMPORAL_ENGINE_VERSION, DwellInterval, OccupancySnapshot

__all__ = [
    "SERVICE_WINDOW_CONFIG_KEY",
    "TURNOVER_DELAY_CONFIG_KEY",
    "TURNOVER_DELAY_EVALUATOR_ID",
    "TurnoverDelayEvaluator",
    "turnover_delay_definition",
]

# The explicit configuration keys the rule reads (Part 6). The rule NEVER
# hardcodes a threshold — only the key names, which are the canonical
# configuration contract for this rule.
TURNOVER_DELAY_CONFIG_KEY = "turnover_delay_seconds"
SERVICE_WINDOW_CONFIG_KEY = "service_window_seconds"

# The canonical evaluator identity for the turnover_delay rule v1.
TURNOVER_DELAY_EVALUATOR_ID = "turnover_delay_evaluator.v1"


def turnover_delay_definition(*, version: str = "v1") -> RuleDefinition:
    """The canonical ``turnover_delay`` v1 rule definition (Part 3).

    ``(rule_id, rule_version)`` = ``(turnover_delay, v1)`` — explicit,
    immutable, registry-governed. Version handling lives in the versioning
    system (Task 16.2), never hardcoded inside the evaluator.
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.TURNOVER_DELAY.value),
        rule_version=RuleVersion(version),
        rule_name="Turnover Delay",
        description=(
            "Converts an OPEN confirmed Task 15.3 DwellInterval into a "
            "turnover_delay event when a table/service area stays occupied "
            "beyond the configured service window + turnover-delay window."
        ),
        enabled=True,
        input_fact_types=frozenset({
            FactType.DWELL_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
        }),
        configuration_requirements=frozenset({
            TURNOVER_DELAY_CONFIG_KEY,
            SERVICE_WINDOW_CONFIG_KEY,
        }),
        evaluator_id=TURNOVER_DELAY_EVALUATOR_ID,
        output_event_type=RuleEventType.TURNOVER_DELAY,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        cooldown_policy=CooldownPolicy(enabled=True, duration_seconds=300.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


class TurnoverDelayEvaluator:
    """Deterministic, side-effect-free evaluator for ``turnover_delay``.

    Pure (Part 38/40): no database, Redis, S3, HTTP, LLM, wall clock,
    randomness, frame decoding, tracking, or geometry computation. Reads
    only the explicit ``RuleEvaluationInput``.
    """

    evaluator_id = TURNOVER_DELAY_EVALUATOR_ID

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        interval = inp.facts[0]
        if not isinstance(interval, DwellInterval):
            # Defensive — the registry's fact-type boundary already
            # guarantees a DwellInterval primary fact; never fabricate.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    "turnover_delay requires a DwellInterval as the primary "
                    f"fact, got {type(interval).__name__}"
                ),
            )
        if interval.key.fsm_kind != "dwell":
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"dwell interval key fsm_kind must be 'dwell', got {interval.key.fsm_kind!r}"
                ),
            )
        if interval.last_seen != inp.event_time:
            # The crossing instant MUST be the fact's canonical current
            # event-time (Part 5/8) — never processing time, never a
            # re-stamp.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"input event_time {inp.event_time} does not match the "
                    f"interval's last_seen {interval.last_seen}"
                ),
            )

        # Part 14 — turnover completion: a CLOSED interval means turnover
        # completed. The rule MUST NOT keep generating events after
        # completion → NO_MATCH.
        if interval.reason is not None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.NO_MATCH,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                tenant_id=interval.key.tenant_id,
                venue_id=interval.key.venue_id,
                session_id=interval.key.session_id,
            )

        # Part 17 — spatial provenance: the canonical Task 14 spatial
        # identity must be present (never re-computed).
        if interval.key.semantic_context is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=("dwell interval carries no spatial context (semantic_context is None)"),
            )

        # An OccupancySnapshot (declared input, when present) must confirm
        # the space is still occupied — an empty space is a completed
        # turnover, never a delay (Part 10: turnover state is not active).
        if not self._space_occupied(inp):
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.NO_MATCH,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                tenant_id=interval.key.tenant_id,
                venue_id=interval.key.venue_id,
                session_id=interval.key.session_id,
            )

        service_window = self._service_window_of(rule, inp)
        threshold = self._threshold_of(rule, inp)
        if service_window is None or threshold is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"configuration keys {SERVICE_WINDOW_CONFIG_KEY!r} and "
                    f"{TURNOVER_DELAY_CONFIG_KEY!r} must be non-negative "
                    "numbers of seconds (threshold > 0)"
                ),
            )

        duration = interval.duration_seconds
        if duration is None or duration < 0:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"invalid dwell duration {duration!r} — a turnover "
                    "qualification requires a non-negative duration"
                ),
            )

        effective_threshold = service_window + threshold
        if duration < effective_threshold:
            # Part 10 — below threshold: NO_MATCH, no envelope, no
            # evidence, no state mutation.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.NO_MATCH,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                tenant_id=interval.key.tenant_id,
                venue_id=interval.key.venue_id,
                session_id=interval.key.session_id,
            )

        event = self._build_event(
            rule, inp, interval, service_window, threshold, effective_threshold
        )
        return RuleEvaluationResult(
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            status=RuleEvaluationStatus.MATCH,
            event_time=inp.event_time,
            configuration_version_id=inp.configuration_version_id,
            event=event,
            tenant_id=interval.key.tenant_id,
            venue_id=interval.key.venue_id,
            session_id=interval.key.session_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _space_occupied(inp: RuleEvaluationInput) -> bool:
        """Whether the turnover space is confirmed occupied.

        An ``OccupancySnapshot`` is a DECLARED input (Task 16.2 golden
        fixture). When one is present, the confirmed occupancy count must
        be > 0 for the turnover episode to be active; when absent, the
        OPEN DwellInterval alone establishes the active episode.
        """
        for fact in inp.facts:
            if isinstance(fact, OccupancySnapshot):
                return fact.occupancy_count > 0
        return True

    @staticmethod
    def _service_window_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> float | None:
        """Read + validate the EXPLICIT configured service window (Part 6).

        Returns None when missing or not a non-negative number — the
        evaluator then returns INVALID (never a silent default).
        """
        raw = inp.configuration.get(SERVICE_WINDOW_CONFIG_KEY)
        if raw is None:
            return None
        try:
            value = float(raw)
        except TypeError, ValueError:
            return None
        if value < 0:
            return None
        return value

    @staticmethod
    def _threshold_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> float | None:
        """Read + validate the EXPLICIT configured turnover-delay threshold."""
        raw = inp.configuration.get(TURNOVER_DELAY_CONFIG_KEY)
        if raw is None:
            return None
        try:
            value = float(raw)
        except TypeError, ValueError:
            return None
        if value <= 0:
            return None
        return value

    @staticmethod
    def _build_event(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        interval: DwellInterval,
        service_window: float,
        threshold: float,
        effective_threshold: float,
    ) -> EventEnvelope[TurnoverDelayPayload]:
        """Construct the canonical deterministic EventEnvelope (Part 21)."""
        from backend.app.intelligence.rules.evaluator import deterministic_event_id

        event_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        payload = TurnoverDelayPayload(
            interval_id=interval.interval_id,
            tenant_id=interval.key.tenant_id,
            venue_id=interval.key.venue_id,
            session_id=interval.key.session_id,
            camera_id=interval.key.camera_id,
            track_id=interval.key.track_id,
            spatial_context_id=interval.key.semantic_context,
            turnover_start_time=interval.dwell_start,
            threshold_crossing_time=inp.event_time,
            turnover_duration=interval.duration_seconds or 0.0,
            service_window_seconds=service_window,
            threshold_seconds=threshold,
            configuration_version_id=inp.configuration_version_id,
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
        )
        return EventEnvelope(
            event_id=event_id,
            event_type=rule.output_event_type.value,
            event_time=inp.event_time,
            produced_at=inp.processing_time or inp.event_time,
            source=f"rule:{rule.canonical_identity}",
            payload=payload,
        )
