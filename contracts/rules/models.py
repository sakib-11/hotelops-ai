"""Canonical deterministic rule registry contracts (Task 16.1).

Task 16 converts canonical temporal facts from Task 15 into operational
events WITHOUT an LLM. This module establishes ONLY the architecture and
contracts: the rule definition, the evaluation input/output contracts, and
the controlled vocabularies (rule identity, event types, fact types). The
individual business rules (occupancy session, dwell threshold, queue
candidate, service gap, turnover delay, data quality) are later Task 16
steps and are deliberately NOT implemented here.

Architecture (Task 16.1):

    TemporalFact (Task 15 canonical facts)
        ↓ RuleRegistry (registered RuleDefinitions)
    RuleDefinition (strongly typed, versioned, immutable)
        ↓ RuleEvaluator (Task 16.x — NOT implemented yet)
    RuleEvaluationResult (NO_MATCH / MATCH / SUPPRESSED / INVALID)
        ↓
    EventEnvelope (Task 4, reused) + EvidenceRef requests (Task 4, reused)

Determinism (Part 11): the same facts + configuration +
configuration_version + rule_version MUST produce the same logical
result. Nothing here reads the wall clock, samples randomness, or touches
I/O; ``event_time`` is always event-time (Task 15 semantics), never
``datetime.now()``.

The registry operates ABOVE the CV/temporal layers (Part 7): rules consume
canonical Task 15 facts only — never detector/tracker objects, RTSP
frames, raw camera packets, or inference-framework results.

Immutability (Part 6): ``RuleDefinition`` is a frozen model; the registry
stores it as-is and rejects duplicate ``(rule_id, rule_version)``
registration — a behavior change is always a NEW rule version, never an
in-place mutation (required for historical replay).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    EventId,
    RuleId,
    RuleVersion,
    TenantId,
    TrackId,
    VenueId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)
from contracts.events import EventEnvelope, EvidenceRef
from contracts.temporal import (
    DwellInterval,
    MovementClassificationTransition,
    MovementMeasurement,
    OccupancySnapshot,
    TemporalTransition,
    WaitingInterval,
)

__all__ = [
    "EVIDENCE_REQUIREMENT_NONE",
    "RULE_VERSION_PATTERN",
    "CooldownPolicy",
    "DataQualityPayload",
    "DataQualitySeverity",
    "DwellThresholdPayload",
    "EvidenceRequirement",
    "FactType",
    "OccupancySessionPayload",
    "OccupancySessionPhase",
    "QualityFinding",
    "QueueCandidatePayload",
    "RuleDefinition",
    "RuleEvaluationInput",
    "RuleEvaluationResult",
    "RuleEvaluationStatus",
    "RuleEventType",
    "RuleIdentifier",
    "ServiceGapCandidatePayload",
    "TemporalFact",
    "TurnoverDelayPayload",
    "validate_rule_version",
]

# =============================================================================
# Controlled vocabularies (Parts 4 / 9) — identifiers are NEVER hardcoded
# throughout the application; they live here as the single source.
# =============================================================================


class RuleIdentifier(StrEnum):
    """Centralized canonical rule identifiers (Task 16 Part 4).

    These are the stable natural keys of the operational rules. Business
    rules are NOT implemented in 16.1; this enum is the controlled catalog
    later rule steps register under. ``queue_candidate:v1`` vs
    ``queue_candidate:v2`` are distinct via ``RuleVersion``.
    """

    OCCUPANCY_SESSION = "occupancy_session"
    DWELL_THRESHOLD = "dwell_threshold"
    QUEUE_CANDIDATE = "queue_candidate"
    SERVICE_GAP_CANDIDATE = "service_gap_candidate"
    TURNOVER_DELAY = "turnover_delay"
    DATA_QUALITY = "data_quality"


class RuleEventType(StrEnum):
    """Controlled operational event-type vocabulary (Task 16 Part 9).

    Rule output MUST reference one of these canonical event types — no
    arbitrary free-form event names. Values are consistent with the
    Task 4 ``EventEnvelope.event_type`` string convention (lowercase,
    snake_case, min length 1); the envelope itself is NOT duplicated.
    """

    OCCUPANCY_SESSION = "occupancy_session"
    DWELL_THRESHOLD = "dwell_threshold"
    QUEUE_CANDIDATE = "queue_candidate"
    SERVICE_GAP_CANDIDATE = "service_gap_candidate"
    TURNOVER_DELAY = "turnover_delay"
    DATA_QUALITY = "data_quality"


class FactType(StrEnum):
    """Canonical temporal fact types a rule may declare as input (Part 7).

    One member per Task 15 canonical fact contract. A rule's
    ``input_fact_types`` MUST be drawn from this set — the registry
    rejects any other value (unsupported fact type).
    """

    TEMPORAL_TRANSITION = "temporal_transition"
    DWELL_INTERVAL = "dwell_interval"
    OCCUPANCY_SNAPSHOT = "occupancy_snapshot"
    MOVEMENT_MEASUREMENT = "movement_measurement"
    MOVEMENT_CLASSIFICATION_TRANSITION = "movement_classification_transition"
    WAITING_INTERVAL = "waiting_interval"


# The canonical Task 15 facts a rule may consume (Part 7). Rules never see
# CV-layer objects; this union is the ONLY sanctioned input boundary.
TemporalFact = (
    TemporalTransition
    | DwellInterval
    | OccupancySnapshot
    | MovementMeasurement
    | MovementClassificationTransition
    | WaitingInterval
)


class EvidenceRequirement(StrEnum):
    """Whether a rule's MATCH must be accompanied by an evidence request."""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


# Sentinel for the common default (avoids a bare string in the contract).
EVIDENCE_REQUIREMENT_NONE = EvidenceRequirement.NONE

# =============================================================================
# Versioning (Part 4) — explicit rule versioning
# =============================================================================

# Explicit version format: "v1", "v2", "v1.2", ... A version change is
# always explicit and distinguishes queue_candidate:v1 from :v2.
RULE_VERSION_PATTERN = re.compile(r"^v[0-9]+(?:\.[0-9]+)*$")


def validate_rule_version(version: RuleVersion) -> RuleVersion:
    """Validate an explicit rule version string (e.g. ``"v1"``, ``"v1.2"``).

    Raises:
        ValueError: if the version is not a supported explicit form.
    """
    if not isinstance(version, str) or RULE_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(
            f"invalid rule version {version!r}; expected an explicit version such as 'v1' or 'v1.2'"
        )
    return version


# =============================================================================
# Cooldown policy (Part 3 — declared here; EXECUTION is a later Task 16 step)
# =============================================================================


class CooldownPolicy(BaseModel, frozen=True):
    """Declared cooldown behavior for a rule (policy only, no execution).

    ``enabled`` + ``duration_seconds`` describe when repeat MATCHes of the
    same rule should be suppressed; the cooldown EXECUTION and duplicate
    suppression belong to later Task 16 steps and are NOT implemented here.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    duration_seconds: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _validate_enabled_duration(self) -> CooldownPolicy:
        if self.enabled and self.duration_seconds <= 0:
            raise ValueError(
                "an enabled cooldown requires duration_seconds > 0 "
                "(a zero-duration cooldown is meaningless)"
            )
        return self


# =============================================================================
# Rule definition (Part 3)
# =============================================================================


class RuleDefinition(BaseModel, frozen=True):
    """A strongly typed, immutable, versioned rule definition.

    Fields (Task 16.1 Part 3):
      - ``rule_id`` — stable canonical identity (``RuleIdentifier`` value);
        never hardcoded elsewhere.
      - ``rule_version`` — explicit version; ``(rule_id, rule_version)`` is
        the registry key and the identity is immutable once registered.
      - ``rule_name`` / ``description`` — human metadata.
      - ``enabled`` — whether the rule participates in evaluation.
      - ``input_fact_types`` — the canonical Task 15 fact types the rule
        consumes (Part 7); must be non-empty and drawn from ``FactType``.
      - ``configuration_requirements`` — the configuration keys the rule
        requires; evaluation must receive them explicitly (Part 10).
      - ``evaluator_id`` — the deterministic evaluator identity that
        implements this rule (registered in later Task 16 steps).
      - ``output_event_type`` — a controlled ``RuleEventType`` (Part 9).
      - ``evidence_requirement`` — whether MATCH must request evidence.
      - ``cooldown_policy`` — declared cooldown (policy only).
      - ``deterministic_version`` — the version of the deterministic
        interpretation semantics this rule was authored against (Task 15's
        ``TEMPORAL_ENGINE_VERSION``); a change means a NEW rule version.

    Frozen by construction: registering then mutating metadata in place is
    impossible; behavior changes require a new ``rule_version``.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    rule_id: RuleId
    rule_version: RuleVersion
    rule_name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    enabled: bool = True
    input_fact_types: frozenset[FactType] = Field(..., min_length=1)
    configuration_requirements: frozenset[str] = Field(default_factory=frozenset)
    evaluator_id: str = Field(..., min_length=1)
    output_event_type: RuleEventType
    evidence_requirement: EvidenceRequirement = EvidenceRequirement.NONE
    cooldown_policy: CooldownPolicy = Field(default_factory=CooldownPolicy)
    deterministic_version: str = Field(..., min_length=1)

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_version = field_validator("rule_version")(validate_rule_version)

    @model_validator(mode="after")
    def _validate_definition(self) -> RuleDefinition:
        if not self.rule_id or not str(self.rule_id).strip():
            raise ValueError("rule_id must be a non-empty canonical identifier")
        if not self.rule_name.strip():
            raise ValueError("rule_name must be a non-empty string")
        if not self.description.strip():
            raise ValueError("description must be a non-empty string")
        if not self.evaluator_id.strip():
            raise ValueError("evaluator_id must be a non-empty string")
        return self

    @property
    def canonical_identity(self) -> str:
        """Deterministic identity ``f"{rule_id}:{rule_version}"`` (Part 4)."""
        return f"{self.rule_id}:{self.rule_version}"


# =============================================================================
# Evaluation input / output contracts (Parts 8 / 10)
# =============================================================================


class RuleEvaluationInput(BaseModel, frozen=True):
    """The explicit, versioned inputs a rule evaluation receives (Part 10).

    A rule evaluation NEVER silently reads "the latest configuration":
    ``configuration`` is the explicit configuration snapshot and
    ``configuration_version_id`` names the pinned version that produced it.
    ``event_time`` is the authoritative EVENT time driving the evaluation
    (Task 15 semantics) — never ``datetime.now()``; processing time is not
    part of this contract. Provenance (tenant/venue/session/track/context)
    is carried by the nested keys of the canonical facts.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    facts: tuple[TemporalFact, ...]
    configuration: dict[str, Any] = Field(default_factory=dict)
    configuration_version_id: ConfigurationVersionId
    rule_version: RuleVersion
    event_time: datetime
    # Caller-supplied processing-time METADATA only (the temporal engine
    # convention: Task 15 ``TemporalInput.processing_time``). It NEVER
    # affects business semantics — ``event_time`` is authoritative — and
    # is used only to stamp the deterministic ``EventEnvelope.produced_at``
    # (defaults to ``event_time`` when omitted so the pure core never reads
    # the wall clock).
    processing_time: datetime | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_version = field_validator("rule_version")(validate_rule_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
    _validate_processing_time = field_validator("processing_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_facts(self) -> RuleEvaluationInput:
        if not self.facts:
            raise ValueError("a rule evaluation requires at least one canonical fact")
        return self


class RuleEvaluationStatus(StrEnum):
    """Deterministic outcome of a rule evaluation (Task 16.1 Part 8)."""

    NO_MATCH = "no_match"  # the rule's conditions did not hold
    MATCH = "match"  # the rule fired → EventEnvelope (+ evidence if required)
    SUPPRESSED = "suppressed"  # a MATCH would occur but is suppressed (cooldown/dedup)
    INVALID = "invalid"  # inputs/configuration were invalid — never fabricated


class RuleEvaluationResult(BaseModel, frozen=True):
    """A typed rule evaluation result (Task 16.1 Part 8).

    Carries everything needed to deterministically create the canonical
    ``EventEnvelope`` (Task 4) and any ``EvidenceRef`` requests (Task 4) —
    those contracts are REUSED, never duplicated. ``tenant_id`` /
    ``venue_id`` / ``session_id`` are the provenance of the source facts,
    exposed for Task 8 observability context (Part 18).

    Invariants:
      - a MATCH must carry an EventEnvelope; a non-MATCH must not;
      - an INVALID result must carry a ``reason`` (never silently ignored).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    rule_id: RuleId
    rule_version: RuleVersion
    status: RuleEvaluationStatus
    event_time: datetime
    configuration_version_id: ConfigurationVersionId
    event: EventEnvelope[Any] | None = None
    evidence_requests: tuple[EvidenceRef, ...] = ()
    tenant_id: TenantId | None = None
    venue_id: VenueId | None = None
    session_id: VideoSessionId | None = None
    reason: str | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_version = field_validator("rule_version")(validate_rule_version)
    _validate_event_time = field_validator("event_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_result(self) -> RuleEvaluationResult:
        if self.status is RuleEvaluationStatus.MATCH:
            if self.event is None:
                raise ValueError("a MATCH result must carry a canonical EventEnvelope")
        elif self.event is not None:
            raise ValueError("only a MATCH result may carry an EventEnvelope")
        if self.status is RuleEvaluationStatus.INVALID and not self.reason:
            raise ValueError("an INVALID result must carry a deterministic reason")
        return self

    @property
    def canonical_identity(self) -> str:
        """Deterministic identity ``f"{rule_id}:{rule_version}"`` (Part 4)."""
        return f"{self.rule_id}:{self.rule_version}"


# =============================================================================
# Task 16.4 — occupancy session payload (the FIRST operational rule)
# =============================================================================


class OccupancySessionPhase(StrEnum):
    """The deterministic boundary direction of one occupancy session event.

    The canonical event type is ``occupancy_session`` (Task 16.1 Part 9 —
    one controlled type, no second taxonomy); the phase distinguishes the
    two logical boundaries of one session:

    - ``STARTED`` — the scope became occupied (confirmed count 0 -> >0);
    - ``ENDED`` — the scope became unoccupied (confirmed count >0 -> 0).
    """

    STARTED = "started"
    ENDED = "ended"


class OccupancySessionPayload(BaseModel, frozen=True):
    """Typed payload of an ``occupancy_session`` event (Task 16.4 Part 8).

    Carries ONLY canonical information already available from the
    qualifying ``OccupancySnapshot`` (Task 15.4) and the evaluation
    provenance — no speculative business fields:

    - ``phase`` — started / ended (the logical boundary);
    - scope identity — tenant / venue / session / camera / spatial
      context (from the snapshot's canonical key, never raw CV);
    - ``occupancy_count`` — the confirmed count at the boundary;
    - ``occupied_tracks`` — the canonical entity identities counted at
      the boundary (sorted for deterministic serialization);
    - ``occupancy_time`` — the boundary event time (the snapshot's
      event_time, i.e. the confirmed start or end instant);
    - provenance — configuration_version_id + rule identity/version.

    ``duration`` is deliberately ABSENT: it is not deterministically
    known from a single boundary snapshot (the rule is stateless per
    fact, and correlating start/end would require session state outside
    the deterministic core).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    phase: OccupancySessionPhase
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId
    spatial_context_id: str | None = None
    occupancy_count: int = Field(..., ge=0)
    occupied_tracks: tuple[TrackId, ...] = ()
    occupancy_time: datetime
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_occupancy_time = field_validator("occupancy_time")(validate_utc)


# =============================================================================
# Task 16.5 — dwell threshold payload (the SECOND operational rule)
# =============================================================================


class DwellThresholdPayload(BaseModel, frozen=True):
    """Typed payload of a ``dwell_threshold`` event (Task 16.5 Part 11).

    Carries ONLY canonical information already available from the
    qualifying ``DwellInterval`` (Task 15.3) and the evaluation
    provenance — no speculative business fields:

    - scope identity — tenant / venue / session / camera / spatial
      context (from the interval's canonical key, never raw CV);
    - ``interval_id`` — the canonical dwell interval identity (stable
      while the interval is open), so downstream consumers can group
      facts of one logical dwell session;
    - ``dwell_start_time`` — the confirmed-PRESENT instant;
    - ``threshold_crossing_time`` — the event-time instant of THIS
      evaluation (``inp.event_time`` == the fact's ``last_seen``);
    - ``dwell_duration`` — the canonical event-time duration (seconds,
      the project's single duration unit — never mixed units);
    - ``threshold_seconds`` — the EXPLICIT configured threshold the
      crossing was evaluated against (preserved for traceability);
    - provenance — configuration_version_id + rule identity/version.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    interval_id: EventId
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId
    spatial_context_id: str | None = None
    dwell_start_time: datetime
    threshold_crossing_time: datetime
    dwell_duration: float = Field(..., ge=0)
    threshold_seconds: float = Field(..., gt=0)
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_dwell_start = field_validator("dwell_start_time")(validate_utc)
    _validate_crossing = field_validator("threshold_crossing_time")(validate_utc)


# =============================================================================
# Task 16.6 — queue candidate payload (the THIRD operational rule)
# =============================================================================


class QueueCandidatePayload(BaseModel, frozen=True):
    """Typed payload of a ``queue_candidate`` event (Task 16.6 Part 7).

    Carries ONLY canonical information already available from the
    qualifying ``WaitingInterval`` (Task 15.5.3) and the evaluation
    provenance — no speculative business fields:

    - scope identity — tenant / venue / session / camera (from the
      interval's canonical key, never raw CV);
    - ``track_id`` — the canonical subject identity of the waiting
      entity (a person is the queue subject; the track id is the only
      entity identity the deterministic core carries);
    - ``spatial_context_id`` — the queue/service-area profile id the
      fact was confirmed waiting in (``key.semantic_context`` — the
      canonical spatial context, never re-derived geometry);
    - ``interval_id`` — the canonical waiting interval identity (stable
      while the interval is open), so downstream consumers can group
      facts of one logical waiting episode;
    - ``waiting_start_time`` — the confirmed-WAITING instant;
    - ``qualification_time`` — the event-time instant of THIS evaluation
      (``inp.event_time`` == the fact's ``last_seen``);
    - ``waiting_duration`` — the canonical event-time duration (seconds,
      the project's single duration unit — never mixed units);
    - ``threshold_seconds`` — the EXPLICIT configured minimum waiting
      duration the qualification was evaluated against (traceability);
    - provenance — configuration_version_id + rule identity/version.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    interval_id: EventId
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId
    track_id: TrackId
    spatial_context_id: str | None = None
    waiting_start_time: datetime
    qualification_time: datetime
    waiting_duration: float = Field(..., ge=0)
    threshold_seconds: float = Field(..., gt=0)
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_waiting_start = field_validator("waiting_start_time")(validate_utc)
    _validate_qualification = field_validator("qualification_time")(validate_utc)


# =============================================================================
# Task 16.7 — service-gap candidate payload (the FOURTH operational rule)
# =============================================================================


class ServiceGapCandidatePayload(BaseModel, frozen=True):
    """Typed payload of a ``service_gap_candidate`` event (Task 16.7 Part 21).

    Carries ONLY canonical information already available from the
    qualifying ``WaitingInterval`` (Task 15.5.3) and the evaluation
    provenance — no speculative business fields:

    - scope identity — tenant / venue / session / camera (from the
      interval's canonical key, never raw CV);
    - ``track_id`` — the canonical subject identity of the waiting
      entity (a guest waiting for service);
    - ``service_area_id`` — the configured service-area profile id the
      fact was confirmed waiting in (``key.semantic_context`` — the
      canonical spatial context, never re-derived geometry);
    - ``interval_id`` — the canonical waiting interval identity (stable
      while the interval is open), so downstream consumers can group
      facts of one logical service-gap episode;
    - ``gap_start_time`` — the confirmed-WAITING instant;
    - ``qualification_time`` — the event-time instant of THIS evaluation
      (``inp.event_time`` == the fact's ``last_seen``);
    - ``gap_duration`` — the canonical event-time duration (seconds,
      the project's single duration unit — never mixed units);
    - ``threshold_seconds`` — the EXPLICIT configured service-gap grace
      threshold the qualification was evaluated against (traceability);
    - provenance — configuration_version_id + rule identity/version.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    interval_id: EventId
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId
    track_id: TrackId
    service_area_id: str | None = None
    gap_start_time: datetime
    qualification_time: datetime
    gap_duration: float = Field(..., ge=0)
    threshold_seconds: float = Field(..., gt=0)
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_gap_start = field_validator("gap_start_time")(validate_utc)
    _validate_qualification = field_validator("qualification_time")(validate_utc)


# =============================================================================
# Task 16.8 — turnover delay payload (the FIFTH operational rule)
# =============================================================================


class TurnoverDelayPayload(BaseModel, frozen=True):
    """Typed payload of a ``turnover_delay`` event (Task 16.8 Part 21).

    Carries ONLY canonical information already available from the
    qualifying ``DwellInterval`` (Task 15.3) and the evaluation
    provenance — no speculative business fields:

    - scope identity — tenant / venue / session / camera (from the
      interval's canonical key, never raw CV);
    - ``track_id`` — the canonical subject identity of the occupying
      entity; ``spatial_context_id`` — the table/service-area/zone
      profile id (``key.semantic_context`` — the canonical Task 14
      spatial identity, never re-derived geometry);
    - ``interval_id`` — the canonical dwell interval identity (stable
      while the interval is open), so downstream consumers can group
      facts of one logical turnover episode;
    - ``turnover_start_time`` — the confirmed-occupied instant;
    - ``threshold_crossing_time`` — the event-time instant of THIS
      evaluation (``inp.event_time`` == the fact's ``last_seen``);
    - ``turnover_duration`` — the canonical event-time duration
      (seconds, the project's single duration unit — never mixed);
    - ``service_window_seconds`` / ``threshold_seconds`` — the EXPLICIT
      configured service window and turnover-delay threshold the
      qualification was evaluated against (traceability); the effective
      boundary is ``service_window + turnover_delay``;
    - provenance — configuration_version_id + rule identity/version.
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    interval_id: EventId
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId
    track_id: TrackId
    spatial_context_id: str | None = None
    turnover_start_time: datetime
    threshold_crossing_time: datetime
    turnover_duration: float = Field(..., ge=0)
    service_window_seconds: float = Field(..., ge=0)
    threshold_seconds: float = Field(..., gt=0)
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_turnover_start = field_validator("turnover_start_time")(validate_utc)
    _validate_crossing = field_validator("threshold_crossing_time")(validate_utc)


# =============================================================================
# Task 16.9 — data-quality event payload (the FINAL Task 16 rule)
# =============================================================================


class DataQualitySeverity(StrEnum):
    """Deterministic severity of a data-quality finding (Task 16.9 Part 8).

    The minimum canonical severity model required for data-quality events
    — ``INFO`` < ``WARNING`` < ``ERROR`` < ``CRITICAL`` (Part 8's suggested
    semantics). Deliberately a SEPARATE vocabulary from the alert
    ``Severity`` (contracts.operations: CRITICAL/HIGH/MEDIUM/LOW/INFO):
    quality severity classifies FACT quality with ERROR/WARNING semantics,
    which the alert enum cannot express losslessly (it has no WARNING or
    ERROR members). Severity is DETERMINISTIC for a given check — it is
    declared in the check's registry entry, never computed from runtime
    state (Part 8: "The severity must be deterministic for a given check").
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class QualityFinding(BaseModel, frozen=True):
    """One deterministic data-quality finding (Task 16.9 Part 10).

    ``quality_code`` is the stable machine-readable code (Part 9). The
    finding names the affected canonical fact and carries the deterministic
    severity + human description of the check that fired. Findings are pure
    functions of the fact + explicit configuration — no wall clock, no
    randomness, no external state (Part 25).
    """

    model_config = {"extra": "forbid"}

    quality_code: str = Field(..., min_length=1)
    severity: DataQualitySeverity
    description: str = Field(..., min_length=1)
    affected_fact_type: str = Field(..., min_length=1)  # FactType value
    affected_fact_id: str = Field(..., min_length=1)  # the fact's canonical id
    check_version: str = Field(..., min_length=1)


class DataQualityPayload(BaseModel, frozen=True):
    """Typed payload of a ``data_quality`` event (Task 16.9 Part 20).

    One AGGREGATE quality event per evaluation (Part 13 — the rule result
    model carries exactly one EventEnvelope), with ALL findings included in
    deterministic order (sorted by ``quality_code``; Part 14). ``findings``
    is non-empty — a MATCH always carries at least one finding;
    ``primary_quality_code`` / ``primary_severity`` are the first finding's
    code + severity (the headline, Part 10).

    Carries ONLY canonical information from the affected fact's key +
    evaluation provenance — never raw video, detections, or secrets
    (Part 39). ``camera_id`` / ``track_id`` are OPTIONAL because a missing
    source/subject identity is itself a finding
    (``DATA_MISSING_REQUIRED_IDENTITY``); tenant/venue/session are always
    present — the engine forbids an event without full scope provenance
    (Parts 22/23).
    """

    model_config = {"extra": "forbid"}

    schema_version: str = Field(default=SCHEMA_VERSION)
    findings: tuple[QualityFinding, ...] = Field(..., min_length=1)
    primary_quality_code: str = Field(..., min_length=1)
    primary_severity: DataQualitySeverity
    affected_fact_type: str = Field(..., min_length=1)
    affected_fact_id: str = Field(..., min_length=1)
    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId | None = None
    track_id: TrackId | None = None
    spatial_context_id: str | None = None
    event_time: datetime
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_event_time = field_validator("event_time")(validate_utc)
