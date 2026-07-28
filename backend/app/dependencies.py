"""FastAPI dependency injection functions.

Separated from main.py to avoid circular imports with API routes.
"""

from __future__ import annotations

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.health.service import ReadinessService
from backend.app.state import app_state


def get_settings() -> Settings:
    """FastAPI dependency — returns application settings."""
    if app_state.settings is None:
        msg = "Application not initialized"
        raise RuntimeError(msg)
    return app_state.settings


def get_readiness_service() -> ReadinessService:
    """FastAPI dependency — returns the readiness service."""
    if app_state.readiness is None:
        msg = "Application not initialized"
        raise RuntimeError(msg)
    return app_state.readiness
