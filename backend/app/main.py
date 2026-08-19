"""HotelOps AI — FastAPI Application.

Application lifecycle managed through FastAPI lifespan.
Startup initializes all infrastructure clients.
Shutdown cleans up all resources deterministically.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend.app.api.router import api_router
from backend.app.application.services.media_errors import (
    MediaConflictError,
    MediaNotFoundError,
    MediaProtectedError,
    MediaValidationError,
)
from backend.app.application.services.operational_errors import OperationalNotFoundError
from backend.app.dependencies import get_settings
from backend.app.domain.configuration.service import (
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    ConfigurationStaleValidationError,
)
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.handler import (
    authentication_error_handler,
    authorization_error_handler,
)
from backend.app.infrastructure.observability.metrics import MetricsMiddleware
from backend.app.infrastructure.observability.middleware import RequestContextMiddleware
from backend.app.state import app_state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """Application lifespan — startup and shutdown."""
    # Startup
    await app_state.initialize()
    yield {"state": app_state}
    # Shutdown
    await app_state.cleanup()


app = FastAPI(
    title="HotelOps AI",
    version="0.1.0",
    lifespan=lifespan,
)

# Request context middleware — outermost, so every request (and any
# exception) carries request/correlation/trace context (Task 8.4).
app.add_middleware(RequestContextMiddleware)  # type: ignore[arg-type]

# Metrics middleware — records HTTP request counters/histograms when
# metrics are enabled (no-op otherwise). Added after the context
# middleware so it measures the full request lifecycle (Task 8.11).
app.add_middleware(MetricsMiddleware)  # type: ignore[arg-type]


def _media_error_response(status_code: int, detail: str) -> JSONResponse:
    """Build a JSON error response for a media lifecycle error."""
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _media_not_found_handler(request: Any, exc: MediaNotFoundError) -> JSONResponse:
    return _media_error_response(404, exc.detail)


def _media_conflict_handler(request: Any, exc: MediaConflictError) -> JSONResponse:
    return _media_error_response(409, exc.detail)


def _media_validation_handler(request: Any, exc: MediaValidationError) -> JSONResponse:
    return _media_error_response(422, exc.detail)


def _media_protected_handler(request: Any, exc: MediaProtectedError) -> JSONResponse:
    return _media_error_response(403, exc.detail)


# Register exception handlers — must be before routes.
app.add_exception_handler(AuthenticationError, authentication_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(AuthorizationError, authorization_error_handler)  # type: ignore[arg-type]

# Media lifecycle error handlers (Task 9) — 404 / 409 / 422 / 403.
app.add_exception_handler(MediaNotFoundError, _media_not_found_handler)  # type: ignore[arg-type]
app.add_exception_handler(MediaConflictError, _media_conflict_handler)  # type: ignore[arg-type]
app.add_exception_handler(MediaValidationError, _media_validation_handler)  # type: ignore[arg-type]
app.add_exception_handler(MediaProtectedError, _media_protected_handler)  # type: ignore[arg-type]


# Configuration domain error handlers (Task 10) — 404 / 409 / 422.
def _config_not_found_handler(request: Any, exc: ConfigurationNotFoundError) -> JSONResponse:
    return _media_error_response(404, str(exc))


def _config_conflict_handler(request: Any, exc: ConfigurationConflictError) -> JSONResponse:
    return _media_error_response(409, str(exc))


def _config_stale_handler(request: Any, exc: ConfigurationStaleValidationError) -> JSONResponse:
    return _media_error_response(409, str(exc))


app.add_exception_handler(ConfigurationNotFoundError, _config_not_found_handler)  # type: ignore[arg-type]
app.add_exception_handler(ConfigurationConflictError, _config_conflict_handler)  # type: ignore[arg-type]
app.add_exception_handler(  # type: ignore[arg-type]
    ConfigurationStaleValidationError, _config_stale_handler
)


# Operational vertical-slice error handlers (Task 18.12) — 404.
def _operational_not_found_handler(request: Any, exc: OperationalNotFoundError) -> JSONResponse:
    return _media_error_response(404, exc.detail)


app.add_exception_handler(  # type: ignore[arg-type]
    OperationalNotFoundError, _operational_not_found_handler
)

app.include_router(api_router)  # type: ignore[arg-type]


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint — service and build metadata."""
    settings = get_settings()
    result: dict[str, str] = {
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "status": "running",
    }
    if settings.build_commit:
        result["build_commit"] = settings.build_commit
    if settings.build_timestamp:
        result["build_timestamp"] = settings.build_timestamp
    return result
