"""Typed exception taxonomy for the deterministic rule registry (Task 16.1).

Mirrors the provider-isolation convention of the sibling packages
(``temporal``, ``geometry``, ``spatial``): downstream business logic
depends only on these types, never on raw ``ValueError`` or registry
internals leaking out.

Semantics (Task 16.1 Part 14):

- ``RuleError`` is the base for every rule-registry failure.
- ``UnknownRuleError`` — the rule_id is not registered.
- ``UnsupportedRuleVersionError`` — the rule_id exists but the requested
  version is not registered (queue_candidate:v2 asked while only :v1 is).
- ``InvalidRuleDefinitionError`` — a definition is missing metadata, has
  an invalid rule version, an unsupported fact type, or an invalid output
  event type. Malformed definitions are never repaired or coerced.
- ``DuplicateRuleError`` — the same ``(rule_id, rule_version)`` is already
  registered; nothing is silently overwritten (Part 6 immutability).
- ``UnsupportedEvaluatorError`` — the rule references an evaluator
  identity the registry does not support.
- ``UnsupportedDeterministicVersionError`` — the rule was authored
  against a deterministic contract version the registry does not
  support (the registry is pinned to the versions it can interpret).
- ``InvalidRuleInputError`` — evaluation inputs are missing, wrong-typed,
  or violate the canonical contract.
- ``MixedScopeRuleInputError`` — a single evaluation input mixes facts
  from different tenant/venue/session scopes; one evaluation always
  stays within a single scope (Task 16.2 Part 18).
- ``DuplicateEvaluatorError`` — the same evaluator identity is already
  registered in the evaluator registry; nothing is silently overwritten.
- ``InvalidRuleEvaluationError`` — an evaluator returned a result that
  violates the evaluation contract (wrong rule identity, non-deterministic
  event id, wrong event type, provenance mismatch, missing event on a
  MATCH, or evidence-policy violation). The engine never emits such a
  result (Task 16.3 Part 15).
- ``RuleEvaluationExecutionError`` — an evaluator raised an unexpected
  exception during execution; the evaluation fails as a controlled
  domain error and no partial event is ever produced.
- ``MissingRuleConfigurationError`` — required configuration keys were not
  provided for the pinned configuration version.
- ``RuleConfigurationMismatchError`` — provided configuration/rule version
  does not match the rule's pinned version.
- ``UnsupportedFactTypeError`` — an input fact is not a canonical Task 15
  fact type the rule declared.
- ``InvalidEvidenceRequestError`` — the canonical EventEnvelope cannot be
  linked to an EvidenceRef request (unknown event type, payload/scope
  mismatch, missing source provenance, or an impossible evidence
  interval). Evidence is never linked to the wrong scope (Task 17.3).

All failures are deterministic: identical input always produces the same
typed error.
"""

from __future__ import annotations

__all__ = [
    "DuplicateEvaluatorError",
    "DuplicateRuleError",
    "InvalidEvidenceRequestError",
    "InvalidRuleDefinitionError",
    "InvalidRuleEvaluationError",
    "InvalidRuleInputError",
    "MissingRuleConfigurationError",
    "MixedScopeRuleInputError",
    "RuleConfigurationMismatchError",
    "RuleError",
    "RuleEvaluationExecutionError",
    "UnknownRuleError",
    "UnsupportedDeterministicVersionError",
    "UnsupportedEvaluatorError",
    "UnsupportedFactTypeError",
    "UnsupportedRuleVersionError",
]


class RuleError(Exception):
    """Base exception for all rule registry errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class UnknownRuleError(RuleError):
    """The requested rule_id is not registered."""


class UnsupportedRuleVersionError(RuleError):
    """The rule_id exists but the requested version is not registered."""


class InvalidRuleDefinitionError(RuleError):
    """The rule definition is missing metadata or violates the contract."""


class DuplicateRuleError(RuleError):
    """The same (rule_id, rule_version) is already registered."""


class UnsupportedEvaluatorError(RuleError):
    """The rule references an evaluator the registry does not support."""


class UnsupportedDeterministicVersionError(RuleError):
    """The rule's deterministic contract version is not registry-supported."""


class MixedScopeRuleInputError(RuleError):
    """A single evaluation input mixes facts across tenant/venue/session scopes."""


class DuplicateEvaluatorError(RuleError):
    """The same evaluator identity is already registered."""


class InvalidRuleEvaluationError(RuleError):
    """An evaluator result violates the deterministic evaluation contract."""


class RuleEvaluationExecutionError(RuleError):
    """An evaluator raised an unexpected exception during execution."""


class InvalidRuleInputError(RuleError):
    """Evaluation inputs are missing, wrong-typed, or violate the contract."""


class MissingRuleConfigurationError(RuleError):
    """Required configuration keys were not provided."""


class RuleConfigurationMismatchError(RuleError):
    """Provided configuration/version does not match the rule's pinned version."""


class UnsupportedFactTypeError(RuleError):
    """An input fact is not a canonical fact type the rule declared."""


class InvalidEvidenceRequestError(RuleError):
    """The canonical EventEnvelope cannot be linked to an evidence request.

    Raised by the Task 17.3 EvidenceRequestBuilder when the envelope
    event type is not a canonical rule event, the caller-asserted scope
    does not match the event's payload scope (wrong tenant/venue/session),
    the evidence has no source provenance, or the derived evidence
    interval is impossible. Never links evidence to the wrong scope.
    """
