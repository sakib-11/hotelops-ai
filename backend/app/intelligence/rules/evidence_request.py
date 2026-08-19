"""Deterministic Event → Evidence linkage (Task 17.3).

Connects a material Task 16 event to its canonical ``EvidenceRef`` request
without duplicating any business-rule logic:

    Task 16 EventEnvelope
            ↓ EvidenceRequestBuilder
        EvidenceRef
            ↓ (later) Evidence extraction pipeline

The builder is a PURE, deterministic, side-effect-free function:

- accepts ONLY a canonical ``EventEnvelope`` (Task 4) whose payload is a
  canonical rule payload — it never re-runs rules, never inspects raw
  frames, never runs YOLO/tracking, never queries "latest" configuration
  (the pinned ``configuration_version_id`` comes from the event payload),
  never modifies the envelope, and never touches object storage;
- preserves the full provenance chain — event_id, tenant_id, venue_id,
  session_id, source (the envelope's producer, e.g.
  ``rule:dwell_threshold:v1``, carried on the request metadata),
  camera/asset, event_time, configuration_version, rule_id,
  rule_version — on the typed ``EvidenceRef`` fields;
- ``scope_params_from_envelope`` derives the caller-asserted scope
  directly from the envelope's canonical payload — the Task 18.9
  integration entry point for linking a material Task 16 event;
- determines the requested evidence interval deterministically: the
  event's interval start (``dwell_start_time`` / ``waiting_start_time`` /
  ``gap_start_time`` / ``turnover_start_time`` / ``occupancy_time``) to
  the event time, overridable with explicit bounds;
- derives the request identity with the SAME content-derived UUID5 scheme
  as the rule engine (``EVIDENCE_ID_PREFIX`` over ``TEMPORAL_ID_NAMESPACE``)
  — so the pipeline request IS the engine-attached request, and the same
  event + same evidence parameters always produce one logical request
  (Task 7 idempotency); replay and duplicate delivery collapse to it;
- cross-validates the caller-asserted scope (``EvidenceRequestParams``)
  against the event payload's canonical scope — a wrong tenant, venue, or
  session is REJECTED (``InvalidEvidenceRequestError``), never linked.

Security boundary (Task 5 conventions): the caller supplies its asserted
tenant/venue/session; the builder verifies the asserted scope equals the
server-derived payload scope. An event whose payload carries no source
(``data_quality`` with ``camera_id=None``) still requires source
provenance from the params — evidence is never requested without knowing
which camera/asset it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field, field_validator

from backend.app.intelligence.rules.evaluator import EVIDENCE_ID_PREFIX
from backend.app.intelligence.rules.exceptions import InvalidEvidenceRequestError
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EvidenceId,
    RuleId,
    RuleVersion,
    TenantId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
    validate_utc,
)
from contracts.events import EventEnvelope, EvidenceRef, EvidenceType
from contracts.rules import (
    DataQualityPayload,
    DwellThresholdPayload,
    EvidenceRequirement,
    OccupancySessionPayload,
    QueueCandidatePayload,
    RuleEventType,
    ServiceGapCandidatePayload,
    TurnoverDelayPayload,
)
from contracts.temporal import TEMPORAL_ID_NAMESPACE

__all__ = [
    "EvidenceRequestBuilder",
    "EvidenceRequestParams",
    "InvalidEvidenceRequestError",
    "scope_params_from_envelope",
]


class _RulePayload(Protocol):
    """The canonical scope/provenance every rule payload carries.

    Structural typing lets the builder read only the canonical fields
    without depending on any single payload implementation — and mypy
    verifies the accessors against this shape.
    """

    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId | None
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion


class EvidenceRequestParams(BaseModel, frozen=True):
    """Caller-asserted scope + optional evidence refinement (Task 17.3).

    The builder cross-validates the asserted scope against the event
    envelope's canonical payload scope — a wrong tenant/venue/session is
    rejected, never linked. Optional refinements (``video_asset_id``,
    explicit window, frame range, detector/tracker versions) default to
    the deterministic derivation from the payload.
    """

    model_config = {"extra": "forbid"}

    tenant_id: TenantId
    venue_id: VenueId
    video_session_id: VideoSessionId
    camera_id: CameraId | None = None
    video_asset_id: VideoAssetId | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    detector_version: str | None = None
    tracker_version: str | None = None

    _validate_start = field_validator("start_time")(validate_utc)
    _validate_end = field_validator("end_time")(validate_utc)


# Canonical rule event type → payload model. The builder reads ONLY these
# canonical payloads (event_type is the controlled RuleEventType
# vocabulary — never a free-form string); an unrecognized event type
# cannot be linked deterministically and is rejected.
_EVENT_TYPE_TO_PAYLOAD: dict[str, type[BaseModel]] = {
    RuleEventType.OCCUPANCY_SESSION.value: OccupancySessionPayload,
    RuleEventType.DWELL_THRESHOLD.value: DwellThresholdPayload,
    RuleEventType.QUEUE_CANDIDATE.value: QueueCandidatePayload,
    RuleEventType.SERVICE_GAP_CANDIDATE.value: ServiceGapCandidatePayload,
    RuleEventType.TURNOVER_DELAY.value: TurnoverDelayPayload,
    RuleEventType.DATA_QUALITY.value: DataQualityPayload,
}

# The canonical interval-start field of each rule payload (the instant the
# evidenced episode began). ``occupancy_time`` is a single boundary
# instant — the window degenerates to that instant, which is correct.
_INTERVAL_START_FIELDS: tuple[str, ...] = (
    "dwell_start_time",
    "waiting_start_time",
    "gap_start_time",
    "turnover_start_time",
    "occupancy_time",
)


def _interval_start(payload: _RulePayload) -> datetime | None:
    """The deterministic episode-start instant from a canonical payload."""
    for field_name in _INTERVAL_START_FIELDS:
        if hasattr(payload, field_name):
            value = getattr(payload, field_name)
            if value is not None:
                return cast(datetime, value)
    return None


def _payload_of(envelope: EventEnvelope[Any]) -> _RulePayload:
    """Resolve + validate the canonical rule payload of the envelope."""
    model_cls = _EVENT_TYPE_TO_PAYLOAD.get(envelope.event_type)
    if model_cls is None:
        msg = (
            f"unsupported event_type {envelope.event_type!r} — evidence can "
            f"only be linked from a canonical rule event"
        )
        raise InvalidEvidenceRequestError(msg)
    if isinstance(envelope.payload, dict):
        return cast(_RulePayload, model_cls.model_validate(envelope.payload))
    if not isinstance(envelope.payload, model_cls):
        msg = (
            f"event payload for {envelope.event_type!r} is not the canonical "
            f"{model_cls.__name__} contract (got {type(envelope.payload).__name__})"
        )
        raise InvalidEvidenceRequestError(msg)
    return cast(_RulePayload, envelope.payload)


def scope_params_from_envelope(envelope: EventEnvelope[Any]) -> EvidenceRequestParams:
    """Derive the caller-asserted scope from the envelope's canonical payload.

    The material event is the trusted producer output — its canonical
    rule payload IS the server-derived scope (Task 18.9 integration
    entry point). The builder still cross-validates the derived params
    against the payload, so a corrupted envelope fails deterministically
    instead of ever linking wrong-scope evidence.

    Raises:
        InvalidEvidenceRequestError: the envelope is not a canonical rule
            event, so its scope cannot be derived deterministically.
    """
    payload = _payload_of(envelope)
    return EvidenceRequestParams(
        tenant_id=payload.tenant_id,
        venue_id=payload.venue_id,
        video_session_id=payload.session_id,
        camera_id=payload.camera_id,
    )


class EvidenceRequestBuilder:
    """Deterministic EventEnvelope → EvidenceRef linkage (Task 17.3).

    Thread-safe and stateless: ``build`` is a pure function of its inputs.
    """

    def build(
        self,
        envelope: EventEnvelope[Any],
        *,
        params: EvidenceRequestParams,
        evidence_requirement: EvidenceRequirement = EvidenceRequirement.REQUIRED,
    ) -> EvidenceRef | None:
        """Build the deterministic evidence request for a material event.

        Args:
            envelope: The canonical Task 4 EventEnvelope (never modified).
            params: Caller-asserted scope + optional evidence refinement.
            evidence_requirement: The rule's declared requirement. When
                ``NONE`` the event must NOT be linked — returns None.

        Returns:
            The deterministic EvidenceRef request, or None when the rule
            declares no evidence requirement.

        Raises:
            InvalidEvidenceRequestError: unknown event type, scope
                mismatch, missing source provenance, or impossible
                evidence interval.
        """
        if evidence_requirement is EvidenceRequirement.NONE:
            return None
        if not isinstance(envelope, EventEnvelope):
            msg = f"expected a canonical EventEnvelope, got {type(envelope).__name__}"
            raise InvalidEvidenceRequestError(msg)

        payload = _payload_of(envelope)

        # --- Canonical scope + provenance from the payload (never re-derived) ---
        tenant_id: TenantId = payload.tenant_id
        venue_id: VenueId = payload.venue_id
        session_id: VideoSessionId = payload.session_id
        payload_camera_id: CameraId | None = getattr(payload, "camera_id", None)
        configuration_version_id: ConfigurationVersionId = payload.configuration_version_id
        rule_id: RuleId = payload.rule_id
        rule_version: RuleVersion = payload.rule_version

        # --- Cross-validate the caller-asserted scope (Task 5 boundary) ---
        if params.tenant_id != tenant_id:
            msg = (
                f"tenant scope mismatch: params {params.tenant_id} != "
                f"event payload {tenant_id} — never link another tenant's evidence"
            )
            raise InvalidEvidenceRequestError(msg)
        if params.venue_id != venue_id:
            msg = (
                f"venue scope mismatch: params {params.venue_id} != "
                f"event payload {venue_id} — never link another venue's evidence"
            )
            raise InvalidEvidenceRequestError(msg)
        if params.video_session_id != session_id:
            msg = (
                f"session mismatch: params {params.video_session_id} != "
                f"event payload {session_id} — evidence must stay within the session"
            )
            raise InvalidEvidenceRequestError(msg)
        if (
            params.camera_id is not None
            and payload_camera_id is not None
            and params.camera_id != payload_camera_id
        ):
            msg = f"camera mismatch: params {params.camera_id} != event payload {payload_camera_id}"
            raise InvalidEvidenceRequestError(msg)

        # --- Evidence interval (deterministic; override only via params) ---
        event_time = envelope.event_time
        start_time = (
            params.start_time
            if params.start_time is not None
            else (_interval_start(payload) or event_time)
        )
        end_time = params.end_time if params.end_time is not None else event_time
        if end_time < start_time:
            msg = (
                f"evidence interval end {end_time.isoformat()} precedes "
                f"start {start_time.isoformat()}"
            )
            raise InvalidEvidenceRequestError(msg)

        # --- Source provenance (a clip is meaningless without a source) ---
        camera_id = params.camera_id if params.camera_id is not None else payload_camera_id
        if camera_id is None and params.video_asset_id is None:
            msg = (
                "missing source provenance — evidence cannot be linked "
                "without a camera_id or video_asset_id"
            )
            raise InvalidEvidenceRequestError(msg)

        # --- Deterministic identity — SAME scheme as the rule engine ---
        from uuid import uuid5

        ref_id = EvidenceId(
            uuid5(
                TEMPORAL_ID_NAMESPACE,
                EVIDENCE_ID_PREFIX + f"{envelope.event_id}|{session_id}",
            )
        )

        metadata: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "venue_id": str(venue_id),
            "session_id": str(session_id),
            "event_id": str(envelope.event_id),
            "event_time": event_time.isoformat(),
            "interval_start": start_time.isoformat(),
            "interval_end": end_time.isoformat(),
            # Task 18.9 — the producing source is preserved on the request
            # (``rule:{rule_id}:{rule_version}`` for canonical rule events).
            "source": envelope.source,
            "configuration_version_id": str(configuration_version_id),
            "rule_id": str(rule_id),
            "rule_version": str(rule_version),
        }
        for field_name in ("track_id", "spatial_context_id", "service_area_id"):
            value = getattr(payload, field_name, None)
            if value is not None:
                metadata[field_name] = str(value)

        return EvidenceRef(
            ref_id=ref_id,
            ref_type=EvidenceType.VIDEO_CLIP,
            ref_uri=f"s3://evidence/{tenant_id}/{session_id}/rule/{rule_id}",
            event_id=envelope.event_id,
            event_time=event_time,
            tenant_id=tenant_id,
            venue_id=venue_id,
            video_session_id=session_id,
            camera_id=camera_id,
            video_asset_id=params.video_asset_id,
            start_time=start_time,
            end_time=end_time,
            start_frame=params.start_frame,
            end_frame=params.end_frame,
            configuration_version_id=configuration_version_id,
            detector_version=params.detector_version,
            tracker_version=params.tracker_version,
            rule_id=rule_id,
            rule_version=rule_version,
            metadata=metadata,
        )
