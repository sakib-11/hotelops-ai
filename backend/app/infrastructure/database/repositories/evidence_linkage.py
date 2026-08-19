"""Evidence request linkage repository (Task 18.9).

Deduplicated persistence of event → evidence requests. The evidence
request row's primary key IS the deterministic, content-derived
``ref_id`` (Task 17.3 / Task 7 idempotency) — the SAME event always maps
to the SAME ``ref_id``, so the PK itself enforces one logical evidence
request per event.

``link`` uses the inbox dedup pattern (PostgreSQL ``ON CONFLICT DO
NOTHING`` over the primary key): a duplicate delivery inserts nothing
and the existing row is returned — replay and duplicate delivery both
collapse to the one logical request, never a second row.

The row is created in the durable REQUESTED state with the canonical
``EvidenceRef`` contract persisted on the JSONB ``evidence_request``
metadata key — the exact input the Task 17.11 evidence worker consumes
(``queue_pending`` admits REQUESTED/never-started rows).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.evidence import EvidenceRefModel


class EvidenceLinkageRepository:
    """Stateless data access for event → evidence request linkage."""

    async def link(
        self,
        session: AsyncSession,
        *,
        ref: EvidenceRefModel,
    ) -> EvidenceRefModel:
        """Insert the request row, deduplicating on the deterministic ref_id.

        Returns the inserted row, or the EXISTING row when the same
        ``ref_id`` was already linked (duplicate event delivery) — the
        caller can never observe a second logical request for one event.

        Raises:
            AssertionError: the conflicting row disappeared between the
                insert and the lookup (impossible under the row lock).
        """
        stmt = (
            insert(EvidenceRefModel)
            .values(
                ref_id=ref.ref_id,
                schema_version=ref.schema_version,
                tenant_id=ref.tenant_id,
                venue_id=ref.venue_id,
                ref_type=ref.ref_type,
                ref_uri=ref.ref_uri,
                event_time=ref.event_time,
                event_id=ref.event_id,
                session_id=ref.session_id,
                camera_id=ref.camera_id,
                # The mapped attribute name (``metadata_``): ``metadata``
                # collides with SQLAlchemy's MetaData descriptor on the
                # declarative class.
                metadata_=ref.metadata_,
            )
            .on_conflict_do_nothing(index_elements=["ref_id"])
            .returning(EvidenceRefModel)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        existing = await self.get_by_ref_id(session, ref.ref_id)
        if existing is None:
            msg = f"evidence request {ref.ref_id} conflicted but no existing row was found"
            raise AssertionError(msg)
        return existing

    async def get_by_ref_id(
        self,
        session: AsyncSession,
        ref_id: uuid.UUID | str,
    ) -> EvidenceRefModel | None:
        """Lookup the durable request row by its deterministic identity."""
        stmt = select(EvidenceRefModel).where(EvidenceRefModel.ref_id == ref_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_event_id(
        self,
        session: AsyncSession,
        event_id: uuid.UUID | str,
    ) -> EvidenceRefModel | None:
        """Lookup the durable request row by the material event it links.

        Audit/discovery convenience: one material event has at most one
        logical evidence request (the deterministic ``ref_id`` contract).
        """
        stmt = select(EvidenceRefModel).where(EvidenceRefModel.event_id == event_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = [
    "EvidenceLinkageRepository",
]
