"""Health check endpoints — liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette import status

from backend.app.dependencies import get_readiness_service, get_settings
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.health.models import HealthResponse, ReadinessResponse
from backend.app.infrastructure.health.service import ReadinessService

router: APIRouter = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Liveness check.

    Returns 200 if the application process is alive.
    Does NOT check external dependencies.
    """
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readiness(
    readiness: ReadinessService = Depends(get_readiness_service),
) -> ReadinessResponse:
    """Readiness check.

    Returns 200 with status=ready when all mandatory dependencies are healthy.
    Returns 503 with status=not_ready when any dependency is unavailable.
    The HTTP status code mirrors the overall readiness state.
    """
    result = await readiness.check_readiness()

    if result.status.value == "not_ready":
        return JSONResponse(  # type: ignore[return-value]
            content=result.model_dump(),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return result
