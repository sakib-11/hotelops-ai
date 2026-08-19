"""Inbox service (Task 7 Phase 10).

Deduplicated ingress for inbound events. ``receive`` validates the
canonical EventEnvelope and inserts an inbox row keyed by
(source, source_message_id):

  - first delivery      → a new pending row is returned
  - duplicate delivery  → None is returned (the unique key rejected the
                          insert) and the business effect MUST NOT run

Tenant identity comes from the caller (the outbox publisher derived it
from the durable outbox row — never from the envelope payload), so a
client can never choose whose inbox a message lands in.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.audit_outbox_inbox import InboxMessageModel
from backend.app.infrastructure.database.repositories.inbox import InboxRepository
from contracts.events import EventEnvelope


class InboxService:
    """Writes deduplicated inbound messages into the caller's transaction."""

    async def receive(
        self,
        session: AsyncSession,
        *,
        source: str,
        envelope: EventEnvelope[Any],
        tenant_id: uuid.UUID,
        venue_id: uuid.UUID | None = None,
    ) -> InboxMessageModel | None:
        """Receive a validated event, deduplicating on (source, event_id).

        Args:
            session: The caller's transaction-scoped session.
            source: The logical source (e.g. 'outbox' for events relayed
                from the transactional outbox, or a partner name).
            envelope: The canonical EventEnvelope (validated by
                construction; re-validated here before persistence).
            tenant_id: The tenant the message belongs to — derived by
                the caller from trusted server-side state.
            venue_id: Optional venue context of the message.

        Returns:
            The new inbox row, or None if this message was already
            received (duplicate delivery — skip the effect).
        """
        # Contract validation before persistence: invalid envelopes
        # (bad UUIDs, naive timestamps, malformed payloads) never reach
        # the inbox.
        EventEnvelope[Any].model_validate(envelope.model_dump(mode="json"))

        return await InboxRepository(session).receive(
            source=source,
            source_message_id=str(envelope.event_id),
            tenant_id=tenant_id,
            venue_id=venue_id,
            event_type=envelope.event_type,
            payload=envelope.model_dump(mode="json"),
        )
