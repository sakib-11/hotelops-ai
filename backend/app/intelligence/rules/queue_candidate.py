"""Task 16.6 — the ``queue_candidate`` operational rule.

Converts an already-established canonical ``WaitingInterval`` (Task
15.5.3) into a deterministic ``queue_candidate`` event when the
configured queue-candidate conditions are satisfied. The rule does NOT
determine queue membership from YOLO detections, bounding boxes, raw
frames, tracker objects, or pixels (Part 1) — it consumes only the
confirmed canonical waiting fact and its canonical spatial context.

Conditions (Part 4/5/16), every threshold read from the EXPLICIT
configuration snapshot of the pinned ``configuration_version_id``
(declared via ``configuration_requirements`` — never the latest
configuration, never a silent default; Part 5):

    - the fact is a canonical ``WaitingInterval`` (waiting state
      confirmed by Task 15.5.3);
    - the fact's ``key.semantic_context`` names a CONFIGURED eligible
      queue/service-area profile id (``queue_area_ids``) — spatial
      validation is membership against the configured list, never a
      polygon re-computation (Part 16; Task 14 owns geometry);
    - ``waiting_duration >= waiting_qualification_seconds`` — the
      qualification boundary (Part 20/21/22):

        threshold - 1 unit -> NO_MATCH
        threshold exactly  -> MATCH
        threshold + 1 unit -> MATCH

    A missing/invalid threshold, a missing/invalid eligible-area list,
    or a missing/invalid duration is INVALID, never NO_MATCH (Part 9).

Event-time (Part 6): the event uses ``inp.event_time`` which MUST equal
the interval's ``last_seen`` (the canonical current event-time of the
fact — the qualification instant). A mismatch is INVALID, never silently
re-stamped. ``datetime.now()`` / ``time.time()`` are never read.

Idempotency (Part 12): the event identity is content-derived via
``deterministic_event_id`` (Task 7/15 UUID5 over scope + rule identity +
event type + event time + configuration version), so repeated evaluation
of the same logical qualification reproduces the same logical event
identity — no second deduplication mechanism. Re-entry (Part 14): a NEW
waiting episode is a NEW ``WaitingInterval`` with a distinct
``interval_id``/``waiting_start``, producing a distinct logical event.

Cooldown (Part 13): the rule DECLARES its ``CooldownPolicy`` (v1: 60s,
per the Task 16.2 golden fixture); cooldown EXECUTION belongs to a later
Task 16 step (the contract's policy-only design) — the deterministic
identity above is what makes one logical qualification one logical event.

v2 (Part 19): ``queue_candidate:v2`` is a DISTINCT immutable definition
registered alongside v1 (golden fixture shape: additionally accepts
``occupancy_snapshot`` facts and requires the ``queue_max_length``
configuration key — the "queue-capacity configuration requirement").
Its evaluator keeps v1's deterministic qualification semantics and
additionally requires ``queue_max_length`` to be a positive number
(INVALID otherwise). Capacity-alert business semantics are NOT invented
here — they belong to later Task 16 steps.
"""

from __future__ import annotations

from contracts.common import RuleId, RuleVersion
from contracts.events import EventEnvelope
from contracts.rules import (
    CooldownPolicy,
    EvidenceRequirement,
    FactType,
    QueueCandidatePayload,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import TEMPORAL_ENGINE_VERSION, WaitingInterval

__all__ = [
    "QUEUE_AREA_IDS_CONFIG_KEY",
    "QUEUE_CANDIDATE_CONFIG_KEY",
    "QUEUE_CANDIDATE_EVALUATOR_ID",
    "QUEUE_CANDIDATE_EVALUATOR_V2_ID",
    "QUEUE_MAX_LENGTH_CONFIG_KEY",
    "QueueCandidateEvaluator",
    "QueueCandidateEvaluatorV2",
    "queue_candidate_definition",
    "queue_candidate_definition_v2",
]

# The explicit configuration keys the rule reads (Part 5). The rule NEVER
# hardcodes a threshold or an eligible-area list — only the key names,
# which are the canonical configuration contract for this rule.
QUEUE_CANDIDATE_CONFIG_KEY = "waiting_qualification_seconds"
QUEUE_AREA_IDS_CONFIG_KEY = "queue_area_ids"
QUEUE_MAX_LENGTH_CONFIG_KEY = "queue_max_length"

# Canonical evaluator identities (v1 + v2) — registered in the evaluator
# registry; referenced by the rule definitions.
QUEUE_CANDIDATE_EVALUATOR_ID = "queue_candidate_evaluator.v1"
QUEUE_CANDIDATE_EVALUATOR_V2_ID = "queue_candidate_evaluator.v2"


def queue_candidate_definition(*, version: str = "v1") -> RuleDefinition:
    """The canonical ``queue_candidate`` v1 rule definition (Part 2).

    ``(rule_id, rule_version)`` = ``(queue_candidate, v1)`` — explicit,
    immutable, registry-governed. Version handling lives in the versioning
    system (Task 16.2), never hardcoded inside the evaluator.
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
        rule_version=RuleVersion(version),
        rule_name="Queue Candidate",
        description=(
            "Converts a confirmed Task 15.5.3 WaitingInterval in a configured "
            "queue/service area into a queue_candidate event when the "
            "configured minimum waiting duration is reached."
        ),
        enabled=True,
        input_fact_types=frozenset({FactType.WAITING_INTERVAL}),
        configuration_requirements=frozenset({
            QUEUE_CANDIDATE_CONFIG_KEY,
            QUEUE_AREA_IDS_CONFIG_KEY,
        }),
        evaluator_id=QUEUE_CANDIDATE_EVALUATOR_ID,
        output_event_type=RuleEventType.QUEUE_CANDIDATE,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        cooldown_policy=CooldownPolicy(enabled=True, duration_seconds=60.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


def queue_candidate_definition_v2() -> RuleDefinition:
    """The canonical ``queue_candidate`` v2 rule definition (Part 19).

    A DISTINCT immutable version registered alongside v1 — registering v2
    never changes historical v1 evaluation. Per the Task 16.2 golden
    fixture, v2 additionally accepts ``occupancy_snapshot`` facts and
    requires the ``queue_max_length`` configuration key (the
    queue-capacity configuration requirement). Its evaluator validates
    that configuration; capacity-alert business semantics are NOT
    implemented here (later Task 16 step).
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.QUEUE_CANDIDATE.value),
        rule_version=RuleVersion("v2"),
        rule_name="Queue Candidate",
        description=(
            "Queue-candidate semantics v2: same deterministic waiting "
            "qualification as v1 plus the queue_max_length configuration "
            "requirement (queue-capacity configuration contract)."
        ),
        enabled=True,
        input_fact_types=frozenset({
            FactType.WAITING_INTERVAL,
            FactType.OCCUPANCY_SNAPSHOT,
        }),
        configuration_requirements=frozenset({
            QUEUE_CANDIDATE_CONFIG_KEY,
            QUEUE_AREA_IDS_CONFIG_KEY,
            QUEUE_MAX_LENGTH_CONFIG_KEY,
        }),
        evaluator_id=QUEUE_CANDIDATE_EVALUATOR_V2_ID,
        output_event_type=RuleEventType.QUEUE_CANDIDATE,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        cooldown_policy=CooldownPolicy(enabled=True, duration_seconds=120.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


class QueueCandidateEvaluator:
    """Deterministic, side-effect-free evaluator for ``queue_candidate`` v1.

    Pure (Part 30): no database, Redis, S3, HTTP, LLM, wall clock, or
    randomness. Reads only the explicit ``RuleEvaluationInput``.
    """

    evaluator_id = QUEUE_CANDIDATE_EVALUATOR_ID

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        interval = self._waiting_interval_of(rule, inp)
        if interval is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    "queue_candidate requires a WaitingInterval as the primary "
                    f"fact, got {type(inp.facts[0]).__name__} as facts[0]"
                ),
            )
        if interval.key.fsm_kind != "waiting":
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"waiting interval key fsm_kind must be 'waiting', got "
                    f"{interval.key.fsm_kind!r}"
                ),
            )
        if interval.last_seen != inp.event_time:
            # The qualification instant MUST be the fact's canonical
            # current event-time (Part 3/6) — never processing time,
            # never a re-stamp.
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

        # Part 16 — spatial validation against the EXPLICIT eligible
        # queue/service-area list (never a polygon re-computation).
        if interval.key.semantic_context is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    "waiting interval carries no queue/service-area "
                    "context (semantic_context is None)"
                ),
            )
        queue_area_ids = self._queue_area_ids_of(rule, inp)
        if queue_area_ids is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"configuration key {QUEUE_AREA_IDS_CONFIG_KEY!r} must "
                    "be a non-empty list of queue/service-area profile ids"
                ),
            )
        if interval.key.semantic_context not in queue_area_ids:
            # A valid waiting fact OUTSIDE the configured queue/service
            # areas is a legitimate NO_MATCH — never silently classified
            # as a queue (Part 26).
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

        extra_reason = self._extra_configuration_invalid(rule, inp)
        if extra_reason is not None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=extra_reason,
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
                    f"configuration key {QUEUE_CANDIDATE_CONFIG_KEY!r} must "
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
                    f"invalid waiting duration {duration!r} — a queue "
                    "qualification requires a non-negative duration"
                ),
            )

        if duration < threshold:
            # Part 8 — below qualification: NO_MATCH, no envelope, no
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
    def _waiting_interval_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> WaitingInterval | None:
        """The canonical WaitingInterval — the PRIMARY fact (``facts[0]``).

        The engine derives the deterministic event identity and the
        tenant/venue/session scope from ``inp.facts[0].key`` (Task 16.3
        Part 12), so the waiting interval MUST be the primary fact for
        both v1 and v2 — fact ORDER is part of the deterministic
        contract. v2 may carry additional declared facts (an
        occupancy_snapshot) AFTER the waiting interval; those are
        accepted but do not change the decision. None when facts[0] is
        not a WaitingInterval.
        """
        fact = inp.facts[0]
        if isinstance(fact, WaitingInterval):
            return fact
        return None

    @staticmethod
    def _queue_area_ids_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> frozenset[str] | None:
        """The EXPLICIT eligible queue/service-area profile ids (Part 16).

        Returns None when the key is missing or not a non-empty
        collection of non-empty strings — the evaluator then returns
        INVALID (never a silent default, never an implicit "all areas").
        ``configuration_requirements`` already guarantees the key exists
        at the registry boundary; this re-validates the VALUE.
        """
        raw = inp.configuration.get(QUEUE_AREA_IDS_CONFIG_KEY)
        if raw is None:
            return None
        try:
            values = list(raw)
        except TypeError:
            return None
        if not values:
            return None
        cleaned: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                return None
            cleaned.add(value)
        return frozenset(cleaned)

    @staticmethod
    def _threshold_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> float | None:
        """Read + validate the EXPLICIT configured threshold (Part 5)."""
        raw = inp.configuration.get(QUEUE_CANDIDATE_CONFIG_KEY)
        if raw is None:
            return None
        try:
            value = float(raw)
        except TypeError, ValueError:
            return None
        if value <= 0:
            return None
        return value

    def _extra_configuration_invalid(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> str | None:
        """v1 has no additional configuration; None = valid (Part 5)."""
        return None

    @staticmethod
    def _build_event(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        interval: WaitingInterval,
        threshold: float,
    ) -> EventEnvelope[QueueCandidatePayload]:
        """Construct the canonical deterministic EventEnvelope (Part 7)."""
        from backend.app.intelligence.rules.evaluator import deterministic_event_id

        event_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        payload = QueueCandidatePayload(
            interval_id=interval.interval_id,
            tenant_id=interval.key.tenant_id,
            venue_id=interval.key.venue_id,
            session_id=interval.key.session_id,
            camera_id=interval.key.camera_id,
            track_id=interval.key.track_id,
            spatial_context_id=interval.key.semantic_context,
            waiting_start_time=interval.waiting_start,
            qualification_time=inp.event_time,
            waiting_duration=interval.duration_seconds or 0.0,
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


class QueueCandidateEvaluatorV2(QueueCandidateEvaluator):
    """``queue_candidate`` v2 — v1 semantics + the queue-capacity
    configuration requirement (Part 19 / golden fixture v2).

    Same deterministic qualification as v1; additionally requires the
    ``queue_max_length`` configuration key to be a positive number
    (INVALID otherwise). No capacity-alert business logic is invented
    here — the v2 delta is the additional configuration contract.
    """

    evaluator_id = QUEUE_CANDIDATE_EVALUATOR_V2_ID

    def _extra_configuration_invalid(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> str | None:
        raw = inp.configuration.get(QUEUE_MAX_LENGTH_CONFIG_KEY)
        if raw is None:
            return (
                f"configuration key {QUEUE_MAX_LENGTH_CONFIG_KEY!r} must be "
                "a positive number (queue-capacity configuration requirement)"
            )
        try:
            value = float(raw)
        except TypeError, ValueError:
            return (
                f"configuration key {QUEUE_MAX_LENGTH_CONFIG_KEY!r} must be "
                "a positive number (queue-capacity configuration requirement)"
            )
        if value <= 0:
            return (
                f"configuration key {QUEUE_MAX_LENGTH_CONFIG_KEY!r} must be "
                "a positive number (queue-capacity configuration requirement)"
            )
        return None
