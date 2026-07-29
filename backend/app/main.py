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

from backend.app.api.router import api_router
from backend.app.dependencies import get_settings
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.handler import (
    authentication_error_handler,
    authorization_error_handler,
)
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

# Register auth exception handlers — must be before routes
app.add_exception_handler(AuthenticationError, authentication_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(AuthorizationError, authorization_error_handler)  # type: ignore[arg-type]

app.include_router(api_router)  # type: ignore[arg-type]


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint — basic service information."""
    settings = get_settings()
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "status": "running",
    }
