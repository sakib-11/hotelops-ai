"""Deterministic rule evaluator abstraction (Task 16.3).

The evaluator is the PURE decision layer between a resolved
``RuleDefinition`` and a typed ``RuleEvaluationResult``:

    RuleEvaluationInput
        ↓ RuleRegistry (explicit rule_id + rule_version)
    Versioned RuleDefinition
        ↓ RuleEvaluator (deterministic, side-effect free)
    RuleEvaluationResult
        ↓
    EventEnvelope / EvidenceRef request (Task 4, reused)

Contract (Task 16.3 Part 2): an evaluator is

- deterministic — identical (rule, input) always produces the same
  logical result;
- side-effect free — no database, Redis, S3, HTTP, FastAPI, LLM, or any
  other infrastructure access; it never reads the wall clock, samples
  randomness, or consults global mutable state;
- synchronous — evaluation is a pure function call;
- self-contained — everything it needs is in the explicit
  ``RuleEvaluationInput``; it never silently obtains missing information
  from global state (Part 3).

``RuleEvaluatorRegistry`` maps evaluator identities to implementations.
It is separate from ``RuleRegistry`` (definitions) on purpose: the rule
registry governs WHICH rules exist at which version; the evaluator
registry governs WHICH implementations are available to execute them. A
rule whose ``evaluator_id`` is not registered cannot be evaluated
(``UnsupportedEvaluatorError``) — never silently replaced by another
implementation.

Deterministic identity (Part 12): the helpers here implement the
project's canonical deterministic identity strategy — content-derived
UUID5 over ``TEMPORAL_ID_NAMESPACE`` (Task 15 / Task 7 idempotency). The
same logical evaluation reproduces the same event identity; no second
idempotency system is introduced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from contracts.common import EventId, EvidenceId
from contracts.events import EvidenceRef, EvidenceType
from contracts.rules import (
    RuleDefinition,
    RuleEvaluationInput,
    RuleEvaluationResult,
)
from contracts.temporal import TEMPORAL_ID_NAMESPACE

__all__ = [
    "EVIDENCE_ID_PREFIX",
    "RuleEvaluator",
    "RuleEvaluatorRegistry",
    "deterministic_event_id",
    "deterministic_evidence_ref",
]

_EVENT_ID_PREFIX = "rule_event|"

# The canonical evidence-request identity prefix. Shared with the
# Task 17.3 EvidenceRequestBuilder so the pipeline's envelope-driven
# request IS the engine-attached request — one logical request for one
# logical event (Task 7 idempotency), never a fresh UUID.
EVIDENCE_ID_PREFIX = "rule_evidence|"
_EVIDENCE_ID_PREFIX = EVIDENCE_ID_PREFIX  # backward-compatible alias


@runtime_checkable
class RuleEvaluator(Protocol):
    """A deterministic, side-effect-free rule evaluation unit.

    ``evaluator_id`` is the canonical identity referenced by
    ``RuleDefinition.evaluator_id``. ``evaluate`` receives the resolved
    immutable ``RuleDefinition`` and the explicit versioned input and
    returns a typed ``RuleEvaluationResult`` (NO_MATCH / MATCH /
    SUPPRESSED / INVALID). Implementations MUST be pure: no I/O, no
    ``datetime.now()``, no randomness — any identifier they emit is
    derived via ``deterministic_event_id`` / ``deterministic_evidence_ref``.
    """

    evaluator_id: str

    def evaluate(
        self,
        rule: RuleDefinition,
        inp: RuleEvaluationInput,
    ) -> RuleEvaluationResult: ...


# =============================================================================
# Deterministic identity helpers (Part 12) — content-derived UUID5
# =============================================================================


def deterministic_event_id(
    rule: RuleDefinition,
    inp: RuleEvaluationInput,
    *,
    event_time: datetime,
    event_type: str,
) -> EventId:
    """The canonical EventEnvelope id for a logical rule evaluation.

    Content-derived (UUID5 over ``TEMPORAL_ID_NAMESPACE``) from the
    dimensions that uniquely identify one logical event: tenant, venue,
    session, camera, track, spatial context (from the canonical facts'
    keys), rule identity + version, event type, logical event time, and
    the pinned configuration version. Replaying the same evaluation
    reproduces the same identity (Task 7 idempotency principle) — never
    a fresh UUID.
    """
    from uuid import uuid5

    key = inp.facts[0].key
    canonical = _EVENT_ID_PREFIX + "|".join([
        str(key.tenant_id),
        str(key.venue_id),
        str(key.session_id),
        str(key.camera_id),
        str(key.track_id),
        key.semantic_context or "",
        str(rule.rule_id),
        str(rule.rule_version),
        event_type,
        event_time.isoformat(),
        str(inp.configuration_version_id),
    ])
    return EventId(uuid5(TEMPORAL_ID_NAMESPACE, canonical))


def deterministic_evidence_ref(
    rule: RuleDefinition,
    inp: RuleEvaluationInput,
    *,
    event_id: EventId,
) -> EvidenceRef:
    """A deterministic EvidenceRef REQUEST describing required evidence.

    The rule engine only DESCRIBES the evidence required (Part 10): the
    request carries the session, source/camera, track, spatial context,
    event-time interval, and pinned configuration version — everything an
    evidence subsystem needs to fulfill it later. It never extracts video,
    reads object storage, or calls CV. ``ref_id`` is content-derived from
    the event identity so the same evaluation reproduces the same request.
    """
    from uuid import uuid5

    key = inp.facts[0].key
    ref_id = EvidenceId(
        uuid5(
            TEMPORAL_ID_NAMESPACE,
            _EVIDENCE_ID_PREFIX + f"{event_id}|{key.session_id!s}",
        )
    )
    return EvidenceRef(
        ref_id=ref_id,
        ref_type=EvidenceType.VIDEO_CLIP,
        ref_uri=f"s3://evidence/{key.tenant_id}/{key.session_id}/rule/{rule.rule_id}",
        # Task 17.2 — the typed provenance chain. These fields duplicate
        # the metadata below by design: the typed fields are the
        # machine-readable contract; ``metadata`` remains the free-form
        # provenance carrier for backward compatibility.
        event_id=event_id,
        event_time=inp.event_time,
        tenant_id=key.tenant_id,
        venue_id=key.venue_id,
        video_session_id=key.session_id,
        camera_id=key.camera_id,
        configuration_version_id=inp.configuration_version_id,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        metadata={
            "tenant_id": str(key.tenant_id),
            "venue_id": str(key.venue_id),
            "session_id": str(key.session_id),
            "camera_id": str(key.camera_id),
            "track_id": str(key.track_id),
            "semantic_context": key.semantic_context,
            "event_time": inp.event_time.isoformat(),
            # Task 18.9 — the producing source (same convention the
            # evaluators stamp on the envelope: ``rule:{rule_id}:{rule_version}``).
            "source": f"rule:{rule.canonical_identity}",
            "configuration_version_id": str(inp.configuration_version_id),
            "rule_id": str(rule.rule_id),
            "rule_version": str(rule.rule_version),
            "event_id": str(event_id),
        },
    )


# =============================================================================
# Evaluator registry
# =============================================================================


class RuleEvaluatorRegistry:
    """Registry of evaluator implementations keyed by ``evaluator_id``.

    Mirrors the ``RuleRegistry`` governance style: duplicate identities
    are rejected (typed error), lookup is O(1), iteration is deterministic
    (sorted by id). No I/O, no unbounded state.
    """

    def __init__(self) -> None:
        self._evaluators: dict[str, RuleEvaluator] = {}

    def register(self, evaluator: RuleEvaluator) -> None:
        """Register one evaluator implementation.

        Raises:
            DuplicateEvaluatorError: the evaluator_id is already registered.
        """
        from backend.app.intelligence.rules.exceptions import DuplicateEvaluatorError

        if not isinstance(evaluator, RuleEvaluator):
            msg = (
                f"evaluator must satisfy the RuleEvaluator protocol, got {type(evaluator).__name__}"
            )
            raise DuplicateEvaluatorError(msg)
        evaluator_id = evaluator.evaluator_id
        if not evaluator_id or not str(evaluator_id).strip():
            raise DuplicateEvaluatorError("evaluator_id must be a non-empty string")
        if evaluator_id in self._evaluators:
            raise DuplicateEvaluatorError(
                f"evaluator {evaluator_id!r} is already registered — "
                "implementations are immutable; register a new identity instead"
            )
        self._evaluators[evaluator_id] = evaluator

    def get(self, evaluator_id: str) -> RuleEvaluator | None:
        """Safe lookup: the registered evaluator or None."""
        return self._evaluators.get(evaluator_id)

    def has(self, evaluator_id: str) -> bool:
        """Whether the evaluator identity is registered."""
        return evaluator_id in self._evaluators

    def list(self) -> tuple[RuleEvaluator, ...]:
        """All registered evaluators in deterministic (sorted) order."""
        return tuple(self._evaluators[key] for key in sorted(self._evaluators))

    def resolve(self, evaluator_id: str) -> RuleEvaluator:
        """Strict lookup: the registered evaluator or a typed error.

        Raises:
            UnsupportedEvaluatorError: the identity is not registered.
        """
        from backend.app.intelligence.rules.exceptions import UnsupportedEvaluatorError

        evaluator = self._evaluators.get(evaluator_id)
        if evaluator is None:
            raise UnsupportedEvaluatorError(
                f"evaluator {evaluator_id!r} is not registered; "
                f"registered: {sorted(self._evaluators)}"
            )
        return evaluator
