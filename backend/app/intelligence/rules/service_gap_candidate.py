"""Task 16.7 — the ``service_gap_candidate`` operational rule.

Converts an already-established canonical ``WaitingInterval`` (Task
15.5.3) into a deterministic ``service_gap_candidate`` event when the
configured service-gap grace threshold is crossed. The rule identifies a
POTENTIAL operational service gap: a guest confirmed WAITING in a
configured service area for longer than the configured grace period. It
does NOT inspect raw frames, YOLO detections, tracker objects, bounding
boxes, pixels, or video files (Part 1) — it consumes only the confirmed
canonical waiting fact and its canonical spatial context.

Conditions (Part 4/8), every threshold read from the EXPLICIT
configuration snapshot of the pinned ``configuration_version_id``
(declared via ``configuration_requirements`` — never the latest
configuration, never a silent default; Part 6):

    - the fact is a canonical ``WaitingInterval`` (waiting state
      confirmed by Task 15.5.3);
    - the fact is OPEN — ``reason is None`` — i.e. service has NOT
      resumed (Part 15). A closed interval (service resumed / gap
      ended) is NO_MATCH, so the rule never generates further
      service-gap events after resumption;
    - the fact's ``key.semantic_context`` names a CONFIGURED eligible
      service-area profile id (``service_area_ids``) — spatial
      validation is membership against the configured list, never a
      polygon re-computation (Part 17; Task 14 owns geometry);
    - ``gap_duration >= service_gap_grace_seconds`` — the qualification
      boundary (Part 11 / golden §26-28):

        threshold - 1 unit -> NO_MATCH
        threshold exactly  -> MATCH
        threshold + 1 unit -> MATCH

    A missing/invalid threshold, a missing/invalid eligible-area list,
    or a missing/invalid duration is INVALID, never NO_MATCH (Part 10).

Event-time (Part 7): the event uses ``inp.event_time`` which MUST equal
the interval's ``last_seen`` (the canonical current event-time of the
fact — the qualification instant). A mismatch is INVALID, never silently
re-stamped. ``datetime.now()`` / ``time.time()`` are never read. The
rule follows Task 15's late/out-of-order policy by construction: a fact
Task 15 rejected never reaches the rule; a fact Task 15 accepted is
consumed as-is (Part 34/35).

Idempotency (Part 12/13): the event identity is content-derived via
``deterministic_event_id`` (Task 7/15 UUID5 over scope + rule identity +
event type + event time + configuration version), so repeated evaluation
of the same logical crossing reproduces the same logical event identity
— no second deduplication mechanism. Re-entry / new episode (Part 14): a
NEW waiting episode is a NEW ``WaitingInterval`` with a distinct
``interval_id``/``waiting_start``, producing a distinct logical event.

Cooldown: the rule DECLARES its ``CooldownPolicy`` (disabled, per the
Task 16.2 golden fixture); cooldown EXECUTION belongs to a later Task 16
step (the contract's policy-only design) — the deterministic identity
plus the open-interval semantics above are what bound one episode to one
logical event.

The evaluator is registered via ``build_operational_engine`` (the
sanctioned wiring, Part 36) — never instantiated from unrelated modules.
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
    ServiceGapCandidatePayload,
)
from contracts.temporal import TEMPORAL_ENGINE_VERSION, WaitingInterval

__all__ = [
    "SERVICE_AREA_IDS_CONFIG_KEY",
    "SERVICE_GAP_CONFIG_KEY",
    "SERVICE_GAP_EVALUATOR_ID",
    "ServiceGapCandidateEvaluator",
    "service_gap_candidate_definition",
]

# The explicit configuration keys the rule reads (Part 6). The rule NEVER
# hardcodes a threshold or an eligible-area list — only the key names,
# which are the canonical configuration contract for this rule.
SERVICE_GAP_CONFIG_KEY = "service_gap_grace_seconds"
SERVICE_AREA_IDS_CONFIG_KEY = "service_area_ids"

# The canonical evaluator identity for the service_gap_candidate rule v1.
SERVICE_GAP_EVALUATOR_ID = "service_gap_evaluator.v1"


def service_gap_candidate_definition(*, version: str = "v1") -> RuleDefinition:
    """The canonical ``service_gap_candidate`` v1 rule definition (Part 3).

    ``(rule_id, rule_version)`` = ``(service_gap_candidate, v1)`` —
    explicit, immutable, registry-governed. Version handling lives in the
    versioning system (Task 16.2), never hardcoded inside the evaluator.
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.SERVICE_GAP_CANDIDATE.value),
        rule_version=RuleVersion(version),
        rule_name="Service Gap Candidate",
        description=(
            "Converts an OPEN confirmed Task 15.5.3 WaitingInterval in a "
            "configured service area into a service_gap_candidate event "
            "when the configured service-gap grace duration is reached."
        ),
        enabled=True,
        input_fact_types=frozenset({FactType.WAITING_INTERVAL}),
        configuration_requirements=frozenset({
            SERVICE_GAP_CONFIG_KEY,
            SERVICE_AREA_IDS_CONFIG_KEY,
        }),
        evaluator_id=SERVICE_GAP_EVALUATOR_ID,
        output_event_type=RuleEventType.SERVICE_GAP_CANDIDATE,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        cooldown_policy=CooldownPolicy(enabled=False, duration_seconds=0.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


class ServiceGapCandidateEvaluator:
    """Deterministic, side-effect-free evaluator for ``service_gap_candidate``.

    Pure (Part 37/39): no database, Redis, S3, HTTP, LLM, wall clock,
    randomness, frame decoding, tracking, or geometry computation. Reads
    only the explicit ``RuleEvaluationInput``.
    """

    evaluator_id = SERVICE_GAP_EVALUATOR_ID

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        interval = inp.facts[0]
        if not isinstance(interval, WaitingInterval):
            # Defensive — the registry's fact-type boundary already
            # guarantees a WaitingInterval; never fabricate a result.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    "service_gap_candidate requires a WaitingInterval fact, "
                    f"got {type(interval).__name__}"
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
            # current event-time (Part 5/7) — never processing time,
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

        # Part 15 — service resumption: a CLOSED interval means service
        # resumed / the gap ended. The rule MUST NOT keep generating
        # service-gap events after resumption → NO_MATCH.
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

        # Part 17 — spatial validation against the EXPLICIT eligible
        # service-area list (never a polygon re-computation).
        if interval.key.semantic_context is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    "waiting interval carries no service-area context (semantic_context is None)"
                ),
            )
        service_area_ids = self._service_area_ids_of(rule, inp)
        if service_area_ids is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"configuration key {SERVICE_AREA_IDS_CONFIG_KEY!r} must "
                    "be a non-empty list of service-area profile ids"
                ),
            )
        if interval.key.semantic_context not in service_area_ids:
            # A valid waiting fact OUTSIDE the configured service areas is
            # a legitimate NO_MATCH — never silently classified as a
            # service gap (Part 9).
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

        threshold = self._threshold_of(rule, inp)
        if threshold is None:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"configuration key {SERVICE_GAP_CONFIG_KEY!r} must be a "
                    "positive number of seconds"
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
                    f"invalid waiting duration {duration!r} — a service-gap "
                    "qualification requires a non-negative duration"
                ),
            )

        if duration < threshold:
            # Part 9 — below threshold: NO_MATCH, no envelope, no evidence,
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
    def _service_area_ids_of(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> frozenset[str] | None:
        """The EXPLICIT eligible service-area profile ids (Part 17).

        Returns None when the key is missing or not a non-empty
        collection of non-empty strings — the evaluator then returns
        INVALID (never a silent default, never an implicit "all areas").
        ``configuration_requirements`` already guarantees the key exists
        at the registry boundary; this re-validates the VALUE.
        """
        raw = inp.configuration.get(SERVICE_AREA_IDS_CONFIG_KEY)
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
        """Read + validate the EXPLICIT configured threshold (Part 6)."""
        raw = inp.configuration.get(SERVICE_GAP_CONFIG_KEY)
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
        interval: WaitingInterval,
        threshold: float,
    ) -> EventEnvelope[ServiceGapCandidatePayload]:
        """Construct the canonical deterministic EventEnvelope (Part 21)."""
        from backend.app.intelligence.rules.evaluator import deterministic_event_id

        event_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        payload = ServiceGapCandidatePayload(
            interval_id=interval.interval_id,
            tenant_id=interval.key.tenant_id,
            venue_id=interval.key.venue_id,
            session_id=interval.key.session_id,
            camera_id=interval.key.camera_id,
            track_id=interval.key.track_id,
            service_area_id=interval.key.semantic_context,
            gap_start_time=interval.waiting_start,
            qualification_time=inp.event_time,
            gap_duration=interval.duration_seconds or 0.0,
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
