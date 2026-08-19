"""FastAPI dependency injection functions.

Separated from main.py to avoid circular imports with API routes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.health.service import ReadinessService
from backend.app.infrastructure.storage.protocol import StoragePort
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


def get_database() -> DatabaseClient:
    """FastAPI dependency — returns the database client."""
    if app_state.database is None:
        msg = "Application not initialized"
        raise RuntimeError(msg)
    return app_state.database


def get_storage() -> StoragePort:
    """FastAPI dependency — returns the active storage port."""
    if app_state.storage is None:
        msg = "Application not initialized"
        raise RuntimeError(msg)
    return app_state.storage


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — transaction-scoped database session.

    Committed on success, rolled back on exception, always closed.
    A broken session is never reused (see DatabaseClient.session).
    """
    async with get_database().session() as session:
        yield session
