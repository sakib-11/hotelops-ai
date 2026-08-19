"""Event → Evidence integration (Task 18.9).

Connects a material Task 16 event to the durable Task 17 evidence
pipeline — the ONLY sanctioned place where an event becomes a durable
evidence request:

    Task 16 EventEnvelope
        → EvidenceRequestBuilder (Task 17.3 — deterministic EvidenceRef)
        → EvidenceRefModel row (REQUESTED — the Task 17.11 worker's queue)

The rules NEVER create evidence directly (the engine only DESCRIBES the
required request); this service performs the actual linkage, entirely
with Task 17 pieces: the builder (17.3), the canonical ``EvidenceRef``
contract (17.2), and the durable REQUESTED state the worker consumes
(17.10/17.11). No new queue or dedup architecture is introduced — the
request's primary key IS the content-derived ``ref_id`` (Task 7
idempotency), so one event always maps to one logical evidence request,
enforced by the PK itself.

Preserved on the linked request (the task's list): event_id, tenant,
venue, session, source (the envelope's producer), camera, event_time,
configuration_version, rule_version — all cross-validated by the
builder against the envelope's canonical payload.

The durable request contract is persisted on the ref's JSONB
``evidence_request`` metadata (``EVIDENCE_REQUEST_KEY``) exactly as the
evidence worker expects — the request row is immediately claimable by
the Task 17.11 pipeline.

STOP condition: if the event cannot be deterministically linked (unknown
event type, scope mismatch, missing source provenance, impossible
interval) the typed ``InvalidEvidenceRequestError`` is raised — evidence
is never linked to the wrong scope and never linked non-deterministically.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.evidence.state_machine import (
    EVIDENCE_PROCESSING_STATE_KEY,
    EvidenceProcessingState,
)
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.database.repositories.evidence_linkage import (
    EvidenceLinkageRepository,
)
from backend.app.infrastructure.database.repositories.evidence_work import (
    EVIDENCE_REQUEST_KEY,
)
from backend.app.intelligence.rules.evidence_request import (
    EvidenceRequestBuilder,
    EvidenceRequestParams,
    scope_params_from_envelope,
)
from backend.app.intelligence.rules.exceptions import InvalidEvidenceRequestError
from contracts.events import EventEnvelope, EvidenceRef
from contracts.rules import EvidenceRequirement

__all__ = ["EvidenceLinkageService"]


class EvidenceLinkageService:
    """Durable, idempotent EventEnvelope → evidence request linkage."""

    def __init__(
        self,
        *,
        builder: EvidenceRequestBuilder | None = None,
        repository: EvidenceLinkageRepository | None = None,
    ) -> None:
        self._builder = builder or EvidenceRequestBuilder()
        self._repository = repository or EvidenceLinkageRepository()

    async def link_event(
        self,
        session: AsyncSession,
        envelope: EventEnvelope[Any],
        *,
        params: EvidenceRequestParams | None = None,
        evidence_requirement: EvidenceRequirement = EvidenceRequirement.REQUIRED,
    ) -> EvidenceRefModel | None:
        """Link a material Task 16 event to its durable evidence request.

        Args:
            session: The caller's transaction-scoped session (the caller
                owns the commit — the request row commits with the rest
                of the event-handling transaction).
            envelope: The canonical Task 4/16 EventEnvelope (never
                modified).
            params: Optional caller-asserted scope; defaults to the
                scope derived from the envelope's canonical payload
                (``scope_params_from_envelope``).
            evidence_requirement: The rule's declared requirement. When
                ``NONE`` no request is produced and None is returned.

        Returns:
            The durable REQUESTED request row, or None when the rule
            declares no evidence requirement. A duplicate event returns
            the EXISTING row (one logical evidence request per event).

        Raises:
            InvalidEvidenceRequestError: the envelope cannot be linked
                deterministically (unknown event type, scope mismatch,
                missing source, impossible interval) — the STOP
                condition; evidence is never linked to the wrong scope.
        """
        if not isinstance(envelope, EventEnvelope):
            msg = f"expected a canonical EventEnvelope, got {type(envelope).__name__}"
            raise InvalidEvidenceRequestError(msg)
        scope = params if params is not None else scope_params_from_envelope(envelope)
        ref = self._builder.build(
            envelope,
            params=scope,
            evidence_requirement=evidence_requirement,
        )
        if ref is None:
            # The rule declares no evidence requirement — nothing to link.
            return None
        return await self._repository.link(
            session,
            ref=_to_request_row(ref),
        )


def _to_request_row(ref: EvidenceRef) -> EvidenceRefModel:
    """Map the canonical EvidenceRef request to its durable REQUESTED row.

    The request contract is persisted verbatim on ``evidence_request``
    — the evidence worker rebuilds its input from exactly this key, so
    the linked row is immediately claimable (never re-derived, never
    re-built).
    """
    if ref.tenant_id is None or ref.venue_id is None:
        msg = "linked evidence request lacks tenant/venue scope"
        raise InvalidEvidenceRequestError(msg)
    return EvidenceRefModel(
        ref_id=uuid.UUID(str(ref.ref_id)),
        schema_version=ref.schema_version,
        tenant_id=uuid.UUID(str(ref.tenant_id)),
        venue_id=uuid.UUID(str(ref.venue_id)),
        ref_type=ref.ref_type.value,
        ref_uri=ref.ref_uri,
        event_time=ref.event_time,
        event_id=uuid.UUID(str(ref.event_id)),
        session_id=(
            uuid.UUID(str(ref.video_session_id)) if ref.video_session_id is not None else None
        ),
        camera_id=uuid.UUID(str(ref.camera_id)) if ref.camera_id is not None else None,
        metadata_={
            EVIDENCE_PROCESSING_STATE_KEY: EvidenceProcessingState.REQUESTED.value,
            EVIDENCE_REQUEST_KEY: ref.model_dump(mode="json"),
        },
    )
