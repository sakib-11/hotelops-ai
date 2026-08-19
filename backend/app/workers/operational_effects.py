"""Operational effect handlers — async outbox processing (Task 18.11).

Connects the OUTBOX EVENT persisted by the authoritative persistence
boundary (Task 18.10) to the EXISTING Task 7 worker — the inbox
consumer's registered effect handlers. This is the ONLY slice-specific
wiring: the ``OutboxPublisherWorker`` (lease → publish) and the
``InboxIngressBridge`` (Redis → inbox, deduplicated) are generic Task 7
machinery and need no changes.

    PostgreSQL outbox (18.10 COMMIT)
        → OutboxPublisherWorker  (lease → publish → published)
        → Redis stream           (ADR-004: transport, not truth)
        → InboxIngressBridge     (dedup on (source, event_id) → pending)
        → InboxConsumerWorker    (claim → THIS effect + processed, atomic)

The business effect of a delivered operational event is the durable
evidence request — the Task 18.9 ``EvidenceLinkageService``. The rule
never creates evidence and the boundary never creates evidence: the
DELIVERED event is what triggers the evidence pipeline (Task 17), so
"event generated → evidence request generated" completes asynchronously
without coupling the rule engine to evidence creation.

Idempotency / exactly-once-effect guarantees (Task 7):

- at-least-once delivery (a publisher crash re-publishes) is collapsed
  by the inbox's unique (source, source_message_id) key — one inbox row;
- the effect itself is idempotent: the evidence request's primary key IS
  the content-derived ``ref_id`` (Task 7 idempotency), so even a
  duplicate inbox row would produce ONE logical evidence request;
- the effect runs INSIDE the consumer's transaction with
  ``mark_processed`` — an inbox row is never marked processed before the
  evidence request is safely committed;
- the outbox row is the durability boundary: an event committed by 18.10
  is NEVER lost — every failure path keeps the row and retries or
  dead-letters (preserved, never deleted).

The handler only ever acts on the canonical EventEnvelope carried in the
row payload (the outbox payload IS the serialized envelope) — the
evidence builder re-validates the payload contract, so a corrupted or
non-canonical event fails deterministically instead of linking evidence
to the wrong scope (the Task 18.9 STOP condition).
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.application.services.evidence_linkage import EvidenceLinkageService
from backend.app.infrastructure.database.models.audit_outbox_inbox import InboxMessageModel
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.observability.metrics import (
    PIPELINE_METRIC_WORKER,
    record_pipeline_metric,
)
from backend.app.workers.inbox_consumer import EffectHandler
from contracts.events import EventEnvelope
from contracts.rules import RuleEventType

logger = logging.getLogger(__name__)

__all__ = [
    "OPERATIONAL_EFFECT_TYPES",
    "build_operational_effect_handlers",
]

# The controlled event-type vocabulary whose delivered events carry a
# business effect. Each registered event type is guaranteed to have a
# deterministic effect (the extension point for future slice rules).
OPERATIONAL_EFFECT_TYPES: frozenset[str] = frozenset({
    RuleEventType.OCCUPANCY_SESSION.value,
})


def build_operational_effect_handlers(
    evidence_linkage: EvidenceLinkageService | None = None,
) -> dict[str, EffectHandler]:
    """The slice's effect-handler registry for the Task 7 inbox consumer.

    Returns a mapping of ``event_type → EffectHandler`` suitable for
    ``InboxConsumerWorker(effect_handlers=...)``. The handler runs inside
    the consumer's transaction (the worker owns the commit): it rebuilds
    the canonical envelope from the durable outbox payload and links the
    durable evidence request (Task 18.9). The linkage is idempotent —
    one logical evidence request per event, even under at-least-once
    redelivery.

    Args:
        evidence_linkage: Optional Task 18.9 service (injectable for
            tests); defaults to the production service.
    """
    service = evidence_linkage or EvidenceLinkageService()
    handlers: dict[str, EffectHandler] = {}
    for event_type in OPERATIONAL_EFFECT_TYPES:
        handlers[event_type] = _evidence_effect_for(service, event_type)
    return handlers


def _evidence_effect_for(
    service: EvidenceLinkageService,
    event_type: str,
) -> EffectHandler:
    """Bind the evidence linkage effect to one registered event type."""

    async def effect(
        session: Any,
        inbox_row: InboxMessageModel,
    ) -> None:
        # The outbox payload IS the canonical EventEnvelope serialized at
        # enqueue time — the envelope is rebuilt, never re-derived.
        envelope = EventEnvelope[Any].model_validate(inbox_row.payload)
        # Deterministic linkage: the request's PK is the content-derived
        # ref_id, so a duplicate delivery collapses to the existing row
        # (one logical evidence request per event). Raises
        # InvalidEvidenceRequestError when the event cannot be linked
        # deterministically — the worker dead-letters it with the reason.
        request_row = await service.link_event(session, envelope)
        # Task 18.18 — worker effect telemetry: one pipeline counter and
        # a structured record carrying the full slice correlation scope
        # (tenant/venue/session/camera/source/event/evidence + rule and
        # configuration provenance), so logs reconstruct the slice.
        record_pipeline_metric(PIPELINE_METRIC_WORKER)
        scope = _effect_scope(envelope, request_row)
        logger.info(
            "operational effect applied: event_id=%s event_type=%s evidence_id=%s "
            "tenant_id=%s venue_id=%s session_id=%s camera_id=%s source_id=%s "
            "rule_id=%s rule_version=%s configuration_version_id=%s correlation_id=%s",
            envelope.event_id,
            event_type,
            scope["evidence_id"],
            scope["tenant_id"],
            scope["venue_id"],
            scope["session_id"],
            scope["camera_id"],
            scope["source_id"],
            scope["rule_id"],
            scope["rule_version"],
            scope["configuration_version_id"],
            scope["correlation_id"],
        )

    return effect


def _effect_scope(
    envelope: EventEnvelope[Any],
    request_row: EvidenceRefModel | None,
) -> dict[str, str | None]:
    """The correlation scope of one applied operational effect.

    Every identity the Task 18.18 log contract requires, taken from the
    canonical envelope (contract-validated at enqueue) and the durable
    evidence request row. Values are stringified for stable structured
    logging; absent values stay None.
    """
    payload = envelope.payload
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    elif not isinstance(payload, dict):
        payload = {}
    return {
        "tenant_id": _as_str(payload.get("tenant_id")),
        "venue_id": _as_str(payload.get("venue_id")),
        "session_id": _as_str(payload.get("session_id")),
        "camera_id": _as_str(payload.get("camera_id")),
        "rule_id": _as_str(payload.get("rule_id")),
        "rule_version": _as_str(payload.get("rule_version")),
        "configuration_version_id": _as_str(payload.get("configuration_version_id")),
        "source_id": _as_str(envelope.source),
        "correlation_id": _as_str(envelope.correlation_id),
        "evidence_id": _as_str(request_row.ref_id) if request_row is not None else None,
    }


def _as_str(value: object) -> str | None:
    """Coerce an identifier to its stable string form (None stays None)."""
    return str(value) if value is not None else None
