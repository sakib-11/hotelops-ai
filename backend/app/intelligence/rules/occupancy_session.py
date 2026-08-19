"""Task 16.4 — the ``occupancy_session`` operational rule.

The FIRST production operational rule: it converts the canonical Task 15.4
``OccupancySnapshot`` temporal fact into a deterministic
``occupancy_session`` event. The rule NEVER infers occupancy from
detections, bounding boxes, frames, pixels, or tracker objects (Part 1) —
it consumes only the confirmed, stabilized canonical fact produced by the
Task 15.4 occupancy FSM.

Session semantics (Part 2), derived purely from one snapshot's confirmed
counts:

    confirmed count 0 -> >0   = session STARTED  (scope became occupied)
    confirmed count >0 -> 0   = session ENDED    (scope became unoccupied)
    mid-session changes       = NO_MATCH         (no duplicate events)

A snapshot is emitted by Task 15.4 ONLY when the confirmed entity set
changes (a candidate / unconfirmed observation never produces one), so
"no session from a single noisy observation" holds by construction — this
rule converts an already-confirmed boundary into an operational event and
never re-qualifies occupancy (Part 5/6).

Event-time (Part 11): the event uses ``inp.event_time``, which MUST equal
the snapshot's event time (the qualifying temporal fact's instant); a
mismatch is INVALID, never silently resolved. ``datetime.now()`` is never
read.

Determinism / idempotency (Part 9): the event id is content-derived via
``deterministic_event_id`` (Task 7/15 UUID5 strategy over scope + rule
identity + event type + event time + configuration version), so replaying
the same snapshot reproduces the same logical event — no second
deduplication mechanism.

Evidence (Part 10): the rule declares ``evidence_requirement=REQUIRED``;
the engine constructs the deterministic ``EvidenceRef`` REQUEST (the rule
only describes evidence — it never retrieves video).
"""

from __future__ import annotations

from contracts.common import RuleId, RuleVersion
from contracts.events import EventEnvelope
from contracts.rules import (
    CooldownPolicy,
    EvidenceRequirement,
    FactType,
    OccupancySessionPayload,
    OccupancySessionPhase,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
    RuleIdentifier,
)
from contracts.temporal import TEMPORAL_ENGINE_VERSION, OccupancySnapshot

__all__ = [
    "OCCUPANCY_SESSION_EVALUATOR_ID",
    "OccupancySessionEvaluator",
    "occupancy_session_definition",
]

# The canonical evaluator identity for the occupancy_session rule v1.
# Registered in the evaluator registry; referenced by the rule definition.
OCCUPANCY_SESSION_EVALUATOR_ID = "occupancy_session_evaluator.v1"


def occupancy_session_definition(*, version: str = "v1") -> RuleDefinition:
    """The canonical ``occupancy_session`` rule definition (Part 3).

    ``(rule_id, rule_version)`` = ``(occupancy_session, v1)`` — explicit,
    immutable, registry-governed. Version handling lives in the versioning
    system (Task 16.2), never hardcoded inside the evaluator.
    """
    return RuleDefinition(
        rule_id=RuleId(RuleIdentifier.OCCUPANCY_SESSION.value),
        rule_version=RuleVersion(version),
        rule_name="Occupancy Session",
        description=(
            "Converts a confirmed Task 15.4 occupancy boundary into an "
            "occupancy_session started/ended operational event."
        ),
        enabled=True,
        input_fact_types=frozenset({FactType.OCCUPANCY_SNAPSHOT}),
        configuration_requirements=frozenset(),
        evaluator_id=OCCUPANCY_SESSION_EVALUATOR_ID,
        output_event_type=RuleEventType.OCCUPANCY_SESSION,
        evidence_requirement=EvidenceRequirement.REQUIRED,
        cooldown_policy=CooldownPolicy(enabled=False, duration_seconds=0.0),
        deterministic_version=TEMPORAL_ENGINE_VERSION,
    )


class OccupancySessionEvaluator:
    """Deterministic, side-effect-free evaluator for ``occupancy_session``.

    Pure (Part 29): no database, Redis, S3, HTTP, LLM, wall clock, or
    randomness. Reads only the explicit ``RuleEvaluationInput``.
    """

    evaluator_id = OCCUPANCY_SESSION_EVALUATOR_ID

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        snapshot = inp.facts[0]
        if not isinstance(snapshot, OccupancySnapshot):
            # Defensive — the registry's fact-type boundary already
            # guarantees an OccupancySnapshot; never fabricate a result.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"occupancy_session requires an OccupancySnapshot fact, "
                    f"got {type(snapshot).__name__}"
                ),
            )
        if snapshot.key.fsm_kind != "occupancy":
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"occupancy snapshot key fsm_kind must be 'occupancy', "
                    f"got {snapshot.key.fsm_kind!r}"
                ),
            )
        if snapshot.event_time != inp.event_time:
            # The event time MUST be the qualifying fact's event time
            # (Part 5/6/11) — never processing time, never a re-stamp.
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.INVALID,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                reason=(
                    f"input event_time {inp.event_time} does not match the "
                    f"snapshot's event_time {snapshot.event_time}"
                ),
            )

        # Session boundary determination (Part 2) — from the CONFIRMED
        # counts only. Mid-session changes (both counts > 0) and a
        # no-op (0 -> 0, unreachable from Task 15.4) never fire.
        if snapshot.previous_count == 0 and snapshot.occupancy_count > 0:
            phase = OccupancySessionPhase.STARTED
        elif snapshot.previous_count > 0 and snapshot.occupancy_count == 0:
            phase = OccupancySessionPhase.ENDED
        else:
            return RuleEvaluationResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                status=RuleEvaluationStatus.NO_MATCH,
                event_time=inp.event_time,
                configuration_version_id=inp.configuration_version_id,
                tenant_id=snapshot.key.tenant_id,
                venue_id=snapshot.key.venue_id,
                session_id=snapshot.key.session_id,
            )

        event = self._build_event(rule, inp, snapshot, phase)
        return RuleEvaluationResult(
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            status=RuleEvaluationStatus.MATCH,
            event_time=inp.event_time,
            configuration_version_id=inp.configuration_version_id,
            event=event,
            tenant_id=snapshot.key.tenant_id,
            venue_id=snapshot.key.venue_id,
            session_id=snapshot.key.session_id,
        )

    @staticmethod
    def _build_event(
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        snapshot: OccupancySnapshot,
        phase: OccupancySessionPhase,
    ) -> EventEnvelope[OccupancySessionPayload]:
        """Construct the canonical deterministic EventEnvelope (Part 8/9).

        The envelope's ``event_id`` is content-derived via the project's
        canonical strategy (Task 16.3 ``deterministic_event_id``), so the
        same facts + rule version + configuration version always produce
        the same logical event — replay never emits a second event.
        """
        from backend.app.intelligence.rules.evaluator import deterministic_event_id

        event_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        payload = OccupancySessionPayload(
            phase=phase,
            tenant_id=snapshot.key.tenant_id,
            venue_id=snapshot.key.venue_id,
            session_id=snapshot.key.session_id,
            camera_id=snapshot.key.camera_id,
            spatial_context_id=snapshot.key.semantic_context,
            occupancy_count=snapshot.occupancy_count,
            occupied_tracks=snapshot.occupied_tracks,
            occupancy_time=snapshot.event_time,
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
