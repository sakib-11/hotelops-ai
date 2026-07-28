"""PostgreSQL database infrastructure boundary.

SQLAlchemy async engine lifecycle managed through the application lifespan.
No ORM business models or schema creation in this module.
"""

from __future__ import annotations

import logging
from typing import Any, Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from backend.app.infrastructure.config import Settings

logger = logging.getLogger(__name__)


class DatabaseClient:
    """PostgreSQL/TimescaleDB infrastructure client.

    Manages the SQLAlchemy async engine lifecycle.
    Created during application startup and disposed on shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[Any] | None = None

    async def initialize(self) -> Self:
        """Create the database engine with pool configuration.

        Call during application startup.
        """
        if self._engine is not None:
            logger.warning("Database engine already initialized")
            return self

        self._engine = create_async_engine(
            self._settings.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )
        self._session_factory = async_sessionmaker[Any](
            bind=self._engine,
            expire_on_commit=False,
        )
        logger.info("Database engine initialized")
        return self

    async def check_connectivity(self) -> bool:
        """Execute a minimal connectivity check.

        Returns True if the database is reachable, False otherwise.
        """
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                row: int = result.scalar_one()
                return bool(row == 1)
        except Exception:
            logger.exception("Database connectivity check failed")
            return False

    async def dispose(self) -> None:
        """Dispose of the database engine.

        Call during application shutdown.
        """
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("Database engine disposed")
