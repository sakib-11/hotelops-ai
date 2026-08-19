"""FastAPI routes for live video session management (Task 19.2).

Provides REST API for live session lifecycle:
- POST /live/sessions/start - Start a new live session
- POST /live/sessions/{session_id}/stop - Stop a live session
- GET /live/sessions/{session_id} - Get session status
- GET /live/sessions/{session_id}/transitions - Get transition history
- GET /live/sessions - List sessions (with filters)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.live_session import (
    LiveSessionCreateRequest,
    LiveSessionStatusResponse,
    LiveVideoSessionService,
)
from backend.app.dependencies import get_db_session
from backend.app.infrastructure.auth.deps import get_actor_context
from backend.app.infrastructure.observability.context import correlation_id
from contracts.common import CameraId, TenantId, VenueId, VideoSessionId
from contracts.identity import ActorContext
from contracts.video.models import LiveVideoSessionStatus

router = APIRouter(prefix="/live/sessions", tags=["Live Sessions"])


@router.post(
    "/start",
    response_model=LiveSessionStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new live video session",
)
async def start_live_session(
    request: LiveSessionCreateRequest,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
) -> LiveSessionStatusResponse:
    """Create and start a new live video session for a camera.

    The session begins in CONNECTING state. The ingestion worker will
    transition it to ACTIVE once the RTSP connection is established.
    """
    service = LiveVideoSessionService()

    session_model = await service.create_session(
        session=session,
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        request=request,
        correlation_id=correlation_id(),
    )

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
        last_transition_time=session_model.started_at,
        last_transition_reason="Session created, awaiting RTSP connection",
        metadata=session_model.metadata_,
    )


@router.post(
    "/{session_id}/stop",
    response_model=LiveSessionStatusResponse,
    summary="Stop a live video session",
)
async def stop_live_session(
    session_id: VideoSessionId,
    reason: str = Query(default="Manual stop requested", description="Reason for stopping"),
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
) -> LiveSessionStatusResponse:
    """Stop a live video session (idempotent).

    Transitions the session to STOPPED state from any current state.
    Safe to call multiple times.
    """
    service = LiveVideoSessionService()

    result = await service.request_stop(
        session=session,
        session_id=session_id,
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        reason=reason,
        correlation_id=correlation_id(),
    )

    if not result.success:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail=result.error or "Transition failed")

    # Return updated status
    status_response = await service.get_session_status(
        session=session,
        session_id=session_id,
        tenant_id=actor.tenant_id,
    )
    if status_response is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Session not found after transition")

    return status_response


@router.get(
    "/{session_id}",
    response_model=LiveSessionStatusResponse,
    summary="Get live session status",
)
async def get_live_session_status(
    session_id: VideoSessionId,
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
) -> LiveSessionStatusResponse:
    """Get current status and last transition info for a live session."""
    service = LiveVideoSessionService()

    status_response = await service.get_session_status(
        session=session,
        session_id=session_id,
        tenant_id=actor.tenant_id,
    )

    if status_response is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Live session not found")

    return status_response


@router.get(
    "/{session_id}/transitions",
    response_model=list[dict[str, Any]],
    summary="Get live session transition history",
)
async def get_live_session_transitions(
    session_id: VideoSessionId,
    limit: int = Query(default=100, ge=1, le=500, description="Maximum transitions to return"),
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Get the state transition history for a live session (most recent first)."""
    service = LiveVideoSessionService()

    transitions = await service.get_transition_history(
        session=session,
        session_id=session_id,
        tenant_id=actor.tenant_id,
        limit=limit,
    )

    return [
        {
            "transition_id": str(t.transition_id),
            "previous_state": t.previous_state,
            "new_state": t.new_state,
            "transition_time": t.transition_time.isoformat(),
            "reason": t.reason,
            "source": t.source,
            "correlation_id": t.correlation_id,
            "actor_id": str(t.actor_id) if t.actor_id else None,
        }
        for t in transitions
    ]


@router.get(
    "",
    response_model=list[LiveSessionStatusResponse],
    summary="List live video sessions",
)
async def list_live_sessions(
    camera_id: CameraId | None = Query(default=None, description="Filter by camera"),
    venue_id: VenueId | None = Query(default=None, description="Filter by venue"),
    status: LiveVideoSessionStatus | None = Query(default=None, description="Filter by status"),
    actor: ActorContext = Depends(get_actor_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[LiveSessionStatusResponse]:
    """List live video sessions with optional filters."""
    service = LiveVideoSessionService()

    # Get sessions from repository
    sessions = await service._session_repo.get_active_live_sessions(
        session=session,
        tenant_id=actor.tenant_id,
        venue_id=venue_id,
    )

    # Filter by camera if specified
    if camera_id:
        sessions = [s for s in sessions if s.camera_id == camera_id]

    # Filter by status if specified
    if status:
        sessions = [s for s in sessions if s.status == status.value]

    # Build response for each session
    responses = []
    for s in sessions:
        # Get last transition for each session
        last_transition = await service.get_transition_history(
            session=session,
            session_id=VideoSessionId(s.session_id),
            tenant_id=actor.tenant_id,
            limit=1,
        )
        lt = last_transition[0] if last_transition else None

        responses.append(
            LiveSessionStatusResponse(
                session_id=VideoSessionId(s.session_id),
                camera_id=CameraId(s.camera_id) if s.camera_id else None,
                venue_id=VenueId(s.venue_id),
                tenant_id=TenantId(s.tenant_id),
                status=LiveVideoSessionStatus(s.status),
                started_at=s.started_at,
                ended_at=s.ended_at,
                last_transition_time=lt.transition_time if lt else None,
                last_transition_reason=lt.reason if lt else None,
                metadata=s.metadata_,
            )
        )

    return responses