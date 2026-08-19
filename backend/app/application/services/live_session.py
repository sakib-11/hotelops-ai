"""Live Video Session Service (Task 19.2).

Orchestrates the live video session lifecycle with the FSM:
- Session creation with CONNECTING state
- State transitions via the LiveVideoSessionStateMachine
- Persistence of transition audit log
- Integration with VideoSessionModel (DB) and RTSPFrameSource (ingestion)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.video.live_session_state_machine import (
    LiveSessionTerminalError,
    LiveSessionTransitionError,
    LiveVideoSessionStateMachine,
    LiveSessionTransitionRecord,
    TransitionResult,
)
from backend.app.infrastructure.database.models.video import (
    LiveSessionTransitionLogModel,
    VideoSessionModel,
)
from backend.app.infrastructure.database.repositories.video import VideoSessionRepository
from contracts.common import CameraId, TenantId, VenueId, VideoSessionId
from contracts.video.models import LiveVideoSessionStatus, SourceType


@dataclass(frozen=True, slots=True)
class LiveSessionCreateRequest:
    """Request to create a new live video session."""

    camera_id: CameraId
    venue_id: VenueId
    configuration_version_id: uuid.UUID | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LiveSessionStatusResponse:
    """Current status of a live video session."""

    session_id: VideoSessionId
    camera_id: CameraId | None
    venue_id: VenueId
    tenant_id: TenantId
    status: LiveVideoSessionStatus
    started_at: datetime
    ended_at: datetime | None
    last_transition_time: datetime | None
    last_transition_reason: str | None
    metadata: dict[str, Any] | None


class LiveVideoSessionService:
    """Service for managing live video session lifecycle with FSM enforcement."""

    def __init__(
        self,
        *,
        session_repository: VideoSessionRepository | None = None,
    ) -> None:
        self._session_repo = session_repository or VideoSessionRepository()

    async def create_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: TenantId,
        actor_id: uuid.UUID,
        request: LiveSessionCreateRequest,
        correlation_id: str | None = None,
    ) -> VideoSessionModel:
        """
        Create a new live video session in CONNECTING state.

        The session starts in CONNECTING and the ingestion worker will
        transition it to ACTIVE once the RTSP connection is established.
        """
        # Create the video session row
        session_model = await self._session_repo.create(
            session=session,
            tenant_id=tenant_id,
            venue_id=request.venue_id,
            source_type=SourceType.LIVE,
            camera_id=request.camera_id,
            asset_id=None,
            configuration_version_id=request.configuration_version_id,
            status=LiveVideoSessionStatus.CONNECTING.value,
            started_at=datetime.now(UTC),
            metadata=request.metadata,
        )

        # Log the initial CONNECTING state as a transition from "none"
        transition_log = LiveSessionTransitionLogModel(
            session_id=session_model.session_id,
            venue_id=request.venue_id,
            tenant_id=tenant_id,
            previous_state="none",  # Special initial state
            new_state=LiveVideoSessionStatus.CONNECTING.value,
            transition_time=datetime.now(UTC),
            reason="Session created, awaiting RTSP connection",
            source="system",
            correlation_id=correlation_id,
            actor_id=actor_id,
        )
        session.add(transition_log)

        return session_model

    async def transition_state(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        operation: str,
        reason: str,
        source: str,  # "system" or "actor"
        correlation_id: str | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> TransitionResult:
        """
        Execute a state transition with FSM validation and audit logging.

        This is the single entry point for all live session state changes.
        """
        # Load current session
        session_model = await self._session_repo.get_for_actor(
            session, tenant_id, session_id
        )
        if session_model is None:
            raise ValueError(f"Session {session_id} not found")

        if session_model.source_type != SourceType.LIVE.value:
            raise ValueError(f"Session {session_id} is not a live session")

        current_state = LiveVideoSessionStatus(session_model.status)

        # Execute transition through FSM
        try:
            result = LiveVideoSessionStateMachine.transition(
                current=current_state,
                operation=operation,
                session_id=str(session_id),
                reason=reason,
                source=source,
                correlation_id=correlation_id,
                actor_id=str(actor_id) if actor_id else None,
            )
        except (LiveSessionTransitionError, LiveSessionTerminalError) as exc:
            return TransitionResult(success=False, error=str(exc))

        if not result.success or result.new_state is None:
            return result

        # Update session model
        session_model.status = result.new_state.value
        if result.new_state in (LiveVideoSessionStatus.STOPPED, LiveVideoSessionStatus.FAILED):
            session_model.ended_at = datetime.now(UTC)
        await session.flush()

        # Persist transition log
        if result.transition_record:
            log_entry = LiveSessionTransitionLogModel(
                session_id=session_model.session_id,
                venue_id=session_model.venue_id,
                tenant_id=tenant_id,
                previous_state=result.transition_record.previous_state.value,
                new_state=result.transition_record.new_state.value,
                transition_time=result.transition_record.transition_time,
                reason=result.transition_record.reason,
                source=result.transition_record.source,
                correlation_id=result.transition_record.correlation_id,
                actor_id=result.transition_record.actor_id
                if isinstance(result.transition_record.actor_id, uuid.UUID)
                else uuid.UUID(result.transition_record.actor_id)
                if result.transition_record.actor_id
                else None,
            )
            session.add(log_entry)

        return result

    async def get_session_status(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
    ) -> LiveSessionStatusResponse | None:
        """Get current status of a live session with last transition info."""
        session_model = await self._session_repo.get_for_actor(
            session, tenant_id, session_id
        )
        if session_model is None:
            return None

        # Get last transition
        stmt = (
            select(LiveSessionTransitionLogModel)
            .where(
                LiveSessionTransitionLogModel.session_id == session_id,
                LiveSessionTransitionLogModel.tenant_id == tenant_id,
            )
            .order_by(LiveSessionTransitionLogModel.transition_time.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        last_transition = result.scalar_one_or_none()

        return LiveSessionStatusResponse(
            session_id=VideoSessionId(session_model.session_id),
            camera_id=CameraId(session_model.camera_id)
            if session_model.camera_id
            else None,
            venue_id=VenueId(session_model.venue_id),
            tenant_id=TenantId(session_model.tenant_id),
            status=LiveVideoSessionStatus(session_model.status),
            started_at=session_model.started_at,
            ended_at=session_model.ended_at,
            last_transition_time=last_transition.transition_time
            if last_transition
            else None,
            last_transition_reason=last_transition.reason if last_transition else None,
            metadata=session_model.metadata_,
        )

    async def get_transition_history(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        limit: int = 100,
    ) -> list[LiveSessionTransitionLogModel]:
        """Get transition history for a session (most recent first)."""
        stmt = (
            select(LiveSessionTransitionLogModel)
            .where(
                LiveSessionTransitionLogModel.session_id == session_id,
                LiveSessionTransitionLogModel.tenant_id == tenant_id,
            )
            .order_by(LiveSessionTransitionLogModel.transition_time.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # Convenience methods for common transitions

    async def mark_connected(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark session as connected (CONNECTING -> ACTIVE)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="connected",
            reason="RTSP connection established, frames flowing",
            source="system",
            correlation_id=correlation_id,
        )

    async def mark_connection_failed(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        reason: str,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark connection as failed (CONNECTING -> FAILED)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="connection_failed",
            reason=reason,
            source="system",
            correlation_id=correlation_id,
        )

    async def mark_stale_detected(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        stale_duration_seconds: float,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark stream as stale (ACTIVE -> DEGRADED)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="stale_detected",
            reason=f"No frames received for {stale_duration_seconds:.1f}s",
            source="system",
            correlation_id=correlation_id,
        )

    async def mark_reconnecting(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark session as reconnecting (DEGRADED -> RECONNECTING)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="reconnecting",
            reason="Attempting RTSP reconnection",
            source="system",
            correlation_id=correlation_id,
        )

    async def mark_reconnected(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark session as reconnected (RECONNECTING -> ACTIVE)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="reconnected",
            reason="RTSP reconnection successful, frames flowing",
            source="system",
            correlation_id=correlation_id,
        )

    async def mark_reconnect_failed(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        reason: str,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark reconnection as failed (RECONNECTING -> DEGRADED)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="reconnect_failed",
            reason=reason,
            source="system",
            correlation_id=correlation_id,
        )

    async def mark_fatal_error(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        reason: str,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Mark session as fatally failed (ACTIVE/DEGRADED/RECONNECTING -> FAILED)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="fatal_error",
            reason=reason,
            source="system",
            correlation_id=correlation_id,
        )

    async def request_stop(
        self,
        session: AsyncSession,
        *,
        session_id: VideoSessionId,
        tenant_id: TenantId,
        actor_id: uuid.UUID,
        reason: str = "Manual stop requested",
        correlation_id: str | None = None,
    ) -> TransitionResult:
        """Request session stop (any state -> STOPPED, idempotent)."""
        return await self.transition_state(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            operation="stop_requested",
            reason=reason,
            source="actor",
            correlation_id=correlation_id,
            actor_id=actor_id,
        )