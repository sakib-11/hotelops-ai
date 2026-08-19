"""FastAPI routes for the configuration domain (Task 10.15).

Exposes the controlled lifecycle surface only:
  - create / clone / update DRAFT versions
  - start + run validation (DRAFT -> VALIDATING -> VALIDATED)
  - fetch validation results
  - publish a VALIDATED version (stale-safe, transactional)
  - resolve the exact configuration pinned to a video session

There is NO endpoint that mutates a published version or directly
manipulates status — every transition goes through the state machine
enforced by the domain service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.dependencies import get_db_session
from backend.app.domain.configuration.service import (
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    ConfigurationService,
)
from backend.app.infrastructure.auth.deps import get_actor_context, require_permission
from backend.app.infrastructure.observability.context import correlation_id
from contracts.common import ConfigurationVersionId, VideoSessionId
from contracts.configuration.api import (
    ConfigurationVersionResponse,
    DraftCloneRequest,
    DraftCreateRequest,
    DraftUpdateRequest,
    PublishResponse,
    SessionConfigurationResponse,
    ValidationRunResponse,
)
from contracts.identity import ActorContext, Permission

router = APIRouter(prefix="/configurations", tags=["Configuration"])


def _service(session: AsyncSession) -> ConfigurationService:
    """Build the configuration service.

    Spatial checks use the PostGIS-backed engine (the authoritative
    spatial implementation per ADR-010) bound to the request session;
    the deterministic pure-Python engine remains the offline/test
    fallback behind the same protocol.
    """
    from backend.app.domain.configuration.validation import ConfigurationValidationEngine
    from backend.app.infrastructure.spatial.engine import PostGISGeometryEngine

    validator = ConfigurationValidationEngine(spatial=PostGISGeometryEngine(session))
    return ConfigurationService(validation_engine=validator)


@router.post(
    "/drafts",
    response_model=ConfigurationVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new DRAFT configuration version for a venue",
)
async def create_draft(
    request: DraftCreateRequest,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_MANAGE)),
    session: AsyncSession = Depends(get_db_session),
) -> ConfigurationVersionResponse:
    """Create the venue configuration (if needed) and a new DRAFT version."""
    service = _service(session)
    version, _config = await service.create_draft(
        session=session,
        actor=actor,
        venue_id=request.venue_id,
        name=request.name,
        created_by=str(actor.actor_id),
        correlation_id=correlation_id(),
    )
    await session.commit()
    loaded = await service.get_version(session, actor, version.configuration_version_id)
    return ConfigurationVersionResponse.from_version(loaded)


@router.post(
    "/drafts/clone",
    response_model=ConfigurationVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Clone an existing version into a new DRAFT",
)
async def clone_draft(
    request: DraftCloneRequest,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_MANAGE)),
    session: AsyncSession = Depends(get_db_session),
) -> ConfigurationVersionResponse:
    """Create a new DRAFT from an existing published/draft version."""
    service = _service(session)
    version = await service.create_draft_from_version(
        session=session,
        actor=actor,
        source_version_id=request.source_version_id,
        created_by=str(actor.actor_id),
        correlation_id=correlation_id(),
    )
    await session.commit()
    loaded = await service.get_version(session, actor, version.configuration_version_id)
    return ConfigurationVersionResponse.from_version(loaded)


@router.put(
    "/versions/{version_id}",
    response_model=ConfigurationVersionResponse,
    summary="Replace the entity snapshot of a DRAFT version",
)
async def update_draft(
    version_id: ConfigurationVersionId,
    request: DraftUpdateRequest,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_MANAGE)),
    session: AsyncSession = Depends(get_db_session),
) -> ConfigurationVersionResponse:
    """Replace a DRAFT version's entities (state machine: DRAFT only)."""
    service = _service(session)
    await service.update_draft(
        session=session,
        actor=actor,
        version_id=version_id,
        cameras=request.cameras,
        zones=request.zones,
        tables=request.tables,
        entrances=request.entrances,
        queue_areas=request.queue_areas,
        service_areas=request.service_areas,
        privacy_rois=request.privacy_rois,
        exclusion_rois=request.exclusion_rois,
        correlation_id=correlation_id(),
    )
    await session.commit()
    loaded = await service.get_version(session, actor, version_id)
    return ConfigurationVersionResponse.from_version(loaded)


@router.get(
    "/versions/{version_id}",
    response_model=ConfigurationVersionResponse,
    summary="Get a configuration version snapshot",
)
async def get_version(
    version_id: ConfigurationVersionId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> ConfigurationVersionResponse:
    """Fetch one exact version snapshot (any lifecycle state)."""
    service = _service(session)
    loaded = await service.get_version(session, actor, version_id)
    if loaded is None:
        raise ConfigurationNotFoundError(f"Configuration version {version_id} not found")
    return ConfigurationVersionResponse.from_version(loaded)


@router.post(
    "/versions/{version_id}/validate",
    response_model=ValidationRunResponse,
    summary="Start and run deterministic validation (DRAFT -> VALIDATING -> VALIDATED)",
)
async def validate_version(
    version_id: ConfigurationVersionId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_MANAGE)),
    session: AsyncSession = Depends(get_db_session),
) -> ValidationRunResponse:
    """Run the deterministic validation engine and record the result."""
    service = _service(session)
    await service.start_validation(
        session=session, actor=actor, version_id=version_id, correlation_id=correlation_id()
    )
    version, result = await service.run_validation(
        session=session,
        actor=actor,
        version_id=version_id,
        validated_by=str(actor.actor_id),
        correlation_id=correlation_id(),
    )
    await session.commit()
    return ValidationRunResponse(
        configuration_version_id=version.configuration_version_id,
        status=version.status,
        valid=result.valid,
        result=result,
    )


@router.get(
    "/versions/{version_id}/validation",
    response_model=ValidationRunResponse,
    summary="Get the stored validation result for a version",
)
async def get_validation_result(
    version_id: ConfigurationVersionId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> ValidationRunResponse:
    """Return the last recorded validation result (revision-bound)."""
    service = _service(session)
    loaded = await service.get_version(session, actor, version_id)
    if loaded is None:
        raise ConfigurationNotFoundError(f"Configuration version {version_id} not found")
    if loaded.validation_result is None:
        raise ConfigurationConflictError(f"Version {version_id} has no validation result")
    return ValidationRunResponse(
        configuration_version_id=loaded.configuration_version_id,
        status=loaded.status,
        valid=loaded.validation_result.valid,
        result=loaded.validation_result,
    )


@router.post(
    "/versions/{version_id}/publish",
    response_model=PublishResponse,
    summary="Publish a VALIDATED version atomically (stale-safe, idempotent)",
)
async def publish_version(
    version_id: ConfigurationVersionId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VENUE_MANAGE)),
    session: AsyncSession = Depends(get_db_session),
) -> PublishResponse:
    """Publish — verifies state, exact revision, zero blocking errors."""
    service = _service(session)
    result = await service.publish(
        session=session,
        actor=actor,
        version_id=version_id,
        published_by=str(actor.actor_id),
        correlation_id=correlation_id(),
    )
    await session.commit()
    return PublishResponse(
        configuration_version_id=result.configuration_version_id,
        previous_published_version_id=result.previous_published_version_id,
        published_at=result.published_at,
    )


@router.get(
    "/sessions/{session_id}/configuration",
    response_model=SessionConfigurationResponse,
    summary="Resolve the exact published configuration pinned to a session",
)
async def resolve_session_configuration(
    session_id: VideoSessionId,
    actor: ActorContext = Depends(get_actor_context),
    _perm: None = Depends(require_permission(Permission.VIDEO_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> SessionConfigurationResponse:
    """Resolve the pinned configuration for a session.

    Never substitutes the latest published version — historical replay
    resolves the exact pinned snapshot.
    """
    service = _service(session)
    loaded = await service.resolve_session_configuration(session, actor, session_id)
    if loaded is None:
        raise ConfigurationNotFoundError(
            f"No pinned published configuration for session {session_id}"
        )
    return SessionConfigurationResponse.from_version(session_id, loaded)
