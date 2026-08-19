"""PostgreSQL database infrastructure boundary.

SQLAlchemy async engine lifecycle and the transaction-scoped session
unit of work. No ORM business models or schema creation in this module.

Session lifecycle (see DatabaseClient.session):
    create -> (work) -> commit on success / rollback on failure -> close
A broken session is never silently reused — every session() call creates
and closes a fresh session.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing

logger = logging.getLogger(__name__)


class DatabaseClient:
    """PostgreSQL/TimescaleDB infrastructure client.

    Manages the SQLAlchemy async engine lifecycle and provides the
    transaction-scoped session unit of work (see session()).
    Created during application startup and disposed on shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

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
        self._session_factory = async_sessionmaker[AsyncSession](
            bind=self._engine,
            expire_on_commit=False,
        )
        logger.info("Database engine initialized")
        return self

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """The async session factory bound to the engine.

        Raises:
            RuntimeError: If the client has not been initialized.
        """
        if self._session_factory is None:
            msg = "DatabaseClient is not initialized"
            raise RuntimeError(msg)
        return self._session_factory

    def is_initialized(self) -> bool:
        """True once the engine has been created (lazy — before first connect)."""
        return self._engine is not None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transaction-scoped unit of work.

        Yields a fresh :class:`AsyncSession`. On normal exit the transaction
        is committed; on any exception it is rolled back and re-raised. The
        session is always closed, and a broken session is never reused —
        every call creates a new session.

        Callers ``flush()`` changes into the transaction; the context manager
        owns commit/rollback.

        Raises:
            RuntimeError: If the client has not been initialized.
        """
        # Transaction-scoped span (no-op when tracing is disabled): one
        # span per unit of work, so callers see where DB time goes.
        async with tracing.db_span("db.session") as _, self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "Session rollback failed after an error; "
                        "the session will be closed and never reused"
                    )
                raise

    async def check_connectivity(self) -> bool:
        """Execute a minimal connectivity check.

        Returns True if the database is reachable, False otherwise.
        """
        if self._engine is None:
            return False
        async with tracing.db_span("db.check_connectivity") as _:
            try:
                async with self._engine.connect() as conn:
                    result = await conn.execute(text("SELECT 1"))
                    row: int = result.scalar_one()
                    return bool(row == 1)
            except Exception as exc:
                # Routine probe — expected to fail during startup while the
                # database is unreachable; log at warning level, not exception.
                logger.warning("Database connectivity check failed: %s", exc)
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
