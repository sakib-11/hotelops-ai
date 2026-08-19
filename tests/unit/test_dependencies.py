"""Unit tests for FastAPI dependencies (Task 6.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.dependencies import get_database, get_db_session, get_storage
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.state import app_state


class _FakeSessionContext:
    """Minimal async context manager standing in for `async with factory() as s:`."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session
        self.exited = False

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.exited = True
        return False


def _client_with_session(session: AsyncMock) -> DatabaseClient:
    """A real DatabaseClient whose session() runs against a mock factory."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    client = DatabaseClient(settings)
    factory = MagicMock()
    factory.side_effect = lambda: _FakeSessionContext(session)
    client._session_factory = factory  # type: ignore[assignment]
    return client


def _restore_database(saved) -> None:
    app_state.database = saved


class TestGetDatabase:
    async def test_requires_initialized_application(self) -> None:
        saved = app_state.database
        app_state.database = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                get_database()
        finally:
            _restore_database(saved)

    async def test_returns_database_client(self) -> None:
        saved = app_state.database
        fake = MagicMock()
        app_state.database = fake
        try:
            assert get_database() is fake
        finally:
            _restore_database(saved)


class TestGetStorage:
    async def test_requires_initialized_application(self) -> None:
        saved = app_state.storage
        app_state.storage = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                get_storage()
        finally:
            app_state.storage = saved

    async def test_returns_storage_port(self) -> None:
        saved = app_state.storage
        fake = MagicMock()
        app_state.storage = fake
        try:
            assert get_storage() is fake
        finally:
            app_state.storage = saved


class TestGetDbSession:
    """get_db_session wires the transaction-scoped DatabaseClient.session()."""

    async def test_commits_on_success(self) -> None:
        session = AsyncMock()
        saved = app_state.database
        app_state.database = _client_with_session(session)
        try:
            gen = get_db_session()
            yielded = await gen.__anext__()
            assert yielded is session
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
        finally:
            _restore_database(saved)

        session.commit.assert_awaited_once()
        session.rollback.assert_not_called()

    async def test_rolls_back_on_exception(self) -> None:
        session = AsyncMock()
        saved = app_state.database
        app_state.database = _client_with_session(session)
        try:
            gen = get_db_session()
            await gen.__anext__()
            with pytest.raises(RuntimeError, match="boom"):
                await gen.athrow(RuntimeError("boom"))
        finally:
            _restore_database(saved)

        session.rollback.assert_awaited_once()
        session.commit.assert_not_called()

    async def test_requires_initialized_application(self) -> None:
        saved = app_state.database
        app_state.database = None
        try:
            gen = get_db_session()
            with pytest.raises(RuntimeError, match="not initialized"):
                await gen.__anext__()
        finally:
            _restore_database(saved)
