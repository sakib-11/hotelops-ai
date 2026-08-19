"""Integration tests for the Task 6.2 database foundation.

Exercises the real DatabaseClient (async engine + transaction-scoped
session unit of work) against scratch PostgreSQL/TimescaleDB databases:

  - engine creation and connectivity
  - transaction commit persists / rollback discards
  - a broken session is never reused
  - database connection failure handling
  - Alembic single-configuration URL resolution ($DATABASE_URL)

Gated by INTEGRATION_TESTS=1 (same convention as the other integration tests).

Run:
    docker compose -f infrastructure/docker/compose.yaml up -d postgres
    INTEGRATION_TESTS=1 pytest tests/integration/test_database_lifecycle.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "database" / "alembic.ini"

# Admin (bypass) connection — must be a superuser able to CREATE/DROP databases.
_ADMIN_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hotelops:CHANGE_ME@localhost:5433/hotelops",
)

_requires_postgres = pytest.mark.skipif(
    not os.environ.get("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 and start PostgreSQL "
    "(docker compose -f infrastructure/docker/compose.yaml up -d postgres)",
)

pytestmark = [pytest.mark.integration, _requires_postgres]


# =============================================================================
# Scratch database lifecycle (self-contained helpers)
# =============================================================================


def _admin_connect_kwargs(database: str) -> dict[str, str | int]:
    """Connection keyword arguments for the admin role (asyncpg)."""
    url = make_url(_ADMIN_URL)
    assert url.username is not None and url.password is not None
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "database": database,
    }


async def _admin_execute(sql: str) -> None:
    conn = await asyncpg.connect(**_admin_connect_kwargs("postgres"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _drop_database(name: str) -> None:
    """Drop a scratch database, tolerating briefly lingering connections."""
    conn = await asyncpg.connect(**_admin_connect_kwargs("postgres"))
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
    finally:
        await conn.close()
    for attempt in range(3):
        try:
            await _admin_execute(f'DROP DATABASE IF EXISTS "{name}"')
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(0.3)


def _settings_for_db(name: str) -> Settings:
    """Settings pointing at a scratch database (single configuration system)."""
    url = make_url(_ADMIN_URL)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        POSTGRES_HOST=url.host or "localhost",
        POSTGRES_PORT=url.port or 5432,
        POSTGRES_DB=name,
        POSTGRES_USER=url.username,
        POSTGRES_PASSWORD=url.password,
    )


@pytest_asyncio.fixture
async def scratch_db():
    """A unique, empty scratch database, dropped again after the test."""
    name = f"hotelops_6203_{uuid.uuid4().hex[:8]}"
    await _admin_execute(f'DROP DATABASE IF EXISTS "{name}"')
    await _admin_execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        await _drop_database(name)


async def _scaffold(database: str) -> None:
    """Create a test-local table (scratch scaffolding, not a domain table)."""
    engine = create_async_engine(_settings_for_db(database).database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE lifecycle_test (id integer PRIMARY KEY, val text NOT NULL)")
            )
    finally:
        await engine.dispose()


async def _read_count(database: str) -> int:
    engine = create_async_engine(_settings_for_db(database).database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT count(*) FROM lifecycle_test"))).scalar_one()
    finally:
        await engine.dispose()


# =============================================================================
# Engine + session lifecycle (real asyncpg)
# =============================================================================


class TestEngineAndSessionLifecycle:
    async def test_engine_creation_and_connectivity(self, scratch_db) -> None:
        client = DatabaseClient(_settings_for_db(scratch_db))
        assert client.is_initialized() is False

        await client.initialize()
        assert client.is_initialized() is True
        assert await client.check_connectivity() is True

        await client.dispose()
        assert client.is_initialized() is False

    async def test_session_commit_persists(self, scratch_db) -> None:
        await _scaffold(scratch_db)
        client = DatabaseClient(_settings_for_db(scratch_db))
        await client.initialize()
        try:
            async with client.session() as session:
                await session.execute(
                    text("INSERT INTO lifecycle_test (id, val) VALUES (1, 'committed')")
                )
            # Committed on normal exit
            assert await _read_count(scratch_db) == 1
        finally:
            await client.dispose()

    async def test_session_rollback_discards(self, scratch_db) -> None:
        await _scaffold(scratch_db)
        client = DatabaseClient(_settings_for_db(scratch_db))
        await client.initialize()
        try:
            with pytest.raises(RuntimeError, match="boom"):
                async with client.session() as session:
                    await session.execute(
                        text("INSERT INTO lifecycle_test (id, val) VALUES (2, 'rolled-back')")
                    )
                    raise RuntimeError("boom")
            assert await _read_count(scratch_db) == 0, "Rollback must discard the write"
        finally:
            await client.dispose()

    async def test_broken_session_not_reused(self, scratch_db) -> None:
        await _scaffold(scratch_db)
        client = DatabaseClient(_settings_for_db(scratch_db))
        await client.initialize()
        try:
            with pytest.raises(ProgrammingError):
                async with client.session() as session:
                    await session.execute(text("SELECT * FROM does_not_exist"))

            # A fresh session still works after the broken one was discarded
            async with client.session() as session:
                result = await session.execute(text("SELECT 1"))
                assert result.scalar_one() == 1
        finally:
            await client.dispose()


# =============================================================================
# Database connection failure
# =============================================================================


def _settings_for_unreachable_db() -> Settings:
    url = make_url(_ADMIN_URL)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        POSTGRES_HOST=url.host or "localhost",
        POSTGRES_PORT=59999,  # nothing listens here
        POSTGRES_DB="hotelops",
        POSTGRES_USER=url.username,
        POSTGRES_PASSWORD=url.password,
    )


class TestConnectionFailure:
    async def test_unreachable_database_connectivity_is_false(self) -> None:
        client = DatabaseClient(_settings_for_unreachable_db())
        await client.initialize()
        try:
            assert await client.check_connectivity() is False
        finally:
            await client.dispose()

    async def test_unreachable_database_session_raises(self) -> None:
        client = DatabaseClient(_settings_for_unreachable_db())
        await client.initialize()
        try:
            with pytest.raises(Exception, match=r"(?i)connect call failed|refused"):
                async with client.session() as session:
                    await session.execute(text("SELECT 1"))
        finally:
            await client.dispose()


# =============================================================================
# Alembic single-configuration URL resolution
# =============================================================================


class TestAlembicUrlResolution:
    async def test_alembic_uses_database_url_env(self, scratch_db, monkeypatch) -> None:
        """With an empty ini URL, env.py resolves $DATABASE_URL and migrates."""
        url = str(
            make_url(_ADMIN_URL).set(database=scratch_db).render_as_string(hide_password=False)
        )
        monkeypatch.setenv("DATABASE_URL", url)

        cfg = Config(str(ALEMBIC_INI))  # alembic.ini carries no URL
        await asyncio.to_thread(command.upgrade, cfg, "001_create_identity_tables")

        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                version = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar()
                assert version == "001_create_identity_tables"
        finally:
            await engine.dispose()
