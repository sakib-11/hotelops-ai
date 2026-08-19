"""Deterministic rule evaluation engine (Task 16.3).

Orchestrates one rule evaluation end-to-end:

    Canonical Temporal Facts
        ↓ RuleEvaluationInput
    RuleRegistry → Versioned RuleDefinition (explicit rule_id + rule_version)
        ↓ RuleEvaluatorRegistry → Deterministic Evaluator
    RuleEvaluationResult (NO_MATCH / MATCH / SUPPRESSED / INVALID)
        ↓
    EventEnvelope / EvidenceRef request (Task 4, reused)

Pipeline (Task 16.3 Part 14), every stage explicit:

    1. validate evaluation request        — canonical RuleEvaluationInput
    2. resolve rule definition            — explicit rule_id + rule_version
    3. validate rule version              — the resolved version, never \"latest\"
    4. validate input facts               — fact types, canonical boundary
    5. validate configuration             — required keys + pinned version
    6. resolve evaluator                  — by rule.evaluator_id
    7. execute evaluator                  — pure call, exception-safe
    8. validate evaluator result          — identity/provenance/determinism
    9. construct deterministic metadata   — canonical event identity
    10. construct evidence request        — when the rule requires evidence
    11. record structured telemetry       — Task 8 observability (Part 27)
    12. return RuleEvaluationResult

The engine is PURE (Part 13): it never persists events, publishes
messages, or writes storage — it returns a deterministic result and the
surrounding application layer performs any I/O. It reads no wall clock
and samples no randomness: event identities are content-derived
(Task 7/Task 15 UUID5 strategy), and an evaluator that emits a
non-deterministic identity is REJECTED (Part 15 — never emit an event
from an invalid or non-deterministic result). The one exception is
observability: after the deterministic pipeline completes, step 11 emits
a structured Task 8 log record describing the evaluation — purely
observational, never influencing the returned result.
"""

from __future__ import annotations

import logging

from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_OCCUPANCY_EVENTS,
    record_pipeline_metric,
)
from backend.app.intelligence.rules.evaluator import (
    RuleEvaluator,
    RuleEvaluatorRegistry,
    deterministic_event_id,
    deterministic_evidence_ref,
)
from backend.app.intelligence.rules.exceptions import (
    InvalidRuleEvaluationError,
    InvalidRuleInputError,
    RuleError,
    RuleEvaluationExecutionError,
)
from backend.app.intelligence.rules.registry import RuleRegistry
from contracts.common import RuleId, RuleVersion, TenantId, VenueId, VideoSessionId
from contracts.events import EvidenceRef
from contracts.rules import (
    EvidenceRequirement,
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleEventType,
)

logger = logging.getLogger(__name__)

__all__ = ["RuleEvaluationEngine"]


class RuleEvaluationEngine:
    """The deterministic rule evaluation pipeline.

    Composes a ``RuleRegistry`` (definitions) with a
    ``RuleEvaluatorRegistry`` (implementations). Every evaluation resolves
    an EXPLICIT ``(rule_id, rule_version)`` — there is no \"latest rule\"
    path (Part 4); a missing rule or version raises the existing typed
    errors.
    """

    def __init__(
        self,
        registry: RuleRegistry,
        evaluators: RuleEvaluatorRegistry,
    ) -> None:
        self._registry = registry
        self._evaluators = evaluators

    # ------------------------------------------------------------------
    # Public entry point (the full Part 14 pipeline)
    # ------------------------------------------------------------------

    def evaluate(
        self,
        rule_id: RuleId,
        rule_version: RuleVersion,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        """Evaluate one explicit rule version against canonical facts.

        Args:
            rule_id: the exact rule identity (never \"latest\").
            rule_version: the exact immutable version to evaluate.
            inp: the canonical, fully-versioned evaluation input.

        Returns:
            A typed ``RuleEvaluationResult`` (NO_MATCH / MATCH /
            SUPPRESSED / INVALID) that carries, for MATCH, the canonical
            ``EventEnvelope`` and any required ``EvidenceRef`` requests.

        Raises:
            InvalidRuleInputError: the request is not a canonical input.
            UnknownRuleError: the rule_id is not registered.
            UnsupportedRuleVersionError: the version is not registered.
            UnsupportedEvaluatorError: the rule's evaluator is unavailable.
            MissingRuleConfigurationError: required config keys absent.
            UnsupportedFactTypeError: a fact type is not declared.
            MixedScopeRuleInputError: facts span tenant/venue/session.
            RuleConfigurationMismatchError: input version ≠ rule version.
            RuleEvaluationExecutionError: the evaluator raised unexpectedly.
            InvalidRuleEvaluationError: the evaluator returned a result
                violating the deterministic contract (never returned to
                the caller — evaluation fails explicitly).
        """
        # 1. validate evaluation request
        if not isinstance(inp, RuleEvaluationInput):
            raise InvalidRuleInputError(
                f"evaluation request must be a RuleEvaluationInput, got {type(inp).__name__}"
            )
        # 2. resolve rule definition (explicit version; typed errors)
        rule = self._registry.resolve(rule_id, rule_version)
        # 4. + 5. validate input facts, configuration, scope, version
        self._registry.validate_input(rule, inp)
        # 6. resolve the deterministic evaluator for this rule
        evaluator = self._evaluators.resolve(rule.evaluator_id)
        # 7. execute (exception-safe — a failing evaluator never yields a
        #    partial event)
        result = self._execute(evaluator, rule, inp)
        # 8. validate the evaluator result against the deterministic
        #    contract (identity, provenance, event semantics, determinism)
        self._validate_result(rule, inp, result)
        # 9. + 10. deterministic event metadata is verified in step 8;
        #    evidence requests are guaranteed by the rule's declared policy
        result = self._apply_evidence_policy(rule, inp, result)
        # 11. Task 8 structured telemetry (Part 27) — one deterministic
        #    record per evaluation at the engine boundary. The log is
        #    purely observational and never feeds back into the result.
        self._record_observability(rule, inp, result)
        return result

    # ------------------------------------------------------------------
    # Step 11 — observability (Part 27)
    # ------------------------------------------------------------------

    def _record_observability(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        result: RuleEvaluationResult,
    ) -> None:
        """Task 8 structured telemetry for one rule evaluation (Part 27).

        Records rule identity + versions, configuration version, the
        tenant/venue/session scope, event-time, and the evaluation
        outcome — plus the event payload summary for MATCH (which
        carries the rule's measurements, e.g. ``dwell_duration`` and
        ``threshold_seconds`` for the dwell_threshold rule). Allowlisted
        context fields (tenant_id, venue_id, session_id, camera_id,
        event_id) ride on ``extra=``; identity fields live in the
        message, since the logging module's redaction allowlist governs
        what may be attached as context. Never logs secrets or raw
        video — only canonical, already-structured identifiers.
        """
        tenant, venue, session = self._scope_of(inp)
        payload = result.event.payload if result.event is not None else None
        # The payload is a canonical pydantic model for production rules;
        # test/demo evaluators may attach a plain dict. Both serialize to
        # JSON deterministically for the telemetry record.
        if payload is None:
            payload_summary: dict[str, object] | None = None
        elif hasattr(payload, "model_dump"):
            payload_summary = payload.model_dump(mode="json")
        else:
            payload_summary = dict(payload)
        logger.info(
            "rule evaluation: rule_id=%s rule_version=%s "
            "configuration_version_id=%s status=%s event_time=%s payload=%s",
            rule.rule_id,
            rule.rule_version,
            inp.configuration_version_id,
            result.status.value,
            inp.event_time.isoformat(),
            payload_summary,
            extra={
                "tenant_id": str(tenant),
                "venue_id": str(venue),
                "session_id": str(session),
                "camera_id": str(inp.facts[0].key.camera_id),
                "event_id": str(result.event.event_id) if result.event is not None else None,
            },
        )
        # Task 18.18 — one occupancy-session event produced at the rules
        # boundary (the slice's operational event metric).
        if (
            result.event is not None
            and rule.output_event_type.value == RuleEventType.OCCUPANCY_SESSION.value
        ):
            record_pipeline_metric(PIPELINE_METRIC_OCCUPANCY_EVENTS)

    # ------------------------------------------------------------------
    # Step 7 — exception safety (Part 15)
    # ------------------------------------------------------------------

    def _execute(
        self,
        evaluator: RuleEvaluator,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult:
        try:
            result = evaluator.evaluate(rule, inp)
        except RuleError:
            raise
        except Exception as exc:
            raise RuleEvaluationExecutionError(
                f"evaluator {rule.evaluator_id!r} for rule "
                f"{rule.canonical_identity} raised an unexpected "
                f"{type(exc).__name__}; no event was produced",
                cause=exc,
            ) from exc
        return result

    # ------------------------------------------------------------------
    # Step 8 — evaluator result validation (Parts 8 / 9 / 12 / 15)
    # ------------------------------------------------------------------

    def _validate_result(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        result: RuleEvaluationResult,
    ) -> None:
        if not isinstance(result, RuleEvaluationResult):
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} returned "
                f"{type(result).__name__}, not a RuleEvaluationResult"
            )
        if result.rule_id != rule.rule_id or result.rule_version != rule.rule_version:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} returned a result for "
                f"{result.canonical_identity} but was evaluating "
                f"{rule.canonical_identity} — a rule never reports another "
                "rule's identity"
            )
        if result.configuration_version_id != inp.configuration_version_id:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} reported configuration "
                f"version {result.configuration_version_id} but was given "
                f"{inp.configuration_version_id} — configuration versions "
                "must never be mixed"
            )
        if result.event_time != inp.event_time:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} reported event_time "
                f"{result.event_time} but was given {inp.event_time} — "
                "event-time semantics must be preserved"
            )

        scope = self._scope_of(inp)
        self._validate_provenance(rule, result, scope)

        if result.status is RuleEvaluationStatus.MATCH:
            self._validate_match(rule, inp, result)
        elif result.status in (
            RuleEvaluationStatus.NO_MATCH,
            RuleEvaluationStatus.SUPPRESSED,
        ):
            if result.event is not None:
                raise InvalidRuleEvaluationError(
                    f"evaluator {rule.evaluator_id!r} attached an EventEnvelope "
                    f"to a {result.status.value} result — only MATCH may carry "
                    "an event"
                )
        elif result.status is RuleEvaluationStatus.INVALID and not result.reason:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} returned INVALID without a deterministic reason"
            )

    def _validate_match(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        result: RuleEvaluationResult,
    ) -> None:
        if result.event is None:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} returned MATCH without an "
                "EventEnvelope — never emit an event-less MATCH"
            )
        event = result.event
        if event.event_type != rule.output_event_type.value:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} emitted event type "
                f"{event.event_type!r} but rule {rule.canonical_identity} "
                f"declares {rule.output_event_type.value!r}"
            )
        expected_id = deterministic_event_id(
            rule,
            inp,
            event_time=inp.event_time,
            event_type=rule.output_event_type.value,
        )
        if event.event_id != expected_id:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} emitted non-deterministic "
                f"event id {event.event_id} (expected {expected_id}) — event "
                "identities must be content-derived from the evaluation"
            )
        if event.event_time != inp.event_time:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} emitted event_time "
                f"{event.event_time} != input {inp.event_time}"
            )

    def _validate_provenance(
        self,
        rule: RuleDefinition,
        result: RuleEvaluationResult,
        scope: tuple[TenantId, VenueId, VideoSessionId],
    ) -> None:
        tenant, venue, session = scope
        if result.tenant_id is not None and result.tenant_id != tenant:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} reported tenant "
                f"{result.tenant_id} for facts of tenant {tenant} — "
                "cross-tenant results are never produced"
            )
        if result.venue_id is not None and result.venue_id != venue:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} reported venue "
                f"{result.venue_id} for facts of venue {venue}"
            )
        if result.session_id is not None and result.session_id != session:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} reported session "
                f"{result.session_id} for facts of session {session}"
            )
        if result.status is RuleEvaluationStatus.MATCH and (
            result.tenant_id is None or result.venue_id is None or result.session_id is None
        ):
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} returned MATCH without "
                "full tenant/venue/session provenance"
            )

    # ------------------------------------------------------------------
    # Step 10 — evidence policy (Part 10 / 9)
    # ------------------------------------------------------------------

    def _apply_evidence_policy(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
        result: RuleEvaluationResult,
    ) -> RuleEvaluationResult:
        """Guarantee the rule's declared evidence requirement.

        - REQUIRED + MATCH: the result must carry at least one canonical
          ``EvidenceRef`` request; if the evaluator omitted it, the engine
          constructs the deterministic request (the engine only DESCRIBES
          evidence — it never extracts or fetches it).
        - NONE: no evidence requests may be emitted.
        - OPTIONAL: whatever the evaluator supplied is passed through.
        """
        requirement = rule.evidence_requirement
        if result.status is not RuleEvaluationStatus.MATCH:
            return result
        event = result.event
        if event is None:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} returned MATCH without an "
                "EventEnvelope — never emit an event-less MATCH"
            )
        if requirement is EvidenceRequirement.REQUIRED:
            if not result.evidence_requests:
                request = deterministic_evidence_ref(rule, inp, event_id=event.event_id)
                return result.model_copy(update={"evidence_requests": (request,)})
            for ref in result.evidence_requests:
                if not isinstance(ref, EvidenceRef):
                    raise InvalidRuleEvaluationError(
                        f"evaluator {rule.evaluator_id!r} attached a "
                        f"{type(ref).__name__} where a canonical EvidenceRef "
                        "was required"
                    )
            return result
        if requirement is EvidenceRequirement.NONE and result.evidence_requests:
            raise InvalidRuleEvaluationError(
                f"evaluator {rule.evaluator_id!r} attached evidence requests "
                f"but rule {rule.canonical_identity} declares "
                "evidence_requirement=none"
            )
        for ref in result.evidence_requests:
            if not isinstance(ref, EvidenceRef):
                raise InvalidRuleEvaluationError(
                    f"evaluator {rule.evaluator_id!r} attached a "
                    f"{type(ref).__name__} where a canonical EvidenceRef "
                    "was required"
                )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _scope_of(inp: RuleEvaluationInput) -> tuple[TenantId, VenueId, VideoSessionId]:
        """The single tenant/venue/session scope of the input facts.

        ``validate_input`` already guarantees all facts share one scope, so
        the first fact's key is authoritative here.
        """
        key = inp.facts[0].key
        return (key.tenant_id, key.venue_id, key.session_id)
