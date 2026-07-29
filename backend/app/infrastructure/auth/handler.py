"""FastAPI exception handlers for authentication errors.

Maps domain exceptions to correct HTTP status codes:
  - AuthenticationError → 401 UNAUTHORIZED
  - AuthorizationError  → 403 FORBIDDEN
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)


async def authentication_error_handler(
    request: Request,
    exc: AuthenticationError,
) -> JSONResponse:
    """Handle AuthenticationError → 401 UNAUTHORIZED."""
    return JSONResponse(
        status_code=401,
        content={"detail": exc.detail},
    )


async def authorization_error_handler(
    request: Request,
    exc: AuthorizationError,
) -> JSONResponse:
    """Handle AuthorizationError → 403 FORBIDDEN."""
    return JSONResponse(
        status_code=403,
        content={"detail": exc.detail},
    )
