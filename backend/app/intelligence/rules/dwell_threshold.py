"""Task 16.5 — the ``dwell_threshold`` operational rule.

Converts an already-established canonical ``DwellInterval`` (Task 15.3)
into a deterministic ``dwell_threshold`` event when the configured dwell
threshold is crossed. The rule does NOT calculate tracking, spatial
membership, or temporal dwell state — those belong to Tasks 13-15; it
only converts an already-confirmed canonical fact (Part 1).

Boundary policy (Part 5), with the canonical seconds unit:

    dwell_duration >= configured_threshold   -> MATCH
    dwell_duration <  configured_threshold   -> NO_MATCH

So ``threshold - 1`` is NO_MATCH, ``threshold`` exactly is MATCH, and
``threshold + 1`` is MATCH. The threshold comes from the EXPLICIT
``configuration`` snapshot of the pinned ``configuration_version_id``
(declared via ``configuration_requirements`` — never the latest
configuration, never a silent default; Part 4). A missing/invalid
threshold or a missing/invalid duration is INVALID, never NO_MATCH
(Part 15).

Event-time (Part 6): the event uses ``inp.event_time`` which MUST equal
the interval's ``last_seen`` (the canonical current/event-time of the
fact — the crossing instant). A mismatch is INVALID, never silently
re-stamped. ``datetime.now()`` / ``time.time()`` are never read.

One logical crossing -> one logical event (Part 7/8): the event identity
is content-derived via ``deterministic_event_id`` (Task 7/15 UUID5 over
scope + rule identity + event type + event time + configuration version),
so repeated evaluation of the same crossing reproduces the same logical
event identity — no second deduplication mechanism.

Re-entry (Part 9): a NEW dwell session is a NEW ``DwellInterval`` with a
distinct ``interval_id``/``dwell_start``, producing a distinct logical
event. Independent sessions are never merged.
"""

from __future__ import annotations

from contracts.common import RuleId, RuleVersion
from contracts.events import EventEnvelope
from contracts.rules import (
    CooldownPolicy,
    DwellThresholdPayload,
    EvidenceRequirement,
    FactType,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import TEMPORAL_ENGINE_VERSION, DwellInterval

__all__ = [
    "DWELL_THRESHOLD_CONFIG_KEY",
    "DWELL_THRESHOLD_EVALUATOR_ID",
    "DwellThresholdEvaluator",
    "dwell_threshold_definition",
]

# The explicit configuration key the threshold is read from (Part 4). The
# rule NEVER hardcodes a threshold value — only the key name, which is the
# canonical configuration contract for this rule.
DWELL_THRESHOLD_CONFIG_KEY = "dwell_threshold_seconds"

# The canonical evaluator identity for the dwell_threshold rule v1.
DWELL_THRESHOLD_EVALUATOR_ID = "dwell_threshold_evaluator.v1"


def dwell_threshold_definition(*, version: str = "v1") -> RuleDefinition:
    """The canonical ``dwell_threshold`` rule definition (Part 2).

    ``(rule_id, rule_version)`` = ``(dwell_threshold, v1)`` — explicit,
    immutable, registry-governed. Version handling lives in the versioning
    system (Task 16.2), never hardcoded inside the evaluator.
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.DWELL_THRESHOLD.value),
        rule_version=RuleVersion(version),
        rule_name="Dwell Threshold",
        description=(
            "Converts a canonical Task 15.3 DwellInterval into a "
            "dwell_threshold event when the configured dwell duration "
            "threshold is crossed."
        ),
        enabled=True,
        input_fact_types=frozenset({FactType.DWELL_INTERVAL}),
        configuration_requirements=frozenset({DWELL_THRESHOLD_CONFIG_KEY}),
        evaluator_id=DWELL_THRESHOLD_EVALUATOR_ID,
        output_event_type=RuleEventType.DWELL_THRESHOLD,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        cooldown_policy=CooldownPolicy(enabled=False, duration_seconds=0.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


class DwellThresholdEvaluator:
    """Deterministic, side-effect-free evaluator for ``dwell_threshold``.

    Pure (Part 28): no database, Redis, S3, HTTP, LLM, wall clock, or
    randomness. Reads only the explicit ``RuleEvaluationInput``.
    """

    evaluator_id = DWELL_THRESHOLD_EVALUATOR_ID

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        interval = inp.facts[0]
        if not isinstance(interval, DwellInterval):
            # Defensive — the registry's fact-type boundary already
            # guarantees a DwellInterval; never fabricate a result.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"dwell_threshold requires a DwellInterval fact, got {type(interval).__name__}"
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
            # event-time (Part 3/6) — never processing time, never a
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

        threshold = self._threshold_of(rule, inp)
        if threshold is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"configuration key {DWELL_THRESHOLD_CONFIG_KEY!r} must "
                    "be a positive number of seconds"
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
                    f"invalid dwell duration {duration!r} — a threshold "
                    "crossing requires a non-negative duration"
                ),
            )

        if duration < threshold:
            # Part 10 — below threshold: NO_MATCH, no envelope, no evidence,
            # no state mutation.
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

        event = self._build_event(rule, inp, interval, threshold)
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
    def _threshold_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> float | None:
        """Read + validate the EXPLICIT configured threshold (Part 4).

        Returns None when the key is missing or not a positive number —
        the evaluator then returns INVALID (never a silent default).
        ``configuration_requirements`` already guarantees the key exists
        at the registry boundary; this re-validates the VALUE.
        """
        raw = inp.configuration.get(DWELL_THRESHOLD_CONFIG_KEY)
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
        threshold: float,
    ) -> EventEnvelope[DwellThresholdPayload]:
        """Construct the canonical deterministic EventEnvelope (Part 11)."""
        from backend.app.intelligence.rules.evaluator import deterministic_event_id

        event_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        payload = DwellThresholdPayload(
            interval_id=interval.interval_id,
            tenant_id=interval.key.tenant_id,
            venue_id=interval.key.venue_id,
            session_id=interval.key.session_id,
            camera_id=interval.key.camera_id,
            spatial_context_id=interval.key.semantic_context,
            dwell_start_time=interval.dwell_start,
            threshold_crossing_time=inp.event_time,
            dwell_duration=interval.duration_seconds or 0.0,
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
