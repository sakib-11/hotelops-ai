"""Application state — isolated module to prevent circular imports.

Contains the ApplicationState class and the singleton app_state instance.
No FastAPI or API route imports in this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.logging import configure_logging
from backend.app.infrastructure.redis.client import RedisClient
from backend.app.infrastructure.storage.client import StorageClient

if TYPE_CHECKING:
    from backend.app.infrastructure.health.service import ReadinessService

logger = logging.getLogger(__name__)


class ApplicationState:
    """Holds all application-level infrastructure instances."""

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.database: DatabaseClient | None = None
        self.redis: RedisClient | None = None
        self.storage: StorageClient | None = None
        self.readiness: ReadinessService | None = None

    async def initialize(self) -> None:
        """Initialize all infrastructure in correct order."""
        from backend.app.infrastructure.health.service import ReadinessService

        self.settings = Settings()  # type: ignore[call-arg]

        configure_logging(self.settings.log_level)
        logger.info(
            "Starting %s v%s (%s)",
            self.settings.app_name,
            self.settings.app_version,
            self.settings.app_env,
        )

        # Initialize infrastructure — order matters for dependencies
        self.database = DatabaseClient(self.settings)
        await self.database.initialize()
        logger.info("Database client initialized")

        self.redis = RedisClient(self.settings)
        await self.redis.initialize()
        logger.info("Redis client initialized")

        self.storage = StorageClient(self.settings)
        await self.storage.initialize()
        logger.info("Storage client initialized")

        self.readiness = ReadinessService(self.database, self.redis, self.storage)
        logger.info("Application state initialized")

    async def cleanup(self) -> None:
        """Clean up all resources in reverse initialization order."""
        logger.info("Shutting down application resources")

        if self.redis is not None:
            await self.redis.close()

        if self.storage is not None:
            await self.storage.close()

        if self.database is not None:
            await self.database.dispose()

        if self.settings is not None:
            logger.info(
                "%s v%s shutdown complete",
                self.settings.app_name,
                self.settings.app_version,
            )


# Singleton instance
app_state = ApplicationState()
