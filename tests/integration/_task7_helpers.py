"""Shared helpers for the Task 7 reliability integration tests.

Follows the scratch-database conventions established by
tests/integration/test_migrations.py: every test creates a unique
scratch database, migrates it to head, and drops it afterwards. These
tests exercise the REAL Alembic migration chain and the REAL
DatabaseClient/RedisClient/transport.

Run:
    docker compose -f infrastructure/docker/compose.yaml up -d
    INTEGRATION_TESTS=1 pytest tests/integration/test_outbox_publisher.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.redis.client import RedisClient
from backend.app.infrastructure.transport import RedisStreamTransport
from contracts.common import utc_now
from contracts.events import EventEnvelope
from contracts.identity import ActorContext, Permission, RoleName

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "database" / "alembic.ini"

# Admin (bypass) connection — must be a superuser able to
# CREATE/DROP databases. Same default as the other integration tests.
ADMIN_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hotelops:CHANGE_ME@localhost:5433/hotelops",
)


# =============================================================================
# Scratch database lifecycle
# =============================================================================


def _admin_connect_kwargs(database: str) -> dict[str, str | int]:
    url = make_url(ADMIN_URL)
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


async def drop_database(name: str) -> None:
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


async def create_scratch_database() -> str:
    name = f"hotelops_t7_{uuid.uuid4().hex[:8]}"
    await _admin_execute(f'DROP DATABASE IF EXISTS "{name}"')
    await _admin_execute(f'CREATE DATABASE "{name}"')
    return name


def scratch_url(database: str) -> str:
    return make_url(ADMIN_URL).set(database=database).render_as_string(hide_password=False)


def scratch_settings(database: str, **overrides) -> Settings:
    """Settings pointing at a scratch database (+ any overrides)."""
    url = make_url(ADMIN_URL)
    base = dict(
        POSTGRES_HOST=url.host or "localhost",
        POSTGRES_PORT=url.port or 5432,
        POSTGRES_DB=database,
        POSTGRES_USER=url.username,
        POSTGRES_PASSWORD=url.password,
        REDIS_HOST="localhost",
        REDIS_PORT=6380,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def upgrade_to_head(database: str) -> None:
    url = scratch_url(database)
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")


def query_engine(url: str):
    return create_async_engine(url, poolclass=NullPool)


async def scalar(url: str, sql: str) -> object:
    engine = query_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


def make_database_client(database: str, **settings_overrides) -> DatabaseClient:
    return DatabaseClient(scratch_settings(database, **settings_overrides))


# =============================================================================
# Redis
# =============================================================================


async def make_redis_transport(settings: Settings) -> tuple[RedisClient, RedisStreamTransport]:
    redis = RedisClient(settings)
    await redis.initialize()
    if not await redis.check_connectivity():
        await redis.close()
        msg = "Redis is not reachable (docker compose up -d redis)"
        raise RuntimeError(msg)
    return redis, RedisStreamTransport(redis)


# =============================================================================
# Domain test data
# =============================================================================


def make_actor(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    venue_scope: frozenset[uuid.UUID] = frozenset(),
) -> ActorContext:
    return ActorContext(
        actor_id=user_id or uuid.uuid4(),
        tenant_id=tenant_id,
        role_name=RoleName.ADMIN,
        permissions=frozenset(Permission),
        venue_scope=venue_scope,
        authenticated_at=utc_now(),
        active=True,
    )


def make_envelope(event_type: str = "operational.event", **payload) -> EventEnvelope[dict]:
    now = datetime.now(UTC)
    return EventEnvelope[dict](
        event_id=uuid.uuid4(),
        event_type=event_type,
        event_time=now,
        produced_at=now,
        source="test.pipeline",
        payload=payload or {"class_name": "person"},
    )
