"""Integration tests for the Task 6.1 migration testing strategy.

Implements the strategy defined in docs/architecture/database-governance.md
Section 14. Every test runs the real Alembic migration chain (the actual
files in database/migrations/) against a scratch PostgreSQL/TimescaleDB
database, so the tests exercise the exact artifacts used in production:

  - empty database upgrade
  - upgrade from the previous migration head
  - migration head validation (single head == EXPECTED_MIGRATION_HEAD)
  - schema constraints: NOT NULL, FKs, unique, enum invariants, ON DELETE
  - timestamp correctness (timestamptz, UTC server default)
  - expected indexes exist
  - ORM model <-> migration parity (alembic check, zero drift)
  - tenant isolation via RLS on the migration-produced schema
  - migration failure rolls back atomically
  - rollback (downgrade) where supported
  - roll-forward where downgrade is unsafe

Gated by INTEGRATION_TESTS=1 (same convention as tests/integration/test_rls.py).

Run:
    docker compose -f infrastructure/docker/compose.yaml up -d postgres
    INTEGRATION_TESTS=1 pytest tests/integration/test_migrations.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.infrastructure.database.rls import set_rls_on_session

# =============================================================================
# Configuration
# =============================================================================

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "database" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

# The single expected migration head. Bump deliberately when adding migrations.
EXPECTED_MIGRATION_HEAD = "019_temporal_fact_persistence"
PREVIOUS_HEAD = "001_create_identity_tables"

# Admin (migration/bypass) connection — must be a superuser able to
# CREATE/DROP databases. Same default as the other integration tests.
_ADMIN_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hotelops:CHANGE_ME@localhost:5433/hotelops",
)

# Application runtime role created by migration 002 (NOBYPASSRLS).
_APP_ROLE_USER = os.environ.get("DATABASE_USER_APP", "hotelops_app")
_APP_ROLE_PASSWORD = os.environ.get("DATABASE_PASSWORD_APP", "CHANGE_ME")

_requires_postgres = pytest.mark.skipif(
    not os.environ.get("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 and start PostgreSQL "
    "(docker compose -f infrastructure/docker/compose.yaml up -d postgres)",
)

pytestmark = [
    pytest.mark.integration,
    _requires_postgres,
]

# =============================================================================
# Expected schema inventory (migration 001 + 002 produce exactly this)
# =============================================================================

EXPECTED_TABLES = frozenset({
    "tenants",
    "venues",
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "memberships",
    "membership_venues",
    # Task 6.4: video domain
    "cameras",
    "video_streams",
    "video_assets",
    "video_sessions",
    # Task 6.5: operational configuration
    "camera_configs",
    "analysis_configs",
    # Task 6.6: operational events (TimescaleDB hypertable)
    "operational_events",
    # Task 18.10: canonical temporal facts (authoritative persistence)
    "temporal_facts",
    # Task 6.7: evidence persistence
    "evidence_refs",
    "evidence_packages",
    "package_evidence_refs",
    # Task 6.8: analytics storage
    "metrics",
    "opportunities",
    "opportunity_metrics",
    "opportunity_evidence_refs",
    # Task 6.9: AI domain storage
    "findings",
    "recommendations",
    "recommendation_findings",
    # Task 6.10: alert & approval storage
    "alerts",
    "approval_requests",
    "approval_decisions",
    # Task 6.11: integration storage
    "integrations",
    # Task 6.12: audit, outbox, inbox
    "audit_events",
    "outbox_events",
    "inbox_messages",
    # Task 7: idempotency records
    "idempotency_records",
    # Task 10: configuration domain (018)
    "configurations",
    "configuration_versions",
    "config_camera_profiles",
    "config_zones",
    "config_tables",
    "config_entrances",
    "config_queue_areas",
    "config_service_areas",
    "config_privacy_rois",
    "config_exclusion_rois",
    "alembic_version",
})

EXPECTED_INDEXES = frozenset({
    "ix_membership_venues_venue",
    "ix_memberships_role_id",
    "ix_memberships_tenant_id",
    "ix_memberships_tenant_user",
    "ix_memberships_user_id",
    "ix_venues_tenant_id",
    "uq_permissions_name",
    "uq_roles_name",
    "uq_users_email",
    # Task 6.3: composite FK targets for cross-tenant venue scope
    "uq_memberships_membership_tenant",
    "uq_venues_venue_tenant",
    # Task 6.4: video domain
    "ix_cameras_tenant_id",
    "ix_cameras_venue_id",
    "ix_video_streams_tenant_id",
    "ix_video_streams_camera_id",
    "ix_video_streams_venue_id",
    "ix_video_assets_tenant_id",
    "ix_video_assets_venue_id",
    "ix_video_assets_camera_id",
    "ix_video_sessions_tenant_id",
    "ix_video_sessions_venue_id",
    "ix_video_sessions_camera_id",
    "ix_video_sessions_asset_id",
    "uq_cameras_camera_tenant",
    "uq_video_assets_asset_tenant",
    # Task 6.5: operational configuration
    "ix_camera_configs_tenant_id",
    "ix_camera_configs_venue_id",
    "uq_camera_configs_active",
    "uq_camera_configs_version",
    "uq_camera_configs_config_tenant",
    "ix_analysis_configs_tenant_id",
    "uq_analysis_configs_active",
    "uq_analysis_configs_version",
    "uq_analysis_configs_config_tenant",
    # Task 6.6: operational events
    "ix_operational_events_tenant_time",
    "ix_operational_events_type_time",
    "ix_operational_events_venue_id",
    "ix_operational_events_session_id",
    "uq_video_sessions_session_tenant",
    # Task 18.10: canonical temporal facts
    "ix_temporal_facts_event_time",
    "ix_temporal_facts_tenant_time",
    "ix_temporal_facts_type_time",
    "ix_temporal_facts_venue_id",
    "ix_temporal_facts_session_id",
    "ix_temporal_facts_camera_id",
    "ix_temporal_facts_config_version_id",
    # Task 6.7: evidence persistence
    "ix_evidence_refs_tenant_id",
    "ix_evidence_refs_venue_id",
    "ix_evidence_refs_session_id",
    "ix_evidence_refs_event_id",
    "ix_evidence_refs_captured_at",
    "ix_evidence_packages_tenant_id",
    "ix_evidence_packages_venue_id",
    "ix_evidence_packages_created_at",
    "ix_package_evidence_refs_ref_id",
    "uq_evidence_refs_ref_tenant",
    "uq_evidence_packages_package_tenant",
    # Task 6.8: analytics storage
    "ix_metrics_tenant_time",
    "ix_metrics_venue_time",
    "ix_metrics_name_time",
    "ix_metrics_session_id",
    "ix_metrics_camera_id",
    "ix_opportunities_tenant_id",
    "ix_opportunities_venue_id",
    "ix_opportunities_event_time",
    "ix_opportunity_metrics_metric_id",
    "ix_opportunity_evidence_refs_ref_id",
    "uq_opportunities_opportunity_tenant",
    # Task 6.9: AI domain storage
    "ix_findings_tenant_id",
    "ix_findings_venue_id",
    "ix_findings_status",
    "ix_findings_event_time",
    "ix_findings_evidence_package_id",
    "ix_recommendations_tenant_id",
    "ix_recommendations_venue_id",
    "ix_recommendations_status",
    "ix_recommendations_created_at",
    "ix_recommendation_findings_finding_id",
    "uq_findings_finding_tenant",
    "uq_recommendations_recommendation_tenant",
    # Task 6.10: alert & approval storage
    "ix_alerts_tenant_id",
    "ix_alerts_venue_id",
    "ix_alerts_status",
    "ix_alerts_event_time",
    "ix_alerts_finding_id",
    "ix_alerts_recommendation_id",
    "ix_approval_requests_tenant_id",
    "ix_approval_requests_status",
    "ix_approval_requests_recommendation_id",
    "ix_approval_requests_requested_at",
    "ix_approval_decisions_request_id",
    "ix_approval_decisions_actor_id",
    "ix_approval_decisions_decided_at",
    "uq_approval_requests_request_tenant",
    "uq_approval_decisions_terminal",
    # Task 6.11: integration storage
    "ix_integrations_tenant_id",
    "ix_integrations_venue_id",
    "ix_integrations_status",
    "ix_integrations_provider_type",
    "ix_integrations_provider_name",
    "uq_integrations_active_provider",
    # Task 6.12: audit, outbox, inbox
    "ix_audit_events_tenant_id",
    "ix_audit_events_actor_id",
    "ix_audit_events_timestamp",
    "ix_outbox_events_tenant_id",
    "ix_outbox_events_pending",
    "ix_outbox_events_venue_id",
    "uq_outbox_events_event_id",
    "ix_inbox_messages_tenant_id",
    "ix_inbox_messages_pending",
    "ix_inbox_messages_venue_id",
    "uq_inbox_messages_source_message_id",
    # Task 7: idempotency records
    "ix_idempotency_records_tenant_id",
    "ix_idempotency_records_expires_at",
    # Task 10: configuration domain (018)
    "ix_configurations_tenant_id",
    "ix_configurations_venue_id",
    "ix_config_versions_tenant_id",
    "ix_config_versions_venue_id",
    "ix_config_versions_configuration_id",
    "ix_config_versions_status",
    "ix_video_sessions_config_version_id",
    "ix_config_camera_profiles_tenant_id",
    "ix_config_camera_profiles_venue_id",
    "ix_config_camera_profiles_version_id",
    "ix_config_camera_profiles_geom_gist",
    "uq_config_camera_profiles_version_profile",
    "ix_config_zones_tenant_id",
    "ix_config_zones_venue_id",
    "ix_config_zones_version_id",
    "ix_config_zones_geom_gist",
    "uq_config_zones_version_profile",
    "ix_config_tables_tenant_id",
    "ix_config_tables_venue_id",
    "ix_config_tables_version_id",
    "ix_config_tables_geom_gist",
    "uq_config_tables_version_profile",
    "ix_config_entrances_tenant_id",
    "ix_config_entrances_venue_id",
    "ix_config_entrances_version_id",
    "ix_config_entrances_geom_gist",
    "uq_config_entrances_version_profile",
    "ix_config_queue_areas_tenant_id",
    "ix_config_queue_areas_venue_id",
    "ix_config_queue_areas_version_id",
    "ix_config_queue_areas_geom_gist",
    "uq_config_queue_areas_version_profile",
    "ix_config_service_areas_tenant_id",
    "ix_config_service_areas_venue_id",
    "ix_config_service_areas_version_id",
    "ix_config_service_areas_geom_gist",
    "uq_config_service_areas_version_profile",
    "ix_config_privacy_rois_tenant_id",
    "ix_config_privacy_rois_venue_id",
    "ix_config_privacy_rois_version_id",
    "ix_config_privacy_rois_geom_gist",
    "uq_config_privacy_rois_version_profile",
    "ix_config_exclusion_rois_tenant_id",
    "ix_config_exclusion_rois_venue_id",
    "ix_config_exclusion_rois_version_id",
    "ix_config_exclusion_rois_geom_gist",
    "uq_config_exclusion_rois_version_profile",
})

EXPECTED_RLS_POLICIES = frozenset({
    "tenants_select",
    "tenants_insert",
    "tenants_update",
    "tenants_delete",
    "venues_all",
    "memberships_all",
    "membership_venues_all",
    # Task 6.4: video domain
    "cameras_all",
    "video_streams_all",
    "video_assets_all",
    "video_sessions_all",
    # Task 6.5: operational configuration
    "camera_configs_all",
    "analysis_configs_all",
    # Task 6.6: operational events
    "operational_events_all",
    # Task 18.10: canonical temporal facts
    "temporal_facts_all",
    # Task 6.7: evidence persistence
    "evidence_refs_all",
    "evidence_packages_all",
    "package_evidence_refs_all",
    # Task 6.8: analytics storage
    "metrics_all",
    "opportunities_all",
    "opportunity_metrics_all",
    "opportunity_evidence_refs_all",
    # Task 6.9: AI domain storage
    "findings_all",
    "recommendations_all",
    "recommendation_findings_all",
    # Task 6.10: alert & approval storage
    "alerts_all",
    "approval_requests_all",
    "approval_decisions_all",
    # Task 6.11: integration storage
    "integrations_all",
    # Task 10: configuration domain (018)
    "configurations_all",
    "configuration_versions_all",
    "config_camera_profiles_all",
    "config_zones_all",
    "config_tables_all",
    "config_entrances_all",
    "config_queue_areas_all",
    "config_service_areas_all",
    "config_privacy_rois_all",
    "config_exclusion_rois_all",
})

# =============================================================================
# URL / config helpers
# =============================================================================


def _admin_url(database: str) -> str:
    """Admin URL with the database component replaced.

    Uses render_as_string(hide_password=False) because SQLAlchemy >= 2.0.40
    masks the password in str(URL), which would break authentication.
    """
    return make_url(_ADMIN_URL).set(database=database).render_as_string(hide_password=False)


def _app_url(database: str) -> str:
    """Application-role URL (hotelops_app) for RLS tests."""
    url = make_url(_ADMIN_URL).set(database=database)
    url = url.set(username=_APP_ROLE_USER, password=_APP_ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


def _alembic_config(url: str, script_location: str | None = None) -> Config:
    """Alembic Config bound to the given database (and optional script dir)."""
    cfg = Config(str(ALEMBIC_INI))
    if script_location is not None:
        cfg.set_main_option("script_location", script_location)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _upgrade(url: str, target: str, script_location: str | None = None) -> None:
    """Run alembic upgrade in a worker thread.

    env.py calls asyncio.run() internally, which cannot run inside a live
    event loop, so migrations always run off the pytest loop.
    """
    await asyncio.to_thread(command.upgrade, _alembic_config(url, script_location), target)


async def _downgrade(url: str, target: str, script_location: str | None = None) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url, script_location), target)


async def _alembic_check(url: str, script_location: str | None = None) -> None:
    """Run `alembic check` — raises AutogenerateDiffsDetected on drift."""
    await asyncio.to_thread(command.check, _alembic_config(url, script_location))


def _query_engine(url: str):
    return create_async_engine(url, poolclass=NullPool)


async def _scalar(url: str, sql: str) -> object:
    engine = _query_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


async def _version(url: str) -> str:
    """Current revision recorded in alembic_version."""
    return str(await _scalar(url, "SELECT version_num FROM alembic_version"))


async def _table_exists(url: str, name: str) -> bool:
    return bool(
        await _scalar(
            url,
            f"SELECT EXISTS (SELECT 1 FROM pg_tables "
            f"WHERE schemaname = 'public' AND tablename = '{name}')",
        )
    )


async def _all_tables(url: str) -> frozenset[str]:
    rows = await _scalar(
        url, "SELECT string_agg(tablename, ',') FROM pg_tables WHERE schemaname = 'public'"
    )
    return frozenset(str(rows).split(",")) if rows else frozenset()


async def _all_indexes(url: str) -> frozenset[str]:
    rows = await _scalar(
        url,
        "SELECT string_agg(indexname, ',') FROM pg_indexes WHERE schemaname = 'public'",
    )
    return frozenset(str(rows).split(",")) if rows else frozenset()


async def _rls_policy_names(url: str) -> frozenset[str]:
    rows = await _scalar(
        url,
        "SELECT string_agg(policyname, ',') FROM pg_policies WHERE schemaname = 'public'",
    )
    return frozenset(str(rows).split(",")) if rows else frozenset()


# =============================================================================
# Scratch database lifecycle
# =============================================================================


def _admin_connect_kwargs(database: str) -> dict[str, str | int]:
    """Connection keyword arguments for the admin role (asyncpg).

    Uses asyncpg directly because CREATE/DROP DATABASE cannot run inside
    a transaction, and the SQLAlchemy asyncpg dialect's AUTOCOMMIT
    isolation level mishandles the handshake.
    """
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
    """Run a single statement against the maintenance database.

    asyncpg executes standalone statements in autocommit, which is required
    for CREATE/DROP DATABASE.
    """
    conn = await asyncpg.connect(**_admin_connect_kwargs("postgres"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _terminate_connections(database: str) -> None:
    """Kill any lingering connections to the scratch database."""
    conn = await asyncpg.connect(**_admin_connect_kwargs("postgres"))
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
    finally:
        await conn.close()


async def _drop_database(name: str) -> None:
    """Drop a scratch database, tolerating briefly lingering connections.

    pg_terminate_backend is asynchronous; retry the DROP briefly in case a
    connection is still being disposed.
    """
    await _terminate_connections(name)
    for attempt in range(3):
        try:
            await _admin_execute(f'DROP DATABASE IF EXISTS "{name}"')
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(0.3)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _cleanup_orphaned_scratch_databases():
    """Best-effort removal of scratch databases left by killed test runs.

    Scratch names are random per run, so a hard-killed pytest process can
    leave orphaned hotelops_migtest_* databases behind. Clean them once
    per module (never touches the dev `hotelops` database).
    """
    conn = await asyncpg.connect(**_admin_connect_kwargs("postgres"))
    try:
        rows = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE 'hotelops_migtest_%'"
        )
        for row in rows:
            with contextlib.suppress(Exception):
                # In use by another process — skip; next run retries.
                await conn.execute(f'DROP DATABASE IF EXISTS "{row["datname"]}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def fresh_db():
    """A unique, empty scratch database, dropped again after the test."""
    name = f"hotelops_migtest_{uuid.uuid4().hex[:8]}"
    await _admin_execute(f'DROP DATABASE IF EXISTS "{name}"')
    await _admin_execute(f'CREATE DATABASE "{name}"')
    try:
        yield {"name": name, "url": _admin_url(name)}
    finally:
        await _drop_database(name)


@pytest_asyncio.fixture
async def migrated_db(fresh_db):
    """A scratch database upgraded to the current migration head."""
    await _upgrade(fresh_db["url"], "head")
    return fresh_db


# =============================================================================
# Migration head validation (offline — no database required)
# =============================================================================


class TestMigrationHeadValidation:
    """The repository must define exactly one head, equal to EXPECTED_MIGRATION_HEAD."""

    def test_single_head(self) -> None:
        sd = ScriptDirectory.from_config(_alembic_config(_ADMIN_URL, str(MIGRATIONS_DIR)))
        heads = sd.get_heads()
        assert len(heads) == 1, f"Expected a single migration head, got {heads}"

    def test_expected_head_matches_repository_head(self) -> None:
        sd = ScriptDirectory.from_config(_alembic_config(_ADMIN_URL, str(MIGRATIONS_DIR)))
        assert sd.get_current_head() == EXPECTED_MIGRATION_HEAD

    def test_revision_chain_is_linear(self) -> None:
        """Each revision has a unique parent — no branches, one base."""
        sd = ScriptDirectory.from_config(_alembic_config(_ADMIN_URL, str(MIGRATIONS_DIR)))
        revisions = list(sd.walk_revisions())
        downs = [r.down_revision for r in revisions]
        assert len(downs) == len(set(downs)), "Branching migration chain detected"
        assert downs[-1] is None, "Chain does not terminate at a single base"
        assert len(sd.get_bases()) == 1


# =============================================================================
# Empty database upgrade
# =============================================================================


class TestEmptyDatabaseUpgrade:
    """alembic upgrade head must succeed from a completely empty database."""

    async def test_upgrade_succeeds_and_reaches_head(self, fresh_db) -> None:
        url = fresh_db["url"]
        await _upgrade(url, "head")
        assert await _version(url) == EXPECTED_MIGRATION_HEAD

    async def test_full_schema_inventory(self, fresh_db) -> None:
        """The migrated schema matches the expected tables/indexes/policies."""
        url = fresh_db["url"]
        await _upgrade(url, "head")

        assert await _all_tables(url) == EXPECTED_TABLES

        indexes = await _all_indexes(url)
        for expected in EXPECTED_INDEXES:
            assert expected in indexes, f"Missing index: {expected}"

        policies = await _rls_policy_names(url)
        assert policies == EXPECTED_RLS_POLICIES

    async def test_app_role_created_without_rls_bypass(self, fresh_db) -> None:
        """hotelops_app exists and is NOBYPASSRLS.

        Note: roles are cluster-wide, so the role may pre-exist on a shared
        cluster from other runs; migration 002 creates it idempotently. The
        invariant that matters — NOBYPASSRLS so RLS cannot be bypassed — is
        asserted regardless.
        """
        url = fresh_db["url"]
        await _upgrade(url, "head")
        row = await _scalar(
            url,
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'hotelops_app'",
        )
        assert row is not None, "Application role hotelops_app was not created"
        assert row is False, "hotelops_app must be NOBYPASSRLS"

    async def test_upgrade_is_idempotent(self, fresh_db) -> None:
        url = fresh_db["url"]
        await _upgrade(url, "head")
        await _upgrade(url, "head")  # second run is a no-op
        assert await _version(url) == EXPECTED_MIGRATION_HEAD


# =============================================================================
# Upgrade from the previous migration
# =============================================================================


class TestUpgradeFromPreviousHead:
    """The incremental path (001, then 002) must equal the full path."""

    async def test_incremental_upgrade(self, fresh_db) -> None:
        url = fresh_db["url"]
        await _upgrade(url, PREVIOUS_HEAD)
        assert await _version(url) == PREVIOUS_HEAD

        # 001 present: tables exist, RLS not yet enabled
        assert await _table_exists(url, "tenants")
        assert await _rls_policy_names(url) == frozenset()

        # 002 applied: RLS policies appear
        await _upgrade(url, "head")
        assert await _version(url) == EXPECTED_MIGRATION_HEAD
        assert await _rls_policy_names(url) == EXPECTED_RLS_POLICIES


# =============================================================================
# Schema constraints and invariants (against the migrated schema)
# =============================================================================


class TestSchemaConstraints:
    """NOT NULL, FK, unique, enum, and ON DELETE behavior at the DB level."""

    async def test_not_null_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text("INSERT INTO tenants (tenant_id, name) VALUES (:id, NULL)"),
                        {"id": uuid.uuid4()},
                    )
        finally:
            await engine.dispose()

    async def test_foreign_key_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO venues (venue_id, tenant_id, name) "
                            "VALUES (:vid, :tid, :name)"
                        ),
                        {"vid": uuid.uuid4(), "tid": uuid.uuid4(), "name": "Orphan"},
                    )
        finally:
            await engine.dispose()

    async def test_unique_constraint_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO users (user_id, display_name, email) "
                        "VALUES (:id, :name, :email)"
                    ),
                    {"id": uuid.uuid4(), "name": "User One", "email": "dup@example.com"},
                )
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO users (user_id, display_name, email) "
                            "VALUES (:id, :name, :email)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "name": "User Two",
                            "email": "dup@example.com",
                        },
                    )
        finally:
            await engine.dispose()

    async def test_enum_invariant_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # asyncpg surfaces invalid enum values as a generic DBAPIError
                # (translated from InvalidTextRepresentationError).
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO tenants (tenant_id, name, status) "
                            "VALUES (:id, :name, 'bogus')"
                        ),
                        {"id": uuid.uuid4(), "name": "Bad Tenant"},
                    )
        finally:
            await engine.dispose()

    async def test_on_delete_cascade(self, migrated_db) -> None:
        url = migrated_db["url"]
        tenant_id = uuid.uuid4()
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tenant_id, "name": "Cascade Tenant"},
                )
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": uuid.uuid4(), "tid": tenant_id, "name": "Cascaded Venue"},
                )
                await conn.execute(
                    text("DELETE FROM tenants WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
            count = await _scalar(
                url,
                f"SELECT count(*) FROM venues WHERE tenant_id = '{tenant_id}'::uuid",
            )
            assert count == 0, "ON DELETE CASCADE did not remove venues"
        finally:
            await engine.dispose()


# =============================================================================
# Timestamp correctness
# =============================================================================


class TestTimestampCorrectness:
    """All persisted timestamps are timestamptz with a UTC server default."""

    _CREATED_AT_TABLES = ("tenants", "venues", "users", "memberships")

    async def test_created_at_columns_are_timestamptz(self, migrated_db) -> None:
        url = migrated_db["url"]
        for table in self._CREATED_AT_TABLES:
            data_type = await _scalar(
                url,
                "SELECT data_type FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = 'created_at'",
            )
            assert data_type == "timestamp with time zone", f"{table}.created_at"

    async def test_created_at_server_default_is_utc_now(self, migrated_db) -> None:
        url = migrated_db["url"]
        default_expr = await _scalar(
            url,
            "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef "
            "WHERE adrelid = 'tenants'::regclass AND adnum = ("
            "SELECT attnum FROM pg_attribute "
            "WHERE attrelid = 'tenants'::regclass AND attname = 'created_at')",
        )
        assert "now()" in str(default_expr), "created_at server default must be now()"

    async def test_insert_relies_on_server_default_utc(self, migrated_db) -> None:
        url = migrated_db["url"]
        tenant_id = uuid.uuid4()
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tenant_id, "name": "Clock Tenant"},
                )
            created_at = await _scalar(
                url,
                f"SELECT created_at FROM tenants WHERE tenant_id = '{tenant_id}'::uuid",
            )
            assert created_at is not None
            assert created_at.tzinfo is not None, "created_at must be timezone-aware"
            delta = abs((datetime.now(UTC) - created_at).total_seconds())
            assert delta < 60, "created_at is not the current UTC time"
        finally:
            await engine.dispose()


# =============================================================================
# ORM model <-> migration parity
# =============================================================================


class TestModelMigrationParity:
    """alembic check must report zero drift between models and migrations."""

    async def test_alembic_check_reports_no_drift(self, migrated_db) -> None:
        await _alembic_check(migrated_db["url"])


# =============================================================================
# RLS tenant isolation on the migration-produced schema
# =============================================================================

_TENANT_A = "00000000-0000-0000-0000-000000000001"
_TENANT_B = "00000000-0000-0000-0000-000000000002"
_VENUE_A1 = "00000000-0000-0000-0000-000000000020"
_VENUE_B1 = "00000000-0000-0000-0000-000000000022"
_USER_A = "00000000-0000-0000-0000-000000000030"
_ROLE_ADMIN = "00000000-0000-0000-0000-000000000010"
_MEMBERSHIP_A = "00000000-0000-0000-0000-000000000040"


async def _seed_identity(url: str) -> None:
    """Seed two tenants, one venue each, and a tenant-A membership.

    Runs as the admin role, which bypasses RLS.
    """
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, tname in (
                (_TENANT_A, "Tenant A"),
                (_TENANT_B, "Tenant B"),
            ):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": tname},
                )
            for vid, tid, vname in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": vname},
                )
            await conn.execute(
                text("INSERT INTO users (user_id, display_name, email) VALUES (:id, :n, :e)"),
                {"id": _USER_A, "n": "User A", "e": "user_a@example.com"},
            )
            await conn.execute(
                text("INSERT INTO roles (role_id, name) VALUES (:id, 'admin')"),
                {"id": _ROLE_ADMIN},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships "
                    "(membership_id, user_id, tenant_id, role_id, scope, status) "
                    "VALUES (:id, :uid, :tid, :rid, 'all_venues', 'active')"
                ),
                {"id": _MEMBERSHIP_A, "uid": _USER_A, "tid": _TENANT_A, "rid": _ROLE_ADMIN},
            )
    finally:
        await engine.dispose()


async def _seed_video_rls(url: str) -> None:
    """Seed the fixed RLS tenants/venues plus one camera per venue."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            await conn.execute(
                text(
                    "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                    "VALUES (:id, :vid, :tid, :name)"
                ),
                {"id": uuid.uuid4(), "vid": _VENUE_A1, "tid": _TENANT_A, "name": "Camera A"},
            )
            await conn.execute(
                text(
                    "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                    "VALUES (:id, :vid, :tid, :name)"
                ),
                {"id": uuid.uuid4(), "vid": _VENUE_B1, "tid": _TENANT_B, "name": "Camera B"},
            )
    finally:
        await engine.dispose()


async def _seed_config_rls(url: str) -> dict[str, uuid.UUID]:
    """Seed tenants/venues/cameras plus one active camera config and one
    active analysis config per tenant (fixed IDs) for RLS tests."""
    camera_a = uuid.uuid4()
    camera_b = uuid.uuid4()
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (camera_a, _VENUE_A1, _TENANT_A, "Camera A"),
                (camera_b, _VENUE_B1, _TENANT_B, "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
            # One active camera config per tenant
            for cid, vid, tid in (
                (camera_a, _VENUE_A1, _TENANT_A),
                (camera_b, _VENUE_B1, _TENANT_B),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO camera_configs "
                        "(config_id, camera_id, venue_id, tenant_id, status, version) "
                        "VALUES (:id, :cid, :vid, :tid, 'active', 1)"
                    ),
                    {"id": uuid.uuid4(), "cid": cid, "vid": vid, "tid": tid},
                )
            # One active analysis config per tenant venue
            for vid, tid in ((_VENUE_A1, _TENANT_A), (_VENUE_B1, _TENANT_B)):
                await conn.execute(
                    text(
                        "INSERT INTO analysis_configs "
                        "(config_id, venue_id, tenant_id, name, status, version) "
                        "VALUES (:id, :vid, :tid, 'default', 'active', 1)"
                    ),
                    {"id": uuid.uuid4(), "vid": vid, "tid": tid},
                )
    finally:
        await engine.dispose()
    return {"camera_a": camera_a, "camera_b": camera_b}


async def _seed_events_rls(url: str) -> dict[str, uuid.UUID]:
    """Seed tenants/venues/cameras plus one operational event per tenant
    (fixed IDs) for RLS tests."""
    camera_a = uuid.uuid4()
    camera_b = uuid.uuid4()
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (camera_a, _VENUE_A1, _TENANT_A, "Camera A"),
                (camera_b, _VENUE_B1, _TENANT_B, "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
            # PostgreSQL now() is the transaction start time, so the seed's
            # event_time must precede it for the ingestion_time >= event_time
            # invariant to hold in this multi-statement transaction.
            now = datetime.now(UTC) - timedelta(seconds=10)
            for vid, tid, cid in (
                (_VENUE_A1, _TENANT_A, camera_a),
                (_VENUE_B1, _TENANT_B, camera_b),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO operational_events "
                        "(event_id, event_type, tenant_id, venue_id, camera_id, "
                        "event_time, produced_at, source, payload) "
                        "VALUES (:id, 'detection.observation', :tid, :vid, :cid, "
                        ":et, :pa, 'cv.pipeline', CAST(:payload AS jsonb))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tid,
                        "vid": vid,
                        "cid": cid,
                        "et": now,
                        "pa": now,
                        "payload": json.dumps({"class_name": "person"}),
                    },
                )
    finally:
        await engine.dispose()
    return {"camera_a": camera_a, "camera_b": camera_b}


async def _seed_evidence_rls(url: str) -> dict[str, uuid.UUID]:
    """Seed the fixed RLS tenants/venues/cameras plus one evidence ref and one
    package per tenant for RLS tests."""
    camera_a = uuid.uuid4()
    camera_b = uuid.uuid4()
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (camera_a, _VENUE_A1, _TENANT_A, "Camera A"),
                (camera_b, _VENUE_B1, _TENANT_B, "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
            for vid, tid, cid in (
                (_VENUE_A1, _TENANT_A, camera_a),
                (_VENUE_B1, _TENANT_B, camera_b),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO evidence_refs "
                        "(ref_id, tenant_id, venue_id, ref_type, ref_uri, camera_id) "
                        "VALUES (:rid, :tid, :vid, 'frame', :uri, :cid)"
                    ),
                    {
                        "rid": uuid.uuid4(),
                        "tid": tid,
                        "vid": vid,
                        "uri": f"s3://hotelops/{tid}/frame.jpg",
                        "cid": cid,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO evidence_packages "
                        "(package_id, tenant_id, venue_id) VALUES (:pid, :tid, :vid)"
                    ),
                    {"pid": uuid.uuid4(), "tid": tid, "vid": vid},
                )
    finally:
        await engine.dispose()
    return {"camera_a": camera_a, "camera_b": camera_b}


async def _seed_analytics_rls(url: str) -> dict[str, uuid.UUID]:
    """Seed the fixed RLS tenants/venues/cameras plus one metric and one
    opportunity per tenant for RLS tests."""
    camera_a = uuid.uuid4()
    camera_b = uuid.uuid4()
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (camera_a, _VENUE_A1, _TENANT_A, "Camera A"),
                (camera_b, _VENUE_B1, _TENANT_B, "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
            # PostgreSQL now() is the transaction start time, so sample times
            # must precede it for the ingestion_time >= event_time invariant.
            now = datetime.now(UTC) - timedelta(seconds=10)
            for vid, tid, cid in (
                (_VENUE_A1, _TENANT_A, camera_a),
                (_VENUE_B1, _TENANT_B, camera_b),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO metrics "
                        "(metric_id, metric_name, value, event_time, tenant_id, "
                        "venue_id, camera_id) "
                        "VALUES (:mid, 'occupancy_rate', 0.5, :et, :tid, :vid, :cid)"
                    ),
                    {"mid": uuid.uuid4(), "et": now, "tid": tid, "vid": vid, "cid": cid},
                )
                await conn.execute(
                    text(
                        "INSERT INTO opportunities "
                        "(opportunity_id, tenant_id, venue_id, description, event_time) "
                        "VALUES (:oid, :tid, :vid, 'Lobby staffing', :et)"
                    ),
                    {"oid": uuid.uuid4(), "tid": tid, "vid": vid, "et": now},
                )
    finally:
        await engine.dispose()
    return {"camera_a": camera_a, "camera_b": camera_b}


async def _seed_ai_rls(url: str) -> dict[str, uuid.UUID]:
    """Seed the fixed RLS tenants/venues plus one finding and one
    recommendation per tenant for RLS tests."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for vid, tid in ((_VENUE_A1, _TENANT_A), (_VENUE_B1, _TENANT_B)):
                await conn.execute(
                    text(
                        "INSERT INTO findings "
                        "(finding_id, tenant_id, venue_id, finding_type, "
                        "description, event_time) "
                        "VALUES (:fid, :tid, :vid, 'occupancy', 'Lobby crowded', :et)"
                    ),
                    {"fid": uuid.uuid4(), "tid": tid, "vid": vid, "et": datetime.now(UTC)},
                )
                await conn.execute(
                    text(
                        "INSERT INTO recommendations "
                        "(recommendation_id, tenant_id, venue_id, description) "
                        "VALUES (:rid, :tid, :vid, 'Open lane 2')"
                    ),
                    {"rid": uuid.uuid4(), "tid": tid, "vid": vid},
                )
                await conn.execute(
                    text(
                        "INSERT INTO alerts "
                        "(alert_id, tenant_id, venue_id, alert_type, title, "
                        "description, event_time) "
                        "VALUES (:aid, :tid, :vid, 'occupancy', 'Lobby busy', "
                        "'Lobby above threshold', :et)"
                    ),
                    {"aid": uuid.uuid4(), "tid": tid, "vid": vid, "et": datetime.now(UTC)},
                )
    finally:
        await engine.dispose()


async def _seed_approvals_rls(url: str) -> dict[str, uuid.UUID]:
    """Seed the fixed RLS tenants/venues plus one approval request per
    tenant (with a supporting user actor) for RLS tests.

    Returns the request ids so tests can probe cross-tenant lookups.
    """
    actor_a = uuid.uuid4()
    actor_b = uuid.uuid4()
    request_a = uuid.uuid4()
    request_b = uuid.uuid4()
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for uid, _tid, email in (
                (actor_a, _TENANT_A, "actor_a@example.com"),
                (actor_b, _TENANT_B, "actor_b@example.com"),
            ):
                await conn.execute(
                    text("INSERT INTO users (user_id, display_name, email) VALUES (:id, :n, :e)"),
                    {"id": uid, "n": "Actor", "e": email},
                )
            for vid, tid, actor, req in (
                (_VENUE_A1, _TENANT_A, actor_a, request_a),
                (_VENUE_B1, _TENANT_B, actor_b, request_b),
            ):
                rec_id = uuid.uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO recommendations "
                        "(recommendation_id, tenant_id, venue_id, description) "
                        "VALUES (:rid, :tid, :vid, 'Open lane 2')"
                    ),
                    {"rid": rec_id, "tid": tid, "vid": vid},
                )
                await conn.execute(
                    text(
                        "INSERT INTO approval_requests "
                        "(request_id, tenant_id, recommendation_id, requested_by, "
                        "requested_at) "
                        "VALUES (:req, :tid, :rid, :actor, :at)"
                    ),
                    {
                        "req": req,
                        "tid": tid,
                        "rid": rec_id,
                        "actor": actor,
                        "at": datetime.now(UTC),
                    },
                )
    finally:
        await engine.dispose()
    return {"request_a": request_a, "request_b": request_b}


async def _seed_integrations_rls(url: str) -> None:
    """Seed the fixed RLS tenants/venues plus one integration per tenant
    for RLS tests."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((_TENANT_A, "Tenant A"), (_TENANT_B, "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (_VENUE_A1, _TENANT_A, "Venue A-1"),
                (_VENUE_B1, _TENANT_B, "Venue B-1"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for vid, tid in ((_VENUE_A1, _TENANT_A), (_VENUE_B1, _TENANT_B)):
                await conn.execute(
                    text(
                        "INSERT INTO integrations "
                        "(integration_id, tenant_id, venue_id, provider_type, "
                        "provider_name, status) "
                        "VALUES (:iid, :tid, :vid, 'pos', 'lightspeed', 'pending')"
                    ),
                    {"iid": uuid.uuid4(), "tid": tid, "vid": vid},
                )
    finally:
        await engine.dispose()


class TestRlsOnMigratedSchema:
    """RLS (migration 002) isolates tenants on the real migrated schema."""

    async def _app_session(self, database: str):
        """A session connected as the hotelops_app runtime role."""
        engine = create_async_engine(_app_url(database), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        session = factory()
        try:
            yield session
        finally:
            await session.close()
            await engine.dispose()

    async def test_missing_context_fails_closed(self, migrated_db) -> None:
        url = migrated_db["url"]
        await _seed_identity(url)
        async for session in self._app_session(migrated_db["name"]):
            result = await session.execute(text("SELECT count(*) FROM tenants"))
            assert result.scalar_one() == 0, "Missing RLS context must fail closed"

    async def test_tenant_sees_only_own_rows(self, migrated_db) -> None:
        url = migrated_db["url"]
        await _seed_identity(url)
        async for session in self._app_session(migrated_db["name"]):
            await set_rls_on_session(session, _TENANT_A)
            rows = (await session.execute(text("SELECT tenant_id FROM tenants"))).scalars().all()
            assert set(rows) == {uuid.UUID(_TENANT_A)}
            # Direct lookups of a foreign venue return nothing
            result = await session.execute(
                text("SELECT name FROM venues WHERE venue_id = :vid"),
                {"vid": uuid.UUID(_VENUE_B1)},
            )
            assert result.scalar_one_or_none() is None

    async def test_cross_tenant_insert_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        await _seed_identity(url)
        async for session in self._app_session(migrated_db["name"]):
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {
                        "vid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "name": "Cross-Tenant Venue",
                    },
                )
            await session.rollback()

    async def test_cross_tenant_delete_affects_zero_rows(self, migrated_db) -> None:
        url = migrated_db["url"]
        await _seed_identity(url)
        async for session in self._app_session(migrated_db["name"]):
            await set_rls_on_session(session, _TENANT_A)
            result = await session.execute(
                text("DELETE FROM venues WHERE venue_id = :vid"),
                {"vid": uuid.UUID(_VENUE_B1)},
            )
            assert result.rowcount == 0, "RLS must block cross-tenant DELETE"
            await session.commit()

    async def test_membership_venue_link_via_app_role(self, migrated_db) -> None:
        """The app role can scope a venue to its own membership (Task 6.3)."""
        url = migrated_db["url"]
        await _seed_identity(url)
        async for session in self._app_session(migrated_db["name"]):
            await set_rls_on_session(session, _TENANT_A)
            # Same-tenant link: RLS policy (membership of tenant A) AND the
            # composite FK (venue of tenant A) both pass.
            await session.execute(
                text(
                    "INSERT INTO membership_venues "
                    "(membership_id, venue_id, tenant_id) VALUES (:mid, :vid, :tid)"
                ),
                {
                    "mid": uuid.UUID(_MEMBERSHIP_A),
                    "vid": uuid.UUID(_VENUE_A1),
                    "tid": uuid.UUID(_TENANT_A),
                },
            )
            await session.commit()

            # Commit ends the transaction, which clears the SET LOCAL RLS
            # context (transaction-scoped by design). Re-establish it for
            # the next transaction.
            await set_rls_on_session(session, _TENANT_A)
            # Cross-tenant link: the composite FK rejects the tenant-B venue
            # even though RLS (membership of tenant A) would allow the row.
            with pytest.raises(IntegrityError, match="foreign key"):
                await session.execute(
                    text(
                        "INSERT INTO membership_venues "
                        "(membership_id, venue_id, tenant_id) VALUES (:mid, :vid, :tid)"
                    ),
                    {
                        "mid": uuid.UUID(_MEMBERSHIP_A),
                        "vid": uuid.UUID(_VENUE_B1),
                        "tid": uuid.UUID(_TENANT_A),
                    },
                )
            await session.rollback()

    async def test_video_cameras_isolated_by_rls(self, migrated_db) -> None:
        """App role sees only own-tenant cameras; no context fails closed."""
        url = migrated_db["url"]
        await _seed_video_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            # Missing context fails closed
            count = await session.execute(text("SELECT count(*) FROM cameras"))
            assert count.scalar_one() == 0, "Missing RLS context must fail closed"

            await set_rls_on_session(session, _TENANT_A)
            names = (await session.execute(text("SELECT name FROM cameras"))).scalars().all()
            assert names == ["Camera A"], "RLS must isolate cameras to the tenant"

            # Cross-tenant camera lookup returns nothing
            row = await session.execute(
                text("SELECT name FROM cameras WHERE name = :n"),
                {"n": "Camera B"},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

    async def test_config_tables_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.5 — config tables are tenant-isolated by RLS (migration 007)."""
        url = migrated_db["url"]
        ids = await _seed_config_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            # Missing context fails closed on both config tables
            for table in ("camera_configs", "analysis_configs"):
                count = await session.execute(text(f"SELECT count(*) FROM {table}"))
                assert count.scalar_one() == 0, f"Missing RLS context must fail closed on {table}"

            await set_rls_on_session(session, _TENANT_A)
            # Tenant A sees only its own configs
            names = (
                (await session.execute(text("SELECT camera_id FROM camera_configs")))
                .scalars()
                .all()
            )
            assert names == [ids["camera_a"]], "RLS must isolate camera_configs to the tenant"
            profiles = (
                (await session.execute(text("SELECT name FROM analysis_configs"))).scalars().all()
            )
            assert profiles == ["default"], "RLS must isolate analysis_configs to the tenant"

            # Cross-tenant direct lookup of tenant-B's camera config returns nothing
            row = await session.execute(
                text("SELECT config_id FROM camera_configs WHERE camera_id = :cid"),
                {"cid": ids["camera_b"]},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO camera_configs "
                        "(config_id, camera_id, venue_id, tenant_id, status, version) "
                        "VALUES (:id, :cid, :vid, :tid, 'active', 1)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "cid": ids["camera_b"],
                        "vid": uuid.UUID(_VENUE_B1),
                        "tid": uuid.UUID(_TENANT_B),
                    },
                )
            await session.rollback()

    async def test_events_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.6 — operational_events is tenant-isolated by RLS."""
        url = migrated_db["url"]
        ids = await _seed_events_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            # Missing context fails closed
            count = await session.execute(text("SELECT count(*) FROM operational_events"))
            assert count.scalar_one() == 0, "Missing RLS context must fail closed"

            await set_rls_on_session(session, _TENANT_A)
            rows = (
                (await session.execute(text("SELECT event_type FROM operational_events")))
                .scalars()
                .all()
            )
            assert rows == ["detection.observation"], "RLS must isolate events to the tenant"

            # Cross-tenant direct lookup of tenant-B's event returns nothing
            row = await session.execute(
                text("SELECT event_id FROM operational_events WHERE camera_id = :cid"),
                {"cid": ids["camera_b"]},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            now = datetime.now(UTC)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO operational_events "
                        "(event_id, event_type, tenant_id, venue_id, event_time, "
                        "produced_at, source, payload) "
                        "VALUES (:id, 'detection.observation', :tid, :vid, "
                        ":et, :pa, 'cv.pipeline', CAST(:payload AS jsonb))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "vid": uuid.UUID(_VENUE_B1),
                        "et": now,
                        "pa": now,
                        "payload": json.dumps({"class_name": "person"}),
                    },
                )
            await session.rollback()

    async def test_evidence_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.7 — all three evidence tables are tenant-isolated by RLS."""
        url = migrated_db["url"]
        ids = await _seed_evidence_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            # Missing context fails closed on every evidence table
            for table in ("evidence_refs", "evidence_packages", "package_evidence_refs"):
                count = await session.execute(text(f"SELECT count(*) FROM {table}"))
                assert count.scalar_one() == 0, f"Missing RLS context must fail closed on {table}"

            await set_rls_on_session(session, _TENANT_A)
            # Tenant A sees only its own rows
            ref_rows = (
                (await session.execute(text("SELECT ref_id FROM evidence_refs"))).scalars().all()
            )
            assert len(ref_rows) == 1, "RLS must isolate evidence_refs to the tenant"
            pkg_rows = (
                (await session.execute(text("SELECT package_id FROM evidence_packages")))
                .scalars()
                .all()
            )
            assert len(pkg_rows) == 1, "RLS must isolate evidence_packages to the tenant"

            # Cross-tenant direct lookup of tenant-B's evidence returns nothing
            row = await session.execute(
                text("SELECT ref_id FROM evidence_refs WHERE camera_id = :cid"),
                {"cid": ids["camera_b"]},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO evidence_refs "
                        "(ref_id, tenant_id, venue_id, ref_type, ref_uri) "
                        "VALUES (:rid, :tid, :vid, 'frame', 's3://b/k.jpg')"
                    ),
                    {
                        "rid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "vid": uuid.UUID(_VENUE_B1),
                    },
                )
            await session.rollback()

            # Cross-tenant package/ref link is rejected by RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO package_evidence_refs "
                        "(package_id, ref_id, tenant_id) VALUES (:pid, :rid, :tid)"
                    ),
                    {
                        "pid": uuid.uuid4(),
                        "rid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                    },
                )
            await session.rollback()

    async def test_analytics_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.8 — metrics and opportunities are tenant-isolated by RLS."""
        url = migrated_db["url"]
        ids = await _seed_analytics_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            # Missing context fails closed on every analytics table
            for table in (
                "metrics",
                "opportunities",
                "opportunity_metrics",
                "opportunity_evidence_refs",
            ):
                count = await session.execute(text(f"SELECT count(*) FROM {table}"))
                assert count.scalar_one() == 0, f"Missing RLS context must fail closed on {table}"

            await set_rls_on_session(session, _TENANT_A)
            # Tenant A sees only its own rows
            metric_rows = (
                (await session.execute(text("SELECT metric_id FROM metrics"))).scalars().all()
            )
            assert len(metric_rows) == 1, "RLS must isolate metrics to the tenant"
            opp_rows = (
                (await session.execute(text("SELECT opportunity_id FROM opportunities")))
                .scalars()
                .all()
            )
            assert len(opp_rows) == 1, "RLS must isolate opportunities to the tenant"

            # Cross-tenant direct lookup of tenant-B's metric returns nothing
            row = await session.execute(
                text("SELECT metric_id FROM metrics WHERE camera_id = :cid"),
                {"cid": ids["camera_b"]},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO metrics "
                        "(metric_id, metric_name, value, event_time, tenant_id, "
                        "venue_id) "
                        "VALUES (:mid, 'occupancy_rate', 0.5, :et, :tid, :vid)"
                    ),
                    {
                        "mid": uuid.uuid4(),
                        "et": datetime.now(UTC),
                        "tid": uuid.UUID(_TENANT_B),
                        "vid": uuid.UUID(_VENUE_B1),
                    },
                )
            await session.rollback()

            # Cross-tenant M2M link insert is rejected by RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO opportunity_evidence_refs "
                        "(opportunity_id, ref_id, tenant_id) VALUES (:oid, :rid, :tid)"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "rid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                    },
                )
            await session.rollback()

    async def test_ai_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.9 — findings, recommendations, and the M2M link are
        tenant-isolated by RLS."""
        url = migrated_db["url"]
        await _seed_ai_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            # Missing context fails closed on every AI table
            for table in ("findings", "recommendations", "recommendation_findings"):
                count = await session.execute(text(f"SELECT count(*) FROM {table}"))
                assert count.scalar_one() == 0, f"Missing RLS context must fail closed on {table}"

            await set_rls_on_session(session, _TENANT_A)
            # Tenant A sees only its own rows
            finding_rows = (
                (await session.execute(text("SELECT finding_id FROM findings"))).scalars().all()
            )
            assert len(finding_rows) == 1, "RLS must isolate findings to the tenant"
            rec_rows = (
                (await session.execute(text("SELECT recommendation_id FROM recommendations")))
                .scalars()
                .all()
            )
            assert len(rec_rows) == 1, "RLS must isolate recommendations to the tenant"

            # Cross-tenant direct lookup of tenant-B's finding returns nothing
            row = await session.execute(
                text("SELECT finding_id FROM findings WHERE venue_id = :vid"),
                {"vid": uuid.UUID(_VENUE_B1)},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO findings "
                        "(finding_id, tenant_id, venue_id, finding_type, "
                        "description, event_time) "
                        "VALUES (:fid, :tid, :vid, 'occupancy', 'Crowded', :et)"
                    ),
                    {
                        "fid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "vid": uuid.UUID(_VENUE_B1),
                        "et": datetime.now(UTC),
                    },
                )
            await session.rollback()

            # Cross-tenant M2M link insert is rejected by RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO recommendation_findings "
                        "(recommendation_id, finding_id, tenant_id) "
                        "VALUES (:rid, :fid, :tid)"
                    ),
                    {
                        "rid": uuid.uuid4(),
                        "fid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                    },
                )
            await session.rollback()

    async def test_alerts_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.10 — alerts are tenant-isolated by RLS."""
        url = migrated_db["url"]
        await _seed_ai_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            count = await session.execute(text("SELECT count(*) FROM alerts"))
            assert count.scalar_one() == 0, "Missing RLS context must fail closed"

            await set_rls_on_session(session, _TENANT_A)
            rows = (await session.execute(text("SELECT alert_id FROM alerts"))).scalars().all()
            assert len(rows) == 1, "RLS must isolate alerts to the tenant"

            # Cross-tenant direct lookup of tenant-B's alert returns nothing
            row = await session.execute(
                text("SELECT alert_id FROM alerts WHERE venue_id = :vid"),
                {"vid": uuid.UUID(_VENUE_B1)},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO alerts "
                        "(alert_id, tenant_id, venue_id, alert_type, title, "
                        "description, event_time) "
                        "VALUES (:aid, :tid, :vid, 'occupancy', 'Busy', "
                        "'Lobby busy', :et)"
                    ),
                    {
                        "aid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "vid": uuid.UUID(_VENUE_B1),
                        "et": datetime.now(UTC),
                    },
                )
            await session.rollback()

    async def test_approvals_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.10 — approval requests and decisions are tenant-isolated."""
        url = migrated_db["url"]
        ids = await _seed_approvals_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            for table in ("approval_requests", "approval_decisions"):
                count = await session.execute(text(f"SELECT count(*) FROM {table}"))
                assert count.scalar_one() == 0, f"Missing RLS context must fail closed on {table}"

            await set_rls_on_session(session, _TENANT_A)
            rows = (
                (await session.execute(text("SELECT request_id FROM approval_requests")))
                .scalars()
                .all()
            )
            assert rows == [ids["request_a"]], "RLS must isolate approval_requests to the tenant"

            # Cross-tenant direct lookup of tenant-B's request returns nothing
            row = await session.execute(
                text("SELECT request_id FROM approval_requests WHERE request_id = :req"),
                {"req": ids["request_b"]},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO approval_requests "
                        "(request_id, tenant_id, recommendation_id, requested_by, "
                        "requested_at) VALUES (:req, :tid, :rid, :actor, :at)"
                    ),
                    {
                        "req": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "rid": uuid.uuid4(),
                        "actor": uuid.uuid4(),
                        "at": datetime.now(UTC),
                    },
                )
            await session.rollback()

            # Cross-tenant decision insert is rejected by RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO approval_decisions "
                        "(decision_id, request_id, tenant_id, actor_id, decision) "
                        "VALUES (:did, :req, :tid, :actor, 'approved')"
                    ),
                    {
                        "did": uuid.uuid4(),
                        "req": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "actor": uuid.uuid4(),
                    },
                )
            await session.rollback()

    async def test_integrations_isolated_by_rls(self, migrated_db) -> None:
        """Task 6.11 — integrations are tenant-isolated by RLS."""
        url = migrated_db["url"]
        await _seed_integrations_rls(url)
        async for session in self._app_session(migrated_db["name"]):
            count = await session.execute(text("SELECT count(*) FROM integrations"))
            assert count.scalar_one() == 0, "Missing RLS context must fail closed"

            await set_rls_on_session(session, _TENANT_A)
            rows = (
                (await session.execute(text("SELECT integration_id FROM integrations")))
                .scalars()
                .all()
            )
            assert len(rows) == 1, "RLS must isolate integrations to the tenant"

            # Cross-tenant direct lookup of tenant-B's integration returns nothing
            row = await session.execute(
                text("SELECT integration_id FROM integrations WHERE venue_id = :vid"),
                {"vid": uuid.UUID(_VENUE_B1)},
            )
            assert row.scalar_one_or_none() is None
            await session.rollback()

            # Cross-tenant INSERT is rejected by the RLS WITH CHECK
            await set_rls_on_session(session, _TENANT_A)
            with pytest.raises(Exception, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO integrations "
                        "(integration_id, tenant_id, venue_id, provider_type, "
                        "provider_name) VALUES (:iid, :tid, :vid, 'pos', 'lightspeed')"
                    ),
                    {
                        "iid": uuid.uuid4(),
                        "tid": uuid.UUID(_TENANT_B),
                        "vid": uuid.UUID(_VENUE_B1),
                    },
                )
            await session.rollback()

    async def test_audit_outbox_inbox_are_not_rls_scoped(self, migrated_db) -> None:
        """Task 6.12 — audit/outbox/inbox are PLATFORM INFRASTRUCTURE, not
        tenant-scoped RLS tables (governance 3.12/3.13: "tenant_id recorded
        for scoping/claims"). Workers poll across ALL tenants without an
        app.tenant_id context, so RLS would fail closed and break them.
        This test locks in that deliberate design decision."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        # Seed rows for BOTH tenants as the admin role.
        await _insert_outbox(
            url,
            event_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            event_type="operational.event",
        )
        await _insert_outbox(
            url,
            event_id=uuid.uuid4(),
            tenant_id=ids["tenant_b"],
            event_type="operational.event",
        )
        await _insert_inbox(
            url,
            tenant_id=ids["tenant_a"],
            source="pos.lightspeed",
            source_message_id="msg-a",
        )
        await _insert_inbox(
            url,
            tenant_id=ids["tenant_b"],
            source="pos.lightspeed",
            source_message_id="msg-b",
        )
        await _insert_audit(
            url,
            actor_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            action="user.login",
            action_category="authentication",
        )
        await _insert_audit(
            url,
            actor_id=uuid.uuid4(),
            tenant_id=ids["tenant_b"],
            action="user.login",
            action_category="authentication",
        )
        async for session in self._app_session(migrated_db["name"]):
            # No RLS context set at all — workers see every tenant's rows.
            outbox_count = await session.execute(text("SELECT count(*) FROM outbox_events"))
            assert outbox_count.scalar_one() == 2, "Outbox worker must see all tenants"
            inbox_count = await session.execute(text("SELECT count(*) FROM inbox_messages"))
            assert inbox_count.scalar_one() == 2, "Inbox worker must see all tenants"
            # Audit is globally append-only — the Security Auditor reads all.
            audit_count = await session.execute(text("SELECT count(*) FROM audit_events"))
            assert audit_count.scalar_one() == 2, "Audit is a global log"

    async def test_audit_append_only_for_app_role(self, migrated_db) -> None:
        """Task 6.12 — append-only audit: hotelops_app has SELECT + INSERT
        grants ONLY. UPDATE/DELETE are denied at the database (governance
        3.11: immutable append-only log)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        audit_id = await _insert_audit(
            url,
            actor_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            action="user.login",
            action_category="authentication",
        )
        async for session in self._app_session(migrated_db["name"]):
            # SELECT is granted — the Security Auditor role can review.
            count = await session.execute(text("SELECT count(*) FROM audit_events"))
            assert count.scalar_one() == 1

            # UPDATE is NOT granted — the app role cannot tamper with the log.
            with pytest.raises(Exception, match="permission denied"):
                await session.execute(
                    text("UPDATE audit_events SET action = 'tampered' WHERE audit_id = :aid"),
                    {"aid": audit_id},
                )
            await session.rollback()

            # DELETE is NOT granted either.
            with pytest.raises(Exception, match="permission denied"):
                await session.execute(
                    text("DELETE FROM audit_events WHERE audit_id = :aid"),
                    {"aid": audit_id},
                )
            await session.rollback()


# =============================================================================
# Migration failure atomicity (temp script directory with a broken migration)
# ============================================================================= (temp script directory with a broken migration)
# =============================================================================

_BROKEN_MIGRATION = f"""\
\"\"\"Intentional failing migration for atomicity tests (never committed).\"\"\"

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = \"005_broken\"
down_revision: str | None = \"{EXPECTED_MIGRATION_HEAD}\"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        \"partial_table\",
        sa.Column(\"id\", sa.Integer(), primary_key=True),
    )
    raise RuntimeError(\"intentional migration failure\")


def downgrade() -> None:
    pass
"""

_ROLL_FORWARD_MIGRATION = f"""\
\"\"\"Roll-forward-only migration: downgrade is unsafe by design.\"\"\"

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = \"005_rollforward\"
down_revision: str | None = \"{EXPECTED_MIGRATION_HEAD}\"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        \"rollforward_table\",
        sa.Column(\"id\", sa.Integer(), primary_key=True),
    )


def downgrade() -> None:
    raise RuntimeError(\"unsafe to downgrade — roll-forward only\")
"""


@pytest_asyncio.fixture
async def temp_migrations(tmp_path):
    """A throwaway migrations directory: env.py + copies of the real scripts."""
    versions = tmp_path / "versions"
    versions.mkdir()
    shutil.copy(MIGRATIONS_DIR / "env.py", tmp_path / "env.py")
    for name in (
        "001_create_identity_tables.py",
        "002_enable_rls.py",
        "003_membership_venue_scope.py",
        "004_tenancy_check_constraints.py",
        "005_video_domain_schema.py",
        "006_video_rls.py",
        "007_operational_config_schema.py",
        "008_operational_events.py",
        "009_evidence_persistence.py",
        "010_analytics_storage.py",
        "011_ai_domain_storage.py",
        "012_alert_approval_storage.py",
        "013_integration_storage.py",
        "014_audit_outbox_inbox.py",
        "015_constraint_index_review.py",
        "016_outbox_retry_idempotency.py",
    ):
        shutil.copy(MIGRATIONS_DIR / "versions" / name, versions / name)
    return tmp_path


class TestMigrationAtomicity:
    """A failing migration must roll back atomically — no partial schema."""

    async def test_failing_migration_rolls_back_atomically(self, fresh_db, temp_migrations) -> None:
        url = fresh_db["url"]
        versions_dir = temp_migrations / "versions"
        (versions_dir / "003_broken.py").write_text(_BROKEN_MIGRATION)
        script_location = str(temp_migrations)

        # Upgrade to the expected head explicitly: "head" would resolve to
        # 005_broken (the newest revision in the temp directory).
        await _upgrade(url, EXPECTED_MIGRATION_HEAD, script_location=script_location)
        assert await _version(url) == EXPECTED_MIGRATION_HEAD

        with pytest.raises(RuntimeError, match="intentional migration failure"):
            await _upgrade(url, "005_broken", script_location=script_location)

        # Atomic rollback: version unchanged, no partial table, prior schema intact
        assert await _version(url) == EXPECTED_MIGRATION_HEAD
        assert not await _table_exists(url, "partial_table")
        assert await _table_exists(url, "tenants")


# =============================================================================
# Rollback (downgrade) where supported
# =============================================================================


class TestMigrationRollback:
    """Downgrades work where rollback is safe (non-destructive migrations)."""

    async def test_downgrade_001_to_base(self, fresh_db) -> None:
        url = fresh_db["url"]
        await _upgrade(url, PREVIOUS_HEAD)
        assert await _table_exists(url, "tenants")

        await _downgrade(url, "base")

        version_count = await _scalar(url, "SELECT count(*) FROM alembic_version")
        assert version_count == 0
        assert not await _table_exists(url, "tenants")

    async def test_downgrade_head_to_previous(self, fresh_db) -> None:
        url = fresh_db["url"]
        await _upgrade(url, "head")

        try:
            await _downgrade(url, PREVIOUS_HEAD)
        except Exception as exc:
            # hotelops_app is a cluster-wide role. DROP ROLE fails when other
            # databases in the same cluster still reference it (documented in
            # governance doc Section 12.2). Assert atomic rollback, then skip.
            if "some objects depend on it" not in str(exc):
                raise
            assert await _version(url) == EXPECTED_MIGRATION_HEAD
            assert await _rls_policy_names(url) == EXPECTED_RLS_POLICIES
            pytest.skip("cluster-wide role dependency from another database blocks DROP ROLE")
            return

        assert await _version(url) == PREVIOUS_HEAD
        assert await _rls_policy_names(url) == frozenset()
        assert await _table_exists(url, "tenants")
        # DROP ROLE is the final downgrade step — success implies cleanup.
        role_count = await _scalar(
            url, "SELECT count(*) FROM pg_roles WHERE rolname = 'hotelops_app'"
        )
        assert role_count == 0, "Downgrade should have dropped the app role"


# =============================================================================
# Roll-forward where downgrade is unsafe
# =============================================================================


class TestRollForwardOnly:
    """Roll-forward-only migrations apply cleanly and refuse downgrade."""

    async def test_roll_forward_only_migration(self, fresh_db, temp_migrations) -> None:
        url = fresh_db["url"]
        versions_dir = temp_migrations / "versions"
        (versions_dir / "003_rollforward.py").write_text(_ROLL_FORWARD_MIGRATION)
        script_location = str(temp_migrations)

        await _upgrade(url, "005_rollforward", script_location=script_location)
        assert await _version(url) == "005_rollforward"
        assert await _table_exists(url, "rollforward_table")

        # Downgrade is explicitly unsafe — the migration refuses and the
        # database remains at 005_rollforward (atomic rollback). Target the
        # previous revision ("head" would be a no-op since 005 is the head).
        with pytest.raises(RuntimeError, match="unsafe to downgrade"):
            await _downgrade(url, PREVIOUS_HEAD, script_location=script_location)
        assert await _version(url) == "005_rollforward"
        assert await _table_exists(url, "rollforward_table")


# =============================================================================
# Task 6.3 — tenancy schema invariants (migrations 003 + 004)
# =============================================================================


async def _seed_tenancy_scope(url: str) -> dict[str, uuid.UUID]:
    """Seed two tenants, two venues, one user, one role, one membership.

    Returns the ids needed to exercise the membership_venues constraints.
    """
    ids: dict[str, uuid.UUID] = {
        "tenant_a": uuid.uuid4(),
        "tenant_b": uuid.uuid4(),
        "venue_a": uuid.uuid4(),
        "venue_b": uuid.uuid4(),
        "user": uuid.uuid4(),
        "role": uuid.uuid4(),
        "membership_a": uuid.uuid4(),
    }
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((ids["tenant_a"], "Tenant A"), (ids["tenant_b"], "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (ids["venue_a"], ids["tenant_a"], "Venue A"),
                (ids["venue_b"], ids["tenant_b"], "Venue B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            await conn.execute(
                text(
                    "INSERT INTO users (user_id, display_name, email) VALUES (:id, :name, :email)"
                ),
                {"id": ids["user"], "name": "Scope User", "email": "scope@example.com"},
            )
            await conn.execute(
                text("INSERT INTO roles (role_id, name) VALUES (:id, 'admin')"),
                {"id": ids["role"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships "
                    "(membership_id, user_id, tenant_id, role_id, scope, status) "
                    "VALUES (:id, :uid, :tid, :rid, 'specific_venues', 'active')"
                ),
                {
                    "id": ids["membership_a"],
                    "uid": ids["user"],
                    "tid": ids["tenant_a"],
                    "rid": ids["role"],
                },
            )
    finally:
        await engine.dispose()
    return ids


async def _link_venue(url: str, membership_id, venue_id, tenant_id) -> None:
    """Insert a membership_venues row (admin role bypasses RLS)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO membership_venues (membership_id, venue_id, tenant_id) "
                    "VALUES (:mid, :vid, :tid)"
                ),
                {"mid": membership_id, "vid": venue_id, "tid": tenant_id},
            )
    finally:
        await engine.dispose()


class TestTenancySchema:
    """Task 6.3 invariants: no cross-tenant venue scope, CHECKs, composite FKs."""

    async def test_membership_venue_has_tenant_column(self, migrated_db) -> None:
        url = migrated_db["url"]
        data_type = await _scalar(
            url,
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'membership_venues' AND column_name = 'tenant_id'",
        )
        assert data_type == "uuid"

    async def test_composite_foreign_keys_exist(self, migrated_db) -> None:
        url = migrated_db["url"]
        fks = await _scalar(
            url,
            "SELECT string_agg(conname, ',') FROM pg_constraint "
            "WHERE conrelid = 'membership_venues'::regclass AND contype = 'f'",
        )
        assert "fk_membership_venues_membership_tenant" in str(fks)
        assert "fk_membership_venues_venue_tenant" in str(fks)

    async def test_venue_unique_constraints_exist(self, migrated_db) -> None:
        url = migrated_db["url"]
        indexes = await _all_indexes(url)
        assert "uq_venues_venue_tenant" in indexes
        assert "uq_memberships_membership_tenant" in indexes

    async def test_cross_tenant_link_rejected(self, migrated_db) -> None:
        """A tenant-A membership must never link to a tenant-B venue."""
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO membership_venues "
                            "(membership_id, venue_id, tenant_id) "
                            "VALUES (:mid, :vid, :tid)"
                        ),
                        {
                            "mid": ids["membership_a"],
                            "vid": ids["venue_b"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_link_tenant_mismatch_rejected(self, migrated_db) -> None:
        """A link row claiming a third tenant matches neither parent."""
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO membership_venues "
                            "(membership_id, venue_id, tenant_id) "
                            "VALUES (:mid, :vid, :tid)"
                        ),
                        {
                            "mid": ids["membership_a"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_b"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_same_tenant_link_allowed(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        await _link_venue(url, ids["membership_a"], ids["venue_a"], ids["tenant_a"])
        count = await _scalar(url, "SELECT count(*) FROM membership_venues")
        assert count == 1

    async def test_link_cascades_from_membership(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        await _link_venue(url, ids["membership_a"], ids["venue_a"], ids["tenant_a"])
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM memberships WHERE membership_id = :id"),
                    {"id": ids["membership_a"]},
                )
        finally:
            await engine.dispose()
        count = await _scalar(url, "SELECT count(*) FROM membership_venues")
        assert count == 0, "membership_venues must cascade when the membership is deleted"

    async def test_link_cascades_from_venue(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        await _link_venue(url, ids["membership_a"], ids["venue_a"], ids["tenant_a"])
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM venues WHERE venue_id = :id"),
                    {"id": ids["venue_a"]},
                )
        finally:
            await engine.dispose()
        count = await _scalar(url, "SELECT count(*) FROM membership_venues")
        assert count == 0, "membership_venues must cascade when the venue is deleted"

    async def test_name_not_empty_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # A failed statement aborts the surrounding transaction, so
                # each expected failure runs inside its own savepoint.
                # Empty and whitespace-only names are rejected on tenants...
                for bad_name in ("", "   "):
                    with pytest.raises(IntegrityError):
                        async with conn.begin_nested():
                            await conn.execute(
                                text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                                {"id": uuid.uuid4(), "name": bad_name},
                            )
                # ...and on venues.
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO venues (venue_id, tenant_id, name) "
                                "VALUES (:vid, :tid, :name)"
                            ),
                            {"vid": uuid.uuid4(), "tid": ids["tenant_a"], "name": ""},
                        )
                # Valid names still work.
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": uuid.uuid4(), "name": "Valid Tenant"},
                )
        finally:
            await engine.dispose()

    async def test_email_has_at_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO users (user_id, display_name, email) "
                            "VALUES (:id, :name, :email)"
                        ),
                        {"id": uuid.uuid4(), "name": "No Email", "email": "not-an-email"},
                    )
        finally:
            await engine.dispose()

    async def test_invalid_membership_references_rejected(self, migrated_db) -> None:
        """Memberships must reference existing users, tenants, and roles."""
        url = migrated_db["url"]
        ids = await _seed_tenancy_scope(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # Nonexistent role
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO memberships "
                                "(membership_id, user_id, tenant_id, role_id, scope, status) "
                                "VALUES (:id, :uid, :tid, :rid, 'all_venues', 'active')"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "uid": ids["user"],
                                "tid": ids["tenant_a"],
                                "rid": uuid.uuid4(),
                            },
                        )
                # Nonexistent user
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO memberships "
                                "(membership_id, user_id, tenant_id, role_id, scope, status) "
                                "VALUES (:id, :uid, :tid, :rid, 'all_venues', 'active')"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "uid": uuid.uuid4(),
                                "tid": ids["tenant_a"],
                                "rid": ids["role"],
                            },
                        )
                # Nonexistent tenant
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO memberships "
                                "(membership_id, user_id, tenant_id, role_id, scope, status) "
                                "VALUES (:id, :uid, :tid, :rid, 'all_venues', 'active')"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "uid": ids["user"],
                                "tid": uuid.uuid4(),
                                "rid": ids["role"],
                            },
                        )
        finally:
            await engine.dispose()


# =============================================================================
# Task 6.4 - video domain schema invariants (migrations 005 + 006)
# =============================================================================


async def _seed_video(url: str) -> dict[str, uuid.UUID]:
    """Seed two tenants, venues, cameras, a stream, and assets.

    Includes a tenant-B recorded asset so cross-tenant source references
    can be exercised. Runs as admin (bypasses RLS).
    """
    ids: dict[str, uuid.UUID] = {
        "tenant_a": uuid.uuid4(),
        "tenant_b": uuid.uuid4(),
        "venue_a": uuid.uuid4(),
        "venue_b": uuid.uuid4(),
        "camera_a": uuid.uuid4(),
        "camera_b": uuid.uuid4(),
        "stream_a": uuid.uuid4(),
        "asset_live": uuid.uuid4(),
        "asset_recorded": uuid.uuid4(),
        "asset_b": uuid.uuid4(),
    }
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((ids["tenant_a"], "Tenant A"), (ids["tenant_b"], "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (ids["venue_a"], ids["tenant_a"], "Venue A"),
                (ids["venue_b"], ids["tenant_b"], "Venue B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (ids["camera_a"], ids["venue_a"], ids["tenant_a"], "Camera A"),
                (ids["camera_b"], ids["venue_b"], ids["tenant_b"], "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
            await conn.execute(
                text(
                    "INSERT INTO video_streams "
                    "(stream_id, camera_id, venue_id, tenant_id, name) "
                    "VALUES (:id, :cid, :vid, :tid, :name)"
                ),
                {
                    "id": ids["stream_a"],
                    "cid": ids["camera_a"],
                    "vid": ids["venue_a"],
                    "tid": ids["tenant_a"],
                    "name": "Stream A",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO video_assets "
                    "(asset_id, venue_id, tenant_id, name, source_type, camera_id) "
                    "VALUES (:id, :vid, :tid, :name, 'live', :cid)"
                ),
                {
                    "id": ids["asset_live"],
                    "vid": ids["venue_a"],
                    "tid": ids["tenant_a"],
                    "name": "Live Asset",
                    "cid": ids["camera_a"],
                },
            )
            for aid, tid, name, uri in (
                (
                    ids["asset_recorded"],
                    ids["tenant_a"],
                    "Recorded Asset",
                    "s3://hotelops/a/rec.mp4",
                ),
                (ids["asset_b"], ids["tenant_b"], "Asset B", "s3://hotelops/b/rec.mp4"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO video_assets "
                        "(asset_id, venue_id, tenant_id, name, source_type, storage_uri) "
                        "VALUES (:id, :vid, :tid, :name, 'recorded', :uri)"
                    ),
                    {
                        "id": aid,
                        "vid": ids["venue_a"] if tid == ids["tenant_a"] else ids["venue_b"],
                        "tid": tid,
                        "name": name,
                        "uri": uri,
                    },
                )
    finally:
        await engine.dispose()
    return ids


class TestVideoSchema:
    """Task 6.4 invariants: camera/asset/session tenancy, sources, constraints."""

    _VIDEO_TABLES = ("cameras", "video_streams", "video_assets", "video_sessions")

    async def test_video_tables_have_tenant_columns(self, migrated_db) -> None:
        url = migrated_db["url"]
        for table in self._VIDEO_TABLES:
            data_type = await _scalar(
                url,
                "SELECT data_type FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = 'tenant_id'",
            )
            assert data_type == "uuid", f"{table}.tenant_id"

    async def test_video_timestamps_are_utc(self, migrated_db) -> None:
        url = migrated_db["url"]
        for table, column in (
            ("cameras", "created_at"),
            ("video_streams", "created_at"),
            ("video_assets", "created_at"),
            ("video_sessions", "created_at"),
            ("video_sessions", "started_at"),
        ):
            data_type = await _scalar(
                url,
                "SELECT data_type FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = '{column}'",
            )
            assert data_type == "timestamp with time zone", f"{table}.{column}"

    async def test_camera_requires_valid_venue(self, migrated_db) -> None:
        """A camera must reference an existing venue (FK enforced)."""
        url = migrated_db["url"]
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                            "VALUES (:id, :vid, :tid, :name)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vid": uuid.uuid4(),
                            "tid": uuid.uuid4(),
                            "name": "Orphan Camera",
                        },
                    )
        finally:
            await engine.dispose()

    async def test_camera_venue_tenant_must_match(self, migrated_db) -> None:
        """A camera claiming a venue of another tenant is rejected."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                            "VALUES (:id, :vid, :tid, :name)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vid": ids["venue_b"],
                            "tid": ids["tenant_a"],
                            "name": "Cross Tenant Camera",
                        },
                    )
        finally:
            await engine.dispose()

    async def test_stream_camera_tenant_must_match(self, migrated_db) -> None:
        """A stream on a camera of another tenant is rejected."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO video_streams "
                            "(stream_id, camera_id, venue_id, tenant_id, name) "
                            "VALUES (:id, :cid, :vid, :tid, :name)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_b"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                            "name": "Cross Tenant Stream",
                        },
                    )
        finally:
            await engine.dispose()

    async def test_recorded_asset_requires_storage_reference(self, migrated_db) -> None:
        """Recorded assets must reference object storage — bytes never in PG."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="source_consistent"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_assets "
                                "(asset_id, venue_id, tenant_id, name, source_type) "
                                "VALUES (:id, :vid, :tid, :name, 'recorded')"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "name": "No Storage",
                            },
                        )
        finally:
            await engine.dispose()

    async def test_live_asset_requires_camera(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="source_consistent"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_assets "
                                "(asset_id, venue_id, tenant_id, name, source_type) "
                                "VALUES (:id, :vid, :tid, :name, 'live')"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "name": "No Camera",
                            },
                        )
        finally:
            await engine.dispose()

    async def test_asset_duration_non_negative(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="duration_non_negative"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_assets "
                                "(asset_id, venue_id, tenant_id, name, source_type, "
                                "storage_uri, duration_seconds) "
                                "VALUES (:id, :vid, :tid, :name, 'recorded', :uri, :dur)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "name": "Bad Duration",
                                "uri": "s3://hotelops/a/neg.mp4",
                                "dur": -1.5,
                            },
                        )
        finally:
            await engine.dispose()

    async def test_camera_pk_unique(self, migrated_db) -> None:
        """Camera identifiers are unique (UUID PK enforced)."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                                "VALUES (:id, :vid, :tid, :name)"
                            ),
                            {
                                "id": ids["camera_a"],
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "name": "Duplicate",
                            },
                        )
        finally:
            await engine.dispose()

    async def test_session_live_requires_camera(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="source_consistent"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_sessions "
                                "(session_id, venue_id, tenant_id, source_type, started_at) "
                                "VALUES (:id, :vid, :tid, 'live', :started)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "started": datetime.now(UTC),
                            },
                        )
        finally:
            await engine.dispose()

    async def test_session_recorded_requires_asset(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="source_consistent"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_sessions "
                                "(session_id, venue_id, tenant_id, source_type, started_at) "
                                "VALUES (:id, :vid, :tid, 'recorded', :started)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "started": datetime.now(UTC),
                            },
                        )
        finally:
            await engine.dispose()

    async def test_session_ended_after_started(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="ended_after_started"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_sessions "
                                "(session_id, venue_id, tenant_id, source_type, camera_id, "
                                "status, started_at, ended_at) "
                                "VALUES (:id, :vid, :tid, 'live', :cid, 'ended', :started, :ended)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "cid": ids["camera_a"],
                                "started": datetime.now(UTC),
                                "ended": datetime.now(UTC).replace(year=2000),
                            },
                        )
        finally:
            await engine.dispose()

    async def test_session_active_cannot_have_ended_at(self, migrated_db) -> None:
        """An active session must not carry an ended_at (status/ended_at link)."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="status_consistent"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_sessions "
                                "(session_id, venue_id, tenant_id, source_type, camera_id, "
                                "started_at, ended_at) "
                                "VALUES (:id, :vid, :tid, 'live', :cid, :started, :ended)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "cid": ids["camera_a"],
                                "started": datetime.now(UTC),
                                "ended": datetime.now(UTC),
                            },
                        )
        finally:
            await engine.dispose()

    async def test_session_cross_tenant_asset_rejected(self, migrated_db) -> None:
        """A session cannot reference an asset of another tenant."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO video_sessions "
                                "(session_id, venue_id, tenant_id, source_type, asset_id, "
                                "started_at) "
                                "VALUES (:id, :vid, :tid, 'recorded', :aid, :started)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "aid": ids["asset_b"],
                                "started": datetime.now(UTC),
                            },
                        )
        finally:
            await engine.dispose()

    async def test_valid_sessions_link_camera_and_asset(self, migrated_db) -> None:
        """Live sessions link a camera; recorded sessions link an asset."""
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO video_sessions "
                        "(session_id, venue_id, tenant_id, source_type, camera_id, started_at) "
                        "VALUES (:id, :vid, :tid, 'live', :cid, :started)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                        "cid": ids["camera_a"],
                        "started": datetime.now(UTC),
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO video_sessions "
                        "(session_id, venue_id, tenant_id, source_type, asset_id, started_at) "
                        "VALUES (:id, :vid, :tid, 'recorded', :aid, :started)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                        "aid": ids["asset_recorded"],
                        "started": datetime.now(UTC),
                    },
                )
            count = await _scalar(url, "SELECT count(*) FROM video_sessions")
            assert count == 2
        finally:
            await engine.dispose()

    async def test_session_status_enum_enforced(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_video(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO video_sessions "
                            "(session_id, venue_id, tenant_id, source_type, camera_id, "
                            "status, started_at) "
                            "VALUES (:id, :vid, :tid, 'live', :cid, 'bogus', :started)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                            "cid": ids["camera_a"],
                            "started": datetime.now(UTC),
                        },
                    )
        finally:
            await engine.dispose()


# =============================================================================
# Task 6.5 — operational configuration schema invariants (migration 007)
# =============================================================================


async def _seed_config(url: str) -> dict[str, uuid.UUID]:
    """Seed two tenants, venues, and one camera per venue for config tests."""
    ids: dict[str, uuid.UUID] = {
        "tenant_a": uuid.uuid4(),
        "tenant_b": uuid.uuid4(),
        "venue_a": uuid.uuid4(),
        "venue_b": uuid.uuid4(),
        "camera_a": uuid.uuid4(),
        "camera_b": uuid.uuid4(),
    }
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((ids["tenant_a"], "Tenant A"), (ids["tenant_b"], "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (ids["venue_a"], ids["tenant_a"], "Venue A"),
                (ids["venue_b"], ids["tenant_b"], "Venue B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (ids["camera_a"], ids["venue_a"], ids["tenant_a"], "Camera A"),
                (ids["camera_b"], ids["venue_b"], ids["tenant_b"], "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
    finally:
        await engine.dispose()
    return ids


async def _insert_camera_config(
    url: str,
    camera_id: uuid.UUID,
    venue_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    status: str = "draft",
    version: int = 1,
    frame_rate=None,
    sensitivity=None,
) -> None:
    """Insert a camera_configs row (admin role bypasses RLS)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO camera_configs "
                    "(config_id, camera_id, venue_id, tenant_id, status, version, "
                    "frame_rate, detection_sensitivity) "
                    "VALUES (:id, :cid, :vid, :tid, :status, :version, :fr, :sens)"
                ),
                {
                    "id": uuid.uuid4(),
                    "cid": camera_id,
                    "vid": venue_id,
                    "tid": tenant_id,
                    "status": status,
                    "version": version,
                    "fr": frame_rate,
                    "sens": sensitivity,
                },
            )
    finally:
        await engine.dispose()


class TestConfigSchema:
    """Task 6.5 invariants: typed config, invalid values, scope validation,
    unique active configuration rules, version semantics, timestamps."""

    _CONFIG_TABLES = ("camera_configs", "analysis_configs")

    async def test_config_tables_have_tenant_columns(self, migrated_db) -> None:
        url = migrated_db["url"]
        for table in self._CONFIG_TABLES:
            data_type = await _scalar(
                url,
                "SELECT data_type FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = 'tenant_id'",
            )
            assert data_type == "uuid", f"{table}.tenant_id"

    async def test_config_timestamps_are_utc(self, migrated_db) -> None:
        url = migrated_db["url"]
        for table in self._CONFIG_TABLES:
            for column in ("created_at", "updated_at"):
                data_type = await _scalar(
                    url,
                    "SELECT data_type FROM information_schema.columns "
                    f"WHERE table_name = '{table}' AND column_name = '{column}'",
                )
                assert data_type == "timestamp with time zone", f"{table}.{column}"

    async def test_updated_at_defaults_to_now(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO camera_configs "
                        "(config_id, camera_id, venue_id, tenant_id, status, version) "
                        "VALUES (:id, :cid, :vid, :tid, 'active', 1)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "cid": ids["camera_a"],
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                    },
                )
            # Only one config row exists — no bind parameters needed
            updated = await _scalar(url, "SELECT updated_at FROM camera_configs")
            assert updated is not None
            assert updated.tzinfo is not None, "updated_at must be timezone-aware"
            delta = abs((datetime.now(UTC) - updated).total_seconds())
            assert delta < 60, "updated_at is not the current UTC time"
        finally:
            await engine.dispose()

    # --- invalid configuration ---

    async def test_camera_config_frame_rate_must_be_positive(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="frame_rate_positive"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, version, frame_rate) "
                            "VALUES (:id, :cid, :vid, :tid, 1, :fr)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_a"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                            "fr": 0,
                        },
                    )
        finally:
            await engine.dispose()

    async def test_camera_config_sensitivity_must_be_in_range(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="sensitivity_range"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, version, "
                            "detection_sensitivity) "
                            "VALUES (:id, :cid, :vid, :tid, 1, :sens)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_a"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                            "sens": 1.5,
                        },
                    )
        finally:
            await engine.dispose()

    async def test_camera_config_version_must_be_positive(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="version_positive"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, version) "
                            "VALUES (:id, :cid, :vid, :tid, 0)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_a"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_analysis_thresholds_out_of_range_rejected(self, migrated_db) -> None:
        """Invalid thresholds are rejected — each failure in its own savepoint
        because a failed statement aborts the surrounding transaction."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # occupancy > 100 is invalid
                with pytest.raises(IntegrityError, match="occupancy_range"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO analysis_configs "
                                "(config_id, venue_id, tenant_id, name, version, "
                                "occupancy_threshold) "
                                "VALUES (:id, :vid, :tid, 'lobby', 1, :occ)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "occ": 150,
                            },
                        )
                # negative dwell time is invalid
                with pytest.raises(IntegrityError, match="dwell_non_negative"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO analysis_configs "
                                "(config_id, venue_id, tenant_id, name, version, "
                                "dwell_time_seconds) "
                                "VALUES (:id, :vid, :tid, 'lobby', 1, :dwell)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "dwell": -5,
                            },
                        )
                # confidence outside [0,1] is invalid
                with pytest.raises(IntegrityError, match="confidence_range"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO analysis_configs "
                                "(config_id, venue_id, tenant_id, name, version, "
                                "confidence_threshold) "
                                "VALUES (:id, :vid, :tid, 'lobby', 1, :conf)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "vid": ids["venue_a"],
                                "tid": ids["tenant_a"],
                                "conf": 2.0,
                            },
                        )
        finally:
            await engine.dispose()

    async def test_analysis_config_name_not_empty(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="name_not_empty"):
                    await conn.execute(
                        text(
                            "INSERT INTO analysis_configs "
                            "(config_id, venue_id, tenant_id, name, version) "
                            "VALUES (:id, :vid, :tid, :name, 1)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                            "name": "  ",
                        },
                    )
        finally:
            await engine.dispose()

    async def test_invalid_config_status_rejected(self, migrated_db) -> None:
        """The effective-state enum rejects unknown status values."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, status, version) "
                            "VALUES (:id, :cid, :vid, :tid, 'bogus', 1)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_a"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    # --- scope validation ---

    async def test_camera_config_camera_tenant_must_match(self, migrated_db) -> None:
        """A camera config cannot reference a camera of another tenant."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="foreign key"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, version) "
                            "VALUES (:id, :cid, :vid, :tid, 1)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_b"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_camera_config_venue_tenant_must_match(self, migrated_db) -> None:
        """A camera config cannot claim a venue of another tenant."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="foreign key"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, version) "
                            "VALUES (:id, :cid, :vid, :tid, 1)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_a"],
                            "vid": ids["venue_b"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_analysis_config_venue_tenant_must_match(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="foreign key"):
                    await conn.execute(
                        text(
                            "INSERT INTO analysis_configs "
                            "(config_id, venue_id, tenant_id, name, version) "
                            "VALUES (:id, :vid, :tid, 'lobby', 1)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vid": ids["venue_b"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    # --- unique active configuration rules ---

    async def test_only_one_active_camera_config_per_camera(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        await _insert_camera_config(
            url, ids["camera_a"], ids["venue_a"], ids["tenant_a"], status="active"
        )
        # A second active config for the same camera violates the unique-active rule
        with pytest.raises(IntegrityError, match="uq_camera_configs_active"):
            await _insert_camera_config(
                url, ids["camera_a"], ids["venue_a"], ids["tenant_a"], status="active", version=2
            )
        # ...but a draft (non-effective) version is allowed alongside it
        await _insert_camera_config(
            url, ids["camera_a"], ids["venue_a"], ids["tenant_a"], status="draft", version=2
        )
        # Another camera may have its own active config
        await _insert_camera_config(
            url, ids["camera_b"], ids["venue_b"], ids["tenant_b"], status="active"
        )
        count = await _scalar(url, "SELECT count(*) FROM camera_configs")
        assert count == 3

    async def test_only_one_active_analysis_config_per_venue_and_name(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO analysis_configs "
                        "(config_id, venue_id, tenant_id, name, status, version) "
                        "VALUES (:id, :vid, :tid, 'default', 'active', 1)"
                    ),
                    {"id": uuid.uuid4(), "vid": ids["venue_a"], "tid": ids["tenant_a"]},
                )
                # Same (venue, name) active profile is rejected
                with pytest.raises(IntegrityError, match="uq_analysis_configs_active"):
                    await conn.execute(
                        text(
                            "INSERT INTO analysis_configs "
                            "(config_id, venue_id, tenant_id, name, status, version) "
                            "VALUES (:id, :vid, :tid, 'default', 'active', 2)"
                        ),
                        {"id": uuid.uuid4(), "vid": ids["venue_a"], "tid": ids["tenant_a"]},
                    )
        finally:
            await engine.dispose()

    async def test_version_uniqueness_per_camera(self, migrated_db) -> None:
        """(camera_id, version) is unique — relational change history."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO camera_configs "
                        "(config_id, camera_id, venue_id, tenant_id, version) "
                        "VALUES (:id, :cid, :vid, :tid, 1)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "cid": ids["camera_a"],
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                    },
                )
                with pytest.raises(IntegrityError, match="uq_camera_configs_version"):
                    await conn.execute(
                        text(
                            "INSERT INTO camera_configs "
                            "(config_id, camera_id, venue_id, tenant_id, version) "
                            "VALUES (:id, :cid, :vid, :tid, 1)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "cid": ids["camera_a"],
                            "vid": ids["venue_a"],
                            "tid": ids["tenant_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_camera_config_cascades_from_camera(self, migrated_db) -> None:
        """Deleting a camera removes its configs (ON DELETE CASCADE)."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        await _insert_camera_config(
            url, ids["camera_a"], ids["venue_a"], ids["tenant_a"], status="active"
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM cameras WHERE camera_id = :id"),
                    {"id": ids["camera_a"]},
                )
        finally:
            await engine.dispose()
        count = await _scalar(url, "SELECT count(*) FROM camera_configs")
        assert count == 0, "camera_configs must cascade when the camera is deleted"

    async def test_valid_config_insert_succeeds(self, migrated_db) -> None:
        """A well-formed typed config with valid thresholds is accepted."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        await _insert_camera_config(
            url,
            ids["camera_a"],
            ids["venue_a"],
            ids["tenant_a"],
            status="active",
            frame_rate=25,
            sensitivity=0.8,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO analysis_configs "
                        "(config_id, venue_id, tenant_id, name, status, version, "
                        "occupancy_threshold, dwell_time_seconds, parameters) "
                        "VALUES (:id, :vid, :tid, 'default', 'active', 1, 80, 300, "
                        "CAST(:params AS jsonb))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                        "params": json.dumps({"zones": ["lobby", "queue"]}),
                    },
                )
        finally:
            await engine.dispose()
        count = await _scalar(url, "SELECT count(*) FROM camera_configs")
        assert count == 1
        count = await _scalar(url, "SELECT count(*) FROM analysis_configs")
        assert count == 1
        # analysis_enabled defaults to true via the server default
        enabled = await _scalar(url, "SELECT analysis_enabled FROM camera_configs")
        assert enabled is True, "analysis_enabled server default must be true"

    async def test_activating_draft_camera_config_when_active_exists_rejected(
        self, migrated_db
    ) -> None:
        """The unique-active rule also blocks the UPDATE draft->active path."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        await _insert_camera_config(
            url, ids["camera_a"], ids["venue_a"], ids["tenant_a"], status="active"
        )
        draft_id = uuid.uuid4()
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO camera_configs "
                        "(config_id, camera_id, venue_id, tenant_id, status, version) "
                        "VALUES (:id, :cid, :vid, :tid, 'draft', 2)"
                    ),
                    {
                        "id": draft_id,
                        "cid": ids["camera_a"],
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                    },
                )
                with pytest.raises(IntegrityError, match="uq_camera_configs_active"):
                    await conn.execute(
                        text("UPDATE camera_configs SET status = 'active' WHERE config_id = :id"),
                        {"id": draft_id},
                    )
        finally:
            await engine.dispose()

    async def test_activating_draft_analysis_config_when_active_exists_rejected(
        self, migrated_db
    ) -> None:
        """The unique-active rule also blocks UPDATE draft->active on analysis profiles."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        draft_id = uuid.uuid4()
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO analysis_configs "
                        "(config_id, venue_id, tenant_id, name, status, version) "
                        "VALUES (:id, :vid, :tid, 'default', 'active', 1)"
                    ),
                    {"id": uuid.uuid4(), "vid": ids["venue_a"], "tid": ids["tenant_a"]},
                )
                await conn.execute(
                    text(
                        "INSERT INTO analysis_configs "
                        "(config_id, venue_id, tenant_id, name, status, version) "
                        "VALUES (:id, :vid, :tid, 'default', 'draft', 2)"
                    ),
                    {"id": draft_id, "vid": ids["venue_a"], "tid": ids["tenant_a"]},
                )
                with pytest.raises(IntegrityError, match="uq_analysis_configs_active"):
                    await conn.execute(
                        text("UPDATE analysis_configs SET status = 'active' WHERE config_id = :id"),
                        {"id": draft_id},
                    )
        finally:
            await engine.dispose()


# =============================================================================
# Task 6.6 — operational event storage (migration 008, TimescaleDB hypertable)
# =============================================================================


async def _seed_events(url: str) -> dict[str, uuid.UUID]:
    """Seed two tenants, venues, cameras, and one session for event tests."""
    ids: dict[str, uuid.UUID] = {
        "tenant_a": uuid.uuid4(),
        "tenant_b": uuid.uuid4(),
        "venue_a": uuid.uuid4(),
        "venue_b": uuid.uuid4(),
        "camera_a": uuid.uuid4(),
        "camera_b": uuid.uuid4(),
        "session_a": uuid.uuid4(),
        "session_b": uuid.uuid4(),
    }
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            for tid, name in ((ids["tenant_a"], "Tenant A"), (ids["tenant_b"], "Tenant B")):
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": name},
                )
            for vid, tid, name in (
                (ids["venue_a"], ids["tenant_a"], "Venue A"),
                (ids["venue_b"], ids["tenant_b"], "Venue B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO venues (venue_id, tenant_id, name) VALUES (:vid, :tid, :name)"
                    ),
                    {"vid": vid, "tid": tid, "name": name},
                )
            for cid, vid, tid, name in (
                (ids["camera_a"], ids["venue_a"], ids["tenant_a"], "Camera A"),
                (ids["camera_b"], ids["venue_b"], ids["tenant_b"], "Camera B"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO cameras (camera_id, venue_id, tenant_id, name) "
                        "VALUES (:id, :vid, :tid, :name)"
                    ),
                    {"id": cid, "vid": vid, "tid": tid, "name": name},
                )
            for sid, vid, tid, cid in (
                (ids["session_a"], ids["venue_a"], ids["tenant_a"], ids["camera_a"]),
                (ids["session_b"], ids["venue_b"], ids["tenant_b"], ids["camera_b"]),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO video_sessions "
                        "(session_id, venue_id, tenant_id, source_type, camera_id, started_at) "
                        "VALUES (:id, :vid, :tid, 'live', :cid, :started)"
                    ),
                    {
                        "id": sid,
                        "vid": vid,
                        "tid": tid,
                        "cid": cid,
                        "started": datetime.now(UTC),
                    },
                )
    finally:
        await engine.dispose()
    return ids


async def _insert_event(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    event_time: datetime | None = None,
    event_type: str = "detection.observation",
    source: str = "cv.pipeline",
    session_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    processing_time: datetime | None = None,
    payload: dict | None = None,
    correlation_id: str | None = None,
) -> uuid.UUID:
    """Insert an operational_events row (admin role bypasses RLS).

    Returns the generated event_id so tests can build provenance links
    (e.g. evidence_refs.event_id).
    """
    engine = _query_engine(url)
    event_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            now = datetime.now(UTC)
            await conn.execute(
                text(
                    "INSERT INTO operational_events "
                    "(event_id, event_type, tenant_id, venue_id, session_id, camera_id, "
                    "event_time, produced_at, processing_time, correlation_id, source, payload) "
                    "VALUES (:id, :et, :tid, :vid, :sid, :cid, :time, :pa, :pt, :corr, :src, "
                    "CAST(:payload AS jsonb))"
                ),
                {
                    "id": event_id,
                    "et": event_type,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "sid": session_id,
                    "cid": camera_id,
                    "time": event_time or now,
                    "pa": event_time or now,
                    "pt": processing_time,
                    "corr": correlation_id,
                    "src": source,
                    "payload": json.dumps(
                        payload if payload is not None else {"class_name": "person"}
                    ),
                },
            )
    finally:
        await engine.dispose()
    return event_id


class TestEventSchema:
    """Task 6.6 invariants: hypertable, event time semantics, envelope-shaped
    typed columns, invalid schema, ordering, tenancy, high-volume writes."""

    async def test_events_table_is_hypertable(self, migrated_db) -> None:
        """operational_events is a TimescaleDB hypertable on event_time."""
        url = migrated_db["url"]
        row = await _scalar(
            url,
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'operational_events'",
        )
        assert row == "operational_events"

    async def test_events_table_single_time_dimension(self, migrated_db) -> None:
        url = migrated_db["url"]
        dims = await _scalar(
            url,
            "SELECT num_dimensions FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'operational_events'",
        )
        assert dims == 1, "The hypertable must partition on event_time only"

    async def test_events_create_chunks_on_insert(self, migrated_db) -> None:
        """Inserting an event materializes at least one hypertable chunk."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_event(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        chunks = await _scalar(
            url,
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'operational_events'",
        )
        assert chunks >= 1

    async def test_event_insertion_round_trip(self, migrated_db) -> None:
        """A full envelope-shaped event persists and reads back intact."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC)
        await _insert_event(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            session_id=ids["session_a"],
            camera_id=ids["camera_a"],
            event_time=event_time,
            correlation_id="corr-1",
            payload={"class_name": "person", "confidence": 0.95},
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT event_type, schema_version, venue_id, session_id, "
                            "camera_id, event_time, correlation_id, source, payload, "
                            "ingestion_time FROM operational_events"
                        )
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.event_type == "detection.observation"
        assert row.schema_version == "1.0"
        assert row.venue_id == ids["venue_a"]
        assert row.session_id == ids["session_a"]
        assert row.camera_id == ids["camera_a"]
        assert row.correlation_id == "corr-1"
        assert row.source == "cv.pipeline"
        assert row.payload == {"class_name": "person", "confidence": 0.95}
        assert row.event_time == event_time
        assert row.ingestion_time is not None, "ingestion_time must be server-defaulted"

    async def test_event_ordering_semantics(self, migrated_db) -> None:
        """Events are stored with their explicit event_time; ORDER BY event_time
        returns them in event-time order regardless of insertion order."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        base = datetime.now(UTC)
        # Insert out of event-time order
        for offset in (2, 0, 1):
            t = base.replace(microsecond=offset)
            await _insert_event(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                event_time=t,
                correlation_id=f"corr-{offset}",
            )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT correlation_id, event_time FROM operational_events "
                            "ORDER BY event_time ASC"
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
        assert [r.correlation_id for r in rows] == ["corr-0", "corr-1", "corr-2"]
        times = [r.event_time for r in rows]
        assert times == sorted(times), "ORDER BY event_time must be strictly ascending"

    async def test_ingestion_time_not_substitute_for_event_time(self, migrated_db) -> None:
        """Backdated event_time persists exactly; ingestion_time is the receipt
        time — never collapsed into a single created_at."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        backdated = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        await _insert_event(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], event_time=backdated
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT event_time, ingestion_time, produced_at FROM operational_events"
                        )
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.event_time == backdated, "event_time must be stored explicitly"
        assert row.produced_at == backdated
        delta = abs((datetime.now(UTC) - row.ingestion_time).total_seconds())
        assert delta < 60, "ingestion_time must reflect receipt (now), not event_time"

    async def test_processing_time_is_distinct(self, migrated_db) -> None:
        """processing_time is explicit and never before event_time."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC)
        processed = event_time + timedelta(seconds=5)
        await _insert_event(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            event_time=event_time,
            processing_time=processed,
        )
        stored = await _scalar(url, "SELECT processing_time FROM operational_events")
        assert stored == processed
        assert stored.tzinfo is not None

    async def test_processing_time_stamped_after_ingest(self, migrated_db) -> None:
        """processing_time is stamped later via UPDATE — the documented write
        path on the append-only log (the reason UPDATE is granted)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(seconds=60)
        await _insert_event(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], event_time=event_time
        )
        processed = event_time + timedelta(seconds=10)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                event_id = (
                    await conn.execute(text("SELECT event_id FROM operational_events"))
                ).scalar_one()
                await conn.execute(
                    text(
                        "UPDATE operational_events SET processing_time = :pt WHERE event_id = :id"
                    ),
                    {"pt": processed, "id": event_id},
                )
        finally:
            await engine.dispose()
        stored = await _scalar(url, "SELECT processing_time FROM operational_events")
        assert stored == processed
        assert stored.tzinfo is not None

    async def test_processing_time_update_before_event_rejected(self, migrated_db) -> None:
        """An UPDATE that stamps processing_time before event_time is rejected by
        the processing_not_before_event CHECK."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(seconds=60)
        await _insert_event(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], event_time=event_time
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                event_id = (
                    await conn.execute(text("SELECT event_id FROM operational_events"))
                ).scalar_one()
                with pytest.raises(IntegrityError, match="processing_not_before_event"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE operational_events SET processing_time = :pt "
                                "WHERE event_id = :id"
                            ),
                            {"pt": event_time - timedelta(seconds=1), "id": event_id},
                        )
        finally:
            await engine.dispose()

    async def test_event_timestamps_are_utc(self, migrated_db) -> None:
        url = migrated_db["url"]
        for column in ("event_time", "produced_at", "ingestion_time", "processing_time"):
            data_type = await _scalar(
                url,
                "SELECT data_type FROM information_schema.columns "
                f"WHERE table_name = 'operational_events' AND column_name = '{column}'",
            )
            assert data_type == "timestamp with time zone", f"{column}"

    async def test_ingestion_server_default_is_utc_now(self, migrated_db) -> None:
        url = migrated_db["url"]
        default_expr = await _scalar(
            url,
            "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef "
            "WHERE adrelid = 'operational_events'::regclass AND adnum = ("
            "SELECT attnum FROM pg_attribute "
            "WHERE attrelid = 'operational_events'::regclass AND attname = 'ingestion_time')",
        )
        assert "now()" in str(default_expr), "ingestion_time server default must be now()"

    # --- invalid schema ---

    async def test_invalid_event_type_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        for bad in ("", "   "):
            with pytest.raises(IntegrityError, match="event_type_not_empty"):
                await _insert_event(
                    url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], event_type=bad
                )

    async def test_invalid_source_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="source_not_empty"):
            await _insert_event(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], source="")

    async def test_event_time_null_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO operational_events "
                            "(event_id, event_type, tenant_id, venue_id, event_time, "
                            "produced_at, source, payload) "
                            "VALUES (:id, 'detection.observation', :tid, :vid, NULL, "
                            "now(), 'cv.pipeline', CAST(:payload AS jsonb))"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                            "payload": json.dumps({}),
                        },
                    )
        finally:
            await engine.dispose()

    async def test_payload_null_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO operational_events "
                            "(event_id, event_type, tenant_id, venue_id, event_time, "
                            "produced_at, source, payload) "
                            "VALUES (:id, 'detection.observation', :tid, :vid, now(), "
                            "now(), 'cv.pipeline', NULL)"
                        ),
                        {"id": uuid.uuid4(), "tid": ids["tenant_a"], "vid": ids["venue_a"]},
                    )
        finally:
            await engine.dispose()

    async def test_ingestion_before_event_rejected(self, migrated_db) -> None:
        """An event with event_time in the future fails: ingestion (now) would
        precede the real-world event."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        future = datetime.now(UTC).replace(year=2030)
        with pytest.raises(IntegrityError, match="ingestion_not_before_event"):
            await _insert_event(
                url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], event_time=future
            )

    async def test_processing_before_event_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        past = datetime(2020, 1, 1, tzinfo=UTC)
        with pytest.raises(IntegrityError, match="processing_not_before_event"):
            await _insert_event(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                processing_time=past,
            )

    async def test_cross_tenant_session_rejected(self, migrated_db) -> None:
        """An event cannot reference a session of another tenant: the composite
        FK (session_id, tenant_id) -> video_sessions rejects tenant-B sessions
        on tenant-A events."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_event(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                session_id=ids["session_b"],  # session_b belongs to tenant B
            )

    async def test_event_requires_valid_venue(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError):
            await _insert_event(url, tenant_id=ids["tenant_a"], venue_id=uuid.uuid4())

    async def test_events_cascade_from_camera(self, migrated_db) -> None:
        """Deleting a camera removes its events (ON DELETE CASCADE)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_event(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            camera_id=ids["camera_a"],
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM cameras WHERE camera_id = :id"),
                    {"id": ids["camera_a"]},
                )
        finally:
            await engine.dispose()
        count = await _scalar(url, "SELECT count(*) FROM operational_events")
        assert count == 0, "operational_events must cascade when the camera is deleted"

    async def test_high_volume_batched_insertion(self, migrated_db) -> None:
        """The hypertable absorbs high-volume batched inserts (5,000 events)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        total = 5000
        batch = 500
        now = datetime.now(UTC)
        engine = _query_engine(url)
        params = []
        for i in range(total):
            params.append({
                "id": uuid.uuid4(),
                "tid": ids["tenant_a"],
                "vid": ids["venue_a"],
                "et": now,
                "pa": now,
                "payload": json.dumps({"seq": i, "class_name": "person"}),
            })
        try:
            async with engine.begin() as conn:
                for start in range(0, total, batch):
                    await conn.execute(
                        text(
                            "INSERT INTO operational_events "
                            "(event_id, event_type, tenant_id, venue_id, event_time, "
                            "produced_at, source, payload) "
                            "VALUES (:id, 'detection.observation', :tid, :vid, :et, :pa, "
                            "'cv.pipeline', CAST(:payload AS jsonb))"
                        ),
                        params[start : start + batch],
                    )
        finally:
            await engine.dispose()
        count = await _scalar(url, "SELECT count(*) FROM operational_events")
        assert count == total
        # The hypertable materialized chunks for the inserted volume
        chunks = await _scalar(
            url,
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'operational_events'",
        )
        assert chunks >= 1


# =============================================================================
# Task 6.7 — evidence persistence (migration 009)
# =============================================================================

_SHA256_EXAMPLE = "a" * 64


async def _insert_evidence_ref(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    ref_type: str = "frame",
    ref_uri: str | None = None,
    content_type: str | None = None,
    size_bytes: int | None = None,
    checksum: str | None = None,
    session_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    event_time: datetime | None = None,
    metadata_: dict | None = None,
) -> uuid.UUID:
    """Insert an evidence_refs row (admin role bypasses RLS).

    Returns the generated ref_id.
    """
    engine = _query_engine(url)
    ref_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO evidence_refs "
                    "(ref_id, tenant_id, venue_id, ref_type, ref_uri, content_type, "
                    "size_bytes, checksum, session_id, camera_id, event_id, event_time, "
                    '"metadata") '
                    "VALUES (:rid, :tid, :vid, :rtype, :uri, :ctype, :size, :sum, "
                    ":sid, :cid, :eid, :et, CAST(:meta AS jsonb))"
                ),
                {
                    "rid": ref_id,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "rtype": ref_type,
                    # Only default when ref_uri is None — an explicit empty
                    # string must reach the DB so the NOT EMPTY CHECK fires.
                    "uri": ref_uri
                    if ref_uri is not None
                    else f"s3://hotelops/evidence/{ref_id}.jpg",
                    "ctype": content_type,
                    "size": size_bytes,
                    "sum": checksum,
                    "sid": session_id,
                    "cid": camera_id,
                    "eid": event_id,
                    "et": event_time,
                    "meta": json.dumps(metadata_) if metadata_ is not None else None,
                },
            )
    finally:
        await engine.dispose()
    return ref_id


async def _insert_evidence_package(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    description: str | None = None,
) -> uuid.UUID:
    """Insert an evidence_packages row (admin role bypasses RLS).

    Returns the generated package_id.
    """
    engine = _query_engine(url)
    package_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO evidence_packages "
                    "(package_id, tenant_id, venue_id, description) "
                    "VALUES (:pid, :tid, :vid, :desc)"
                ),
                {"pid": package_id, "tid": tenant_id, "vid": venue_id, "desc": description},
            )
    finally:
        await engine.dispose()
    return package_id


async def _link_evidence(
    url: str, *, package_id: uuid.UUID, ref_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    """Insert a package_evidence_refs row (admin role bypasses RLS)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO package_evidence_refs (package_id, ref_id, tenant_id) "
                    "VALUES (:pid, :rid, :tid)"
                ),
                {"pid": package_id, "rid": ref_id, "tid": tenant_id},
            )
    finally:
        await engine.dispose()


class TestEvidenceSchema:
    """Task 6.7 invariants: artifact metadata in PG, ownership, FKs, artifact
    reference validation, tenant isolation, orphan prevention."""

    # --- insertion round-trips ---

    async def test_evidence_ref_round_trip(self, migrated_db) -> None:
        """A fully-populated artifact reference persists and reads back."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            ref_type="video_clip",
            content_type="video/mp4",
            size_bytes=1024 * 1024,
            checksum=_SHA256_EXAMPLE,
            session_id=ids["session_a"],
            camera_id=ids["camera_a"],
            metadata_={"duration_s": 12.5, "resolution": "1920x1080"},
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT schema_version, venue_id, ref_type, ref_uri, "
                            "content_type, size_bytes, checksum, session_id, camera_id, "
                            '"metadata" FROM evidence_refs WHERE ref_id = :rid'
                        ),
                        {"rid": ref_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.schema_version == "1.0"
        assert row.venue_id == ids["venue_a"]
        assert row.ref_type == "video_clip"
        assert row.content_type == "video/mp4"
        assert row.size_bytes == 1024 * 1024
        assert row.checksum == _SHA256_EXAMPLE
        assert row.session_id == ids["session_a"]
        assert row.camera_id == ids["camera_a"]
        assert row.metadata == {"duration_s": 12.5, "resolution": "1920x1080"}

    async def test_package_and_link_round_trip(self, migrated_db) -> None:
        """A package, its ref, and the M2M link all persist."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        package_id = await _insert_evidence_package(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Lobby occupancy evidence",
        )
        await _link_evidence(url, package_id=package_id, ref_id=ref_id, tenant_id=ids["tenant_a"])

        assert await _scalar(url, "SELECT count(*) FROM evidence_refs") == 1
        assert await _scalar(url, "SELECT count(*) FROM evidence_packages") == 1
        linked = await _scalar(url, "SELECT count(*) FROM package_evidence_refs")
        assert linked == 1

    # --- ownership ---

    async def test_tenant_id_required_on_all_evidence_tables(self, migrated_db) -> None:
        """Direct tenant ownership: NULL tenant_id is rejected everywhere."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # Each expected failure runs inside its own savepoint — a
                # failed statement aborts the surrounding transaction.
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO evidence_refs "
                                "(ref_id, tenant_id, venue_id, ref_type, ref_uri) "
                                "VALUES (:rid, NULL, :vid, 'frame', 's3://b/k.jpg')"
                            ),
                            {"rid": uuid.uuid4(), "vid": ids["venue_a"]},
                        )
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO evidence_packages "
                                "(package_id, tenant_id, venue_id) VALUES (:pid, NULL, :vid)"
                            ),
                            {"pid": uuid.uuid4(), "vid": ids["venue_a"]},
                        )
        finally:
            await engine.dispose()

    async def test_cross_tenant_venue_reference_rejected(self, migrated_db) -> None:
        """Evidence for tenant A cannot reference tenant B's venue: the
        composite FK (venue_id, tenant_id) -> venues rejects it."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_b"])

    async def test_cross_tenant_camera_reference_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_evidence_ref(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                camera_id=ids["camera_b"],  # camera_b belongs to tenant B
            )

    async def test_cross_tenant_session_reference_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_evidence_ref(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                session_id=ids["session_b"],  # session_b belongs to tenant B
            )

    async def test_cross_tenant_package_link_rejected(self, migrated_db) -> None:
        """A tenant-A package can never link a tenant-B evidence ref: the
        composite FK (ref_id, tenant_id) rejects it."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_a = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        ref_b = await _insert_evidence_ref(url, tenant_id=ids["tenant_b"], venue_id=ids["venue_b"])
        package_a = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"]
        )
        await _link_evidence(url, package_id=package_a, ref_id=ref_a, tenant_id=ids["tenant_a"])
        with pytest.raises(IntegrityError, match="foreign key"):
            await _link_evidence(
                url, package_id=package_a, ref_id=ref_b, tenant_id=ids["tenant_a"]
            )  # --- foreign keys ---

    async def test_invalid_venue_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError):
            await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=uuid.uuid4())

    async def test_invalid_event_reference_rejected(self, migrated_db) -> None:
        """A ref cannot point at a non-existent operational event."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_evidence_ref(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                event_id=uuid.uuid4(),
                event_time=datetime.now(UTC) - timedelta(seconds=30),
            )

    async def test_event_link_pair_required(self, migrated_db) -> None:
        """event_id without event_time is rejected — the FK columns are an
        atomic pair."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="event_pair"):
            await _insert_evidence_ref(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                event_id=uuid.uuid4(),  # no event_time
            )

    async def test_event_link_pair_required_reverse(self, migrated_db) -> None:
        """event_time without event_id is equally rejected — the pair is
        symmetric."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="event_pair"):
                    await conn.execute(
                        text(
                            "INSERT INTO evidence_refs "
                            "(ref_id, tenant_id, venue_id, ref_type, ref_uri, "
                            "event_time) "
                            "VALUES (:rid, :tid, :vid, 'frame', 's3://b/k.jpg', :et)"
                        ),
                        {
                            "rid": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                            "et": datetime.now(UTC) - timedelta(seconds=30),
                        },
                    )
        finally:
            await engine.dispose()

    async def test_event_linkage_round_trip(self, migrated_db) -> None:
        """A ref can cite its source operational event via the hypertable PK
        pair (event_time, event_id)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(seconds=30)
        event_id = await _insert_event(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            event_time=event_time,
            event_type="detection.observation",
        )
        ref_id = await _insert_evidence_ref(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            event_id=event_id,
            event_time=event_time,
        )
        stored = await _scalar(
            url, f"SELECT event_id FROM evidence_refs WHERE ref_id = '{ref_id}'::uuid"
        )
        assert stored == event_id

    # --- artifact reference validation ---

    async def test_empty_ref_uri_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="uri_not_empty"):
            await _insert_evidence_ref(
                url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], ref_uri=""
            )

    async def test_invalid_checksum_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="checksum_sha256"):
            await _insert_evidence_ref(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                checksum="not-a-sha256",
            )

    async def test_valid_checksum_accepted(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            checksum=_SHA256_EXAMPLE,
        )
        stored = await _scalar(
            url, f"SELECT checksum FROM evidence_refs WHERE ref_id = '{ref_id}'::uuid"
        )
        assert stored == _SHA256_EXAMPLE

    async def test_negative_size_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="size_non_negative"):
            await _insert_evidence_ref(
                url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], size_bytes=-1
            )

    async def test_blank_package_description_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="description_not_empty"):
                    await conn.execute(
                        text(
                            "INSERT INTO evidence_packages "
                            "(package_id, tenant_id, venue_id, description) "
                            "VALUES (:pid, :tid, :vid, '')"
                        ),
                        {"pid": uuid.uuid4(), "tid": ids["tenant_a"], "vid": ids["venue_a"]},
                    )
        finally:
            await engine.dispose()

    async def test_asset_evidence_ref_bare_column_round_trip(self, migrated_db) -> None:
        """video_assets.evidence_ref persists as a bare UUID (deliberately no
        FK — see migration 009 note on the dependency cycle)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO video_assets "
                        "(asset_id, venue_id, tenant_id, name, source_type, "
                        "storage_uri, evidence_ref) "
                        "VALUES (:aid, :vid, :tid, 'Asset A', 'recorded', "
                        "'s3://hotelops/a/rec.mp4', :ref)"
                    ),
                    {
                        "aid": uuid.uuid4(),
                        "vid": ids["venue_a"],
                        "tid": ids["tenant_a"],
                        "ref": ref_id,
                    },
                )
        finally:
            await engine.dispose()
        stored = await _scalar(url, "SELECT evidence_ref FROM video_assets")
        assert stored == ref_id

    # --- orphan prevention ---

    async def test_venue_delete_cascades_evidence(self, migrated_db) -> None:
        """Deleting a venue removes its refs, packages, and links."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        package_id = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"]
        )
        await _link_evidence(url, package_id=package_id, ref_id=ref_id, tenant_id=ids["tenant_a"])

        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM venues WHERE venue_id = :vid"), {"vid": ids["venue_a"]}
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM evidence_refs") == 0
        assert await _scalar(url, "SELECT count(*) FROM evidence_packages") == 0
        assert await _scalar(url, "SELECT count(*) FROM package_evidence_refs") == 0

    async def test_package_delete_cascades_link(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        package_id = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"]
        )
        await _link_evidence(url, package_id=package_id, ref_id=ref_id, tenant_id=ids["tenant_a"])

        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM evidence_packages WHERE package_id = :pid"),
                    {"pid": package_id},
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM package_evidence_refs") == 0
        # The artifact itself survives package deletion.
        assert await _scalar(url, "SELECT count(*) FROM evidence_refs") == 1

    async def test_ref_delete_cascades_link(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_id = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        package_id = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"]
        )
        await _link_evidence(url, package_id=package_id, ref_id=ref_id, tenant_id=ids["tenant_a"])

        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM evidence_refs WHERE ref_id = :rid"), {"rid": ref_id}
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM package_evidence_refs") == 0
        assert await _scalar(url, "SELECT count(*) FROM evidence_packages") == 1


# =============================================================================
# Task 6.8 — analytics storage (migration 010)
# =============================================================================


async def _insert_metric(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    metric_name: str,
    value: float,
    event_time: datetime,
    unit: str | None = None,
    session_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> uuid.UUID:
    """Insert a metrics row (admin role bypasses RLS).

    Returns the generated metric_id.
    """
    engine = _query_engine(url)
    metric_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO metrics "
                    "(metric_id, metric_name, value, unit, event_time, "
                    "window_start, window_end, tenant_id, venue_id, session_id, camera_id) "
                    "VALUES (:mid, :name, :val, :unit, :et, :ws, :we, :tid, :vid, :sid, :cid)"
                ),
                {
                    "mid": metric_id,
                    "name": metric_name,
                    "val": value,
                    "unit": unit,
                    "et": event_time,
                    "ws": window_start,
                    "we": window_end,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "sid": session_id,
                    "cid": camera_id,
                },
            )
    finally:
        await engine.dispose()
    return metric_id


async def _insert_opportunity(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    description: str,
    event_time: datetime,
) -> uuid.UUID:
    """Insert an opportunities row (admin role bypasses RLS).

    Returns the generated opportunity_id.
    """
    engine = _query_engine(url)
    opportunity_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO opportunities "
                    "(opportunity_id, tenant_id, venue_id, description, event_time) "
                    "VALUES (:oid, :tid, :vid, :desc, :et)"
                ),
                {
                    "oid": opportunity_id,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "desc": description,
                    "et": event_time,
                },
            )
    finally:
        await engine.dispose()
    return opportunity_id


async def _link_opportunity_metric(
    url: str,
    *,
    opportunity_id: uuid.UUID,
    event_time: datetime,
    metric_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Insert an opportunity_metrics row (admin role bypasses RLS)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO opportunity_metrics "
                    "(opportunity_id, event_time, metric_id, tenant_id) "
                    "VALUES (:oid, :et, :mid, :tid)"
                ),
                {
                    "oid": opportunity_id,
                    "et": event_time,
                    "mid": metric_id,
                    "tid": tenant_id,
                },
            )
    finally:
        await engine.dispose()


async def _link_opportunity_evidence(
    url: str,
    *,
    opportunity_id: uuid.UUID,
    ref_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Insert an opportunity_evidence_refs row (admin role bypasses RLS)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO opportunity_evidence_refs "
                    "(opportunity_id, ref_id, tenant_id) VALUES (:oid, :rid, :tid)"
                ),
                {"oid": opportunity_id, "rid": ref_id, "tid": tenant_id},
            )
    finally:
        await engine.dispose()


class TestAnalyticsSchema:
    """Task 6.8 invariants: metrics hypertable, opportunity records, time
    queries, aggregation correctness, tenancy, migration."""

    # --- hypertable + insertion ---

    async def test_metrics_table_is_hypertable(self, migrated_db) -> None:
        """metrics is a TimescaleDB hypertable on event_time."""
        url = migrated_db["url"]
        row = await _scalar(
            url,
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'metrics'",
        )
        assert row == "metrics"

    async def test_metrics_table_single_time_dimension(self, migrated_db) -> None:
        url = migrated_db["url"]
        dims = await _scalar(
            url,
            "SELECT num_dimensions FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'metrics'",
        )
        assert dims == 1, "The hypertable must partition on event_time only"

    async def test_metrics_create_chunks_on_insert(self, migrated_db) -> None:
        """Inserting a sample materializes at least one hypertable chunk."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_metric(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            metric_name="occupancy_rate",
            value=0.75,
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        chunks = await _scalar(
            url,
            "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'metrics'",
        )
        assert chunks >= 1

    async def test_metric_insertion_round_trip(self, migrated_db) -> None:
        """A fully-populated metric sample persists and reads back."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        metric_id = await _insert_metric(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            metric_name="avg_dwell_time",
            value=42.5,
            unit="minutes",
            session_id=ids["session_a"],
            camera_id=ids["camera_a"],
            event_time=event_time,
            window_start=event_time - timedelta(minutes=60),
            window_end=event_time,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT metric_name, value, unit, event_time, "
                            "window_start, window_end, session_id, camera_id, "
                            "ingestion_time FROM metrics WHERE metric_id = :mid"
                        ),
                        {"mid": metric_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.metric_name == "avg_dwell_time"
        assert abs(row.value - 42.5) < 1e-9
        assert row.unit == "minutes"
        assert row.event_time == event_time
        assert row.window_start == event_time - timedelta(minutes=60)
        assert row.window_end == event_time
        assert row.session_id == ids["session_a"]
        assert row.camera_id == ids["camera_a"]
        assert row.ingestion_time is not None, "ingestion_time must be server-defaulted"

    # --- time queries ---

    async def test_metric_time_range_queries(self, migrated_db) -> None:
        """Range queries over event_time return only in-window samples."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        t1 = datetime.now(UTC) - timedelta(minutes=10)
        t2 = t1 + timedelta(minutes=5)
        t3 = t2 + timedelta(minutes=5)
        for t in (t1, t2, t3):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="occupancy_rate",
                value=0.5,
                event_time=t,
            )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                between = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM metrics "
                            "WHERE event_time >= :lo AND event_time <= :hi"
                        ),
                        {"lo": t1, "hi": t2},
                    )
                ).scalar_one()
                after = (
                    await conn.execute(
                        text("SELECT count(*) FROM metrics WHERE event_time >= :t"), {"t": t3}
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        assert between == 2
        assert after == 1

    # --- aggregation correctness ---

    async def test_aggregation_correctness(self, migrated_db) -> None:
        """AVG over a time range aggregates correctly per metric/venue."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        base = datetime.now(UTC) - timedelta(minutes=30)
        for i, value in enumerate((0.5, 0.7, 0.9)):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="occupancy_rate",
                value=value,
                event_time=base + timedelta(minutes=i),
            )
        # Tenant B contributes a sample too — proves venue scoping in the query.
        await _insert_metric(
            url,
            tenant_id=ids["tenant_b"],
            venue_id=ids["venue_b"],
            metric_name="occupancy_rate",
            value=1.0,
            event_time=base,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT venue_id, count(*) AS n, avg(value) AS mean "
                            "FROM metrics WHERE event_time >= :from_time "
                            "GROUP BY venue_id ORDER BY venue_id"
                        ),
                        {"from_time": base},
                    )
                ).all()
        finally:
            await engine.dispose()
        assert len(rows) == 2
        venue_a = next(r for r in rows if r.venue_id == ids["venue_a"])
        assert venue_a.n == 3
        assert abs(venue_a.mean - 0.7) < 1e-9
        venue_b = next(r for r in rows if r.venue_id == ids["venue_b"])
        assert venue_b.n == 1
        assert abs(venue_b.mean - 1.0) < 1e-9

    # --- invalid schema ---

    async def test_empty_metric_name_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="metric_name_not_empty"):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="",
                value=1.0,
                event_time=datetime.now(UTC),
            )

    async def test_window_pair_required(self, migrated_db) -> None:
        """window_start without window_end is rejected — the pair is atomic."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="window_ordered"):
                    await conn.execute(
                        text(
                            "INSERT INTO metrics "
                            "(metric_id, metric_name, value, event_time, tenant_id, "
                            "venue_id, window_start) "
                            "VALUES (:mid, 'occupancy_rate', 0.5, :et, :tid, :vid, :ws)"
                        ),
                        {
                            "mid": uuid.uuid4(),
                            "et": datetime.now(UTC),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                            "ws": datetime.now(UTC) - timedelta(minutes=60),
                        },
                    )
        finally:
            await engine.dispose()

    async def test_window_pair_required_reverse(self, migrated_db) -> None:
        """window_end without window_start is equally rejected — the pair is
        symmetric."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="window_ordered"):
                    await conn.execute(
                        text(
                            "INSERT INTO metrics "
                            "(metric_id, metric_name, value, event_time, tenant_id, "
                            "venue_id, window_end) "
                            "VALUES (:mid, 'occupancy_rate', 0.5, :et, :tid, :vid, :we)"
                        ),
                        {
                            "mid": uuid.uuid4(),
                            "et": datetime.now(UTC),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                            "we": datetime.now(UTC),
                        },
                    )
        finally:
            await engine.dispose()

    async def test_window_ordering_rejected(self, migrated_db) -> None:
        """A window whose end precedes its start is rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="window_ordered"):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="occupancy_rate",
                value=0.5,
                event_time=datetime.now(UTC),
                window_start=datetime.now(UTC),
                window_end=datetime.now(UTC) - timedelta(minutes=5),
            )

    async def test_future_event_time_rejected(self, migrated_db) -> None:
        """A future-dated sample is rejected (ingestion would precede it)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="ingestion_not_before_event"):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="occupancy_rate",
                value=0.5,
                event_time=datetime.now(UTC).replace(year=2031),
            )

    # --- tenancy ---

    async def test_cross_tenant_venue_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_b"],
                metric_name="occupancy_rate",
                value=0.5,
                event_time=datetime.now(UTC),
            )

    async def test_cross_tenant_session_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="occupancy_rate",
                value=0.5,
                event_time=datetime.now(UTC),
                session_id=ids["session_b"],
            )

    async def test_cross_tenant_camera_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_metric(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                metric_name="occupancy_rate",
                value=0.5,
                event_time=datetime.now(UTC),
                camera_id=ids["camera_b"],
            )

    async def test_cross_tenant_opportunity_link_rejected(self, migrated_db) -> None:
        """A tenant-A opportunity cannot link tenant-B evidence."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        ref_a = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        ref_b = await _insert_evidence_ref(url, tenant_id=ids["tenant_b"], venue_id=ids["venue_b"])
        opportunity_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Lobby staffing",
            event_time=datetime.now(UTC),
        )
        await _link_opportunity_evidence(
            url, opportunity_id=opportunity_id, ref_id=ref_a, tenant_id=ids["tenant_a"]
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            await _link_opportunity_evidence(
                url, opportunity_id=opportunity_id, ref_id=ref_b, tenant_id=ids["tenant_a"]
            )

    # --- opportunities ---

    async def test_opportunity_round_trip(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        opportunity_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Low lobby occupancy",
            event_time=event_time,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT schema_version, venue_id, description, event_time "
                            "FROM opportunities WHERE opportunity_id = :oid"
                        ),
                        {"oid": opportunity_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.schema_version == "1.0"
        assert row.venue_id == ids["venue_a"]
        assert row.description == "Low lobby occupancy"
        assert row.event_time == event_time

    async def test_opportunity_links_round_trip(self, migrated_db) -> None:
        """An opportunity links its supporting metric samples and evidence."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        metric_id = await _insert_metric(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            metric_name="occupancy_rate",
            value=0.2,
            event_time=event_time,
        )
        ref_id = await _insert_evidence_ref(url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"])
        opportunity_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Low lobby occupancy",
            event_time=event_time,
        )
        await _link_opportunity_metric(
            url,
            opportunity_id=opportunity_id,
            event_time=event_time,
            metric_id=metric_id,
            tenant_id=ids["tenant_a"],
        )
        await _link_opportunity_evidence(
            url, opportunity_id=opportunity_id, ref_id=ref_id, tenant_id=ids["tenant_a"]
        )
        assert await _scalar(url, "SELECT count(*) FROM opportunity_metrics") == 1
        assert await _scalar(url, "SELECT count(*) FROM opportunity_evidence_refs") == 1

    async def test_empty_opportunity_description_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="description_not_empty"):
                    await conn.execute(
                        text(
                            "INSERT INTO opportunities "
                            "(opportunity_id, tenant_id, venue_id, description, event_time) "
                            "VALUES (:oid, :tid, :vid, '', :et)"
                        ),
                        {
                            "oid": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                            "et": datetime.now(UTC),
                        },
                    )
        finally:
            await engine.dispose()

    # --- orphan prevention ---

    async def test_venue_delete_cascades_analytics(self, migrated_db) -> None:
        """Deleting a venue removes its metrics, opportunities, and links."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        metric_id = await _insert_metric(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            metric_name="occupancy_rate",
            value=0.5,
            event_time=event_time,
        )
        opportunity_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Low lobby occupancy",
            event_time=event_time,
        )
        await _link_opportunity_metric(
            url,
            opportunity_id=opportunity_id,
            event_time=event_time,
            metric_id=metric_id,
            tenant_id=ids["tenant_a"],
        )

        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM venues WHERE venue_id = :vid"), {"vid": ids["venue_a"]}
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM metrics") == 0
        assert await _scalar(url, "SELECT count(*) FROM opportunities") == 0
        assert await _scalar(url, "SELECT count(*) FROM opportunity_metrics") == 0

    async def test_metric_delete_cascades_link(self, migrated_db) -> None:
        """Deleting a metric sample removes its opportunity links."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        metric_id = await _insert_metric(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            metric_name="occupancy_rate",
            value=0.5,
            event_time=event_time,
        )
        opportunity_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Low lobby occupancy",
            event_time=event_time,
        )
        await _link_opportunity_metric(
            url,
            opportunity_id=opportunity_id,
            event_time=event_time,
            metric_id=metric_id,
            tenant_id=ids["tenant_a"],
        )

        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM metrics WHERE metric_id = :mid"), {"mid": metric_id}
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM opportunity_metrics") == 0
        assert await _scalar(url, "SELECT count(*) FROM opportunities") == 1


# =============================================================================
# Task 6.9 — AI domain storage invariants (migration 011)
# =============================================================================


async def _insert_finding(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    finding_type: str,
    description: str,
    event_time: datetime,
    confidence: float | None = None,
    status: str = "proposed",
    evidence_package_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> uuid.UUID:
    """Insert a findings row (admin role bypasses RLS).

    Returns the generated finding_id.
    """
    engine = _query_engine(url)
    finding_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO findings "
                    "(finding_id, tenant_id, venue_id, finding_type, description, "
                    "event_time, confidence, status, evidence_package_id, model_name) "
                    "VALUES (:fid, :tid, :vid, :ft, :desc, :et, :conf, :st, :epid, :mn)"
                ),
                {
                    "fid": finding_id,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "ft": finding_type,
                    "desc": description,
                    "et": event_time,
                    "conf": confidence,
                    "st": status,
                    "epid": evidence_package_id,
                    "mn": model_name,
                },
            )
    finally:
        await engine.dispose()
    return finding_id


async def _insert_recommendation(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    description: str,
    priority: str = "medium",
    status: str = "pending",
    opportunity_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> uuid.UUID:
    """Insert a recommendations row (admin role bypasses RLS).

    Returns the generated recommendation_id.
    """
    engine = _query_engine(url)
    recommendation_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO recommendations "
                    "(recommendation_id, tenant_id, venue_id, description, priority, "
                    "status, opportunity_id, model_name) "
                    "VALUES (:rid, :tid, :vid, :desc, :pri, :st, :oid, :mn)"
                ),
                {
                    "rid": recommendation_id,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "desc": description,
                    "pri": priority,
                    "st": status,
                    "oid": opportunity_id,
                    "mn": model_name,
                },
            )
    finally:
        await engine.dispose()
    return recommendation_id


async def _link_recommendation_finding(
    url: str, *, recommendation_id: uuid.UUID, finding_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    """Insert a recommendation_findings row (admin role bypasses RLS)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO recommendation_findings "
                    "(recommendation_id, finding_id, tenant_id) "
                    "VALUES (:rid, :fid, :tid)"
                ),
                {"rid": recommendation_id, "fid": finding_id, "tid": tenant_id},
            )
    finally:
        await engine.dispose()


class TestAiSchema:
    """Task 6.9 invariants: finding/recommendation contracts, evidence
    linkage, tenant isolation, status transitions, versioning, migration."""

    # --- finding insertion + evidence linkage ---

    async def test_finding_insertion_round_trip(self, migrated_db) -> None:
        """A fully-populated finding persists and reads back."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        package_id = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"], description="Crowding"
        )
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Lobby above threshold for 20 minutes",
            event_time=event_time,
            confidence=0.92,
            evidence_package_id=package_id,
            model_name="verifier-v1",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT finding_type, description, confidence, event_time, "
                            "status, evidence_package_id, model_name, schema_version, "
                            "created_at FROM findings WHERE finding_id = :fid"
                        ),
                        {"fid": finding_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.finding_type == "occupancy"
        assert row.description == "Lobby above threshold for 20 minutes"
        assert abs(row.confidence - 0.92) < 1e-9
        assert row.event_time == event_time
        assert row.status == "proposed", "default status must be proposed"
        assert row.evidence_package_id == package_id
        assert row.model_name == "verifier-v1"
        assert row.schema_version == "1.0"
        assert row.created_at is not None, "created_at must be server-defaulted"

    async def test_finding_evidence_linkage(self, migrated_db) -> None:
        """A finding references its evidence package by real FK."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        package_id = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"]
        )
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
            evidence_package_id=package_id,
        )
        linked = await _scalar(
            url,
            f"SELECT count(*) FROM findings f JOIN evidence_packages p "
            f"ON p.package_id = f.evidence_package_id "
            f"AND p.tenant_id = f.tenant_id WHERE f.finding_id = '{finding_id}'::uuid",
        )
        assert linked == 1, "The finding must join to its evidence package"

    async def test_evidence_package_delete_restricted(self, migrated_db) -> None:
        """Evidence cited by a derived finding is never silently destroyed
        — deleting the package is rejected while the finding references it
        (orphan prevention)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        package_id = await _insert_evidence_package(
            url, tenant_id=ids["tenant_a"], venue_id=ids["venue_a"]
        )
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
            evidence_package_id=package_id,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="foreign key"):
                    await conn.execute(
                        text("DELETE FROM evidence_packages WHERE package_id = :pid"),
                        {"pid": package_id},
                    )
        finally:
            await engine.dispose()
        # The package survives and the finding still references it.
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM evidence_packages WHERE package_id = '{package_id}'::uuid",
            )
            == 1
        ), "cited evidence must not be deletable"
        assert (
            await _scalar(
                url, f"SELECT count(*) FROM findings WHERE finding_id = '{finding_id}'::uuid"
            )
            == 1
        )

    # --- recommendation insertion ---

    async def test_recommendation_insertion_round_trip(self, migrated_db) -> None:
        """A fully-populated recommendation persists and reads back."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a second lobby lane",
            priority="high",
            model_name="advisor-v2",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT description, priority, status, model_name, "
                            "schema_version, created_at FROM recommendations "
                            "WHERE recommendation_id = :rid"
                        ),
                        {"rid": rec_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.description == "Open a second lobby lane"
        assert row.priority == "high"
        assert row.status == "pending", "default status must be pending"
        assert row.model_name == "advisor-v2"
        assert row.schema_version == "1.0"
        assert row.created_at is not None

    async def test_recommendation_findings_link(self, migrated_db) -> None:
        """A recommendation links its supporting findings (M2M)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=1)
        finding_a = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=event_time,
        )
        finding_b = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="wait_time",
            description="Long queue",
            event_time=event_time,
        )
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a second lane",
        )
        await _link_recommendation_finding(
            url, recommendation_id=rec_id, finding_id=finding_a, tenant_id=ids["tenant_a"]
        )
        await _link_recommendation_finding(
            url, recommendation_id=rec_id, finding_id=finding_b, tenant_id=ids["tenant_a"]
        )
        count = await _scalar(
            url,
            f"SELECT count(*) FROM recommendation_findings WHERE recommendation_id = '{rec_id}'::uuid",
        )
        assert count == 2

    async def test_recommendation_opportunity_link(self, migrated_db) -> None:
        """A recommendation can cite its supporting opportunity (real FK)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=1)
        opp_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Low lobby staffing",
            event_time=event_time,
        )
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Adjust staffing",
            opportunity_id=opp_id,
        )
        assert (
            await _scalar(
                url,
                f"SELECT opportunity_id FROM recommendations WHERE recommendation_id = '{rec_id}'::uuid",
            )
            == opp_id
        )

    async def test_opportunity_delete_restricted(self, migrated_db) -> None:
        """An opportunity cited by a recommendation is not silently removed
        — deleting it is rejected while the recommendation references it."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=1)
        opp_id = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Low lobby staffing",
            event_time=event_time,
        )
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Adjust staffing",
            opportunity_id=opp_id,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="foreign key"):
                    await conn.execute(
                        text("DELETE FROM opportunities WHERE opportunity_id = :oid"),
                        {"oid": opp_id},
                    )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM recommendations WHERE recommendation_id = '{rec_id}'::uuid",
            )
            == 1
        ), "the recommendation survives the blocked delete"

    # --- status transitions ---

    async def test_status_defaults_and_transitions(self, migrated_db) -> None:
        """Workflow states transition; the updated_at timestamp follows."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE findings SET status = 'accepted', updated_at = now() "
                        "WHERE finding_id = :fid"
                    ),
                    {"fid": finding_id},
                )
        finally:
            await engine.dispose()
        row = await _scalar(
            url,
            f"SELECT status FROM findings WHERE finding_id = '{finding_id}'::uuid",
        )
        assert row == "accepted", "status must transition proposed -> accepted"

    async def test_invalid_status_rejected(self, migrated_db) -> None:
        """A status outside the workflow enum is rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO findings "
                            "(finding_id, tenant_id, venue_id, finding_type, "
                            "description, event_time, status) "
                            "VALUES (:fid, :tid, :vid, 'occupancy', 'Crowded', :et, 'bogus')"
                        ),
                        {
                            "fid": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                            "et": datetime.now(UTC),
                        },
                    )
        finally:
            await engine.dispose()

    async def test_recommendation_status_transitions(self, migrated_db) -> None:
        """Recommendation workflow states transition (pending -> implemented)
        and an invalid status is rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        assert (
            await _scalar(
                url,
                f"SELECT status FROM recommendations WHERE recommendation_id = '{rec_id}'::uuid",
            )
            == "pending"
        ), "default status must be pending"
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE recommendations SET status = 'implemented', "
                        "updated_at = now() WHERE recommendation_id = :rid"
                    ),
                    {"rid": rec_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM recommendations WHERE recommendation_id = '{rec_id}'::uuid",
            )
            == "implemented"
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO recommendations "
                            "(recommendation_id, tenant_id, venue_id, description, status) "
                            "VALUES (:rid, :tid, :vid, 'Open a lane', 'bogus')"
                        ),
                        {
                            "rid": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_updated_at_not_before_created(self, migrated_db) -> None:
        """A transition cannot predate the finding's creation."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="updated_not_before_created"):
                    await conn.execute(
                        text(
                            "UPDATE findings SET status = 'accepted', updated_at = :past "
                            "WHERE finding_id = :fid"
                        ),
                        {
                            "past": datetime.now(UTC) - timedelta(hours=1),
                            "fid": finding_id,
                        },
                    )
        finally:
            await engine.dispose()

    # --- versioning ---

    async def test_schema_version_server_default(self, migrated_db) -> None:
        """schema_version defaults to the contract version (1.0)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        assert (
            await _scalar(
                url,
                f"SELECT schema_version FROM findings WHERE finding_id = '{finding_id}'::uuid",
            )
            == "1.0"
        )

    # --- invalid schema ---

    async def test_empty_description_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="description_not_empty"):
            await _insert_finding(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                finding_type="occupancy",
                description="   ",
                event_time=datetime.now(UTC),
            )

    async def test_empty_finding_type_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="finding_type_not_empty"):
            await _insert_finding(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                finding_type="  ",
                description="Crowded lobby",
                event_time=datetime.now(UTC),
            )

    async def test_confidence_range_rejected(self, migrated_db) -> None:
        """Confidence outside [0, 1] is rejected by CHECK."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="confidence_range"):
            await _insert_finding(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                finding_type="occupancy",
                description="Crowded lobby",
                event_time=datetime.now(UTC),
                confidence=1.5,
            )

    # --- tenancy ---

    async def test_cross_tenant_venue_rejected(self, migrated_db) -> None:
        """A finding cannot reference another tenant's venue."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_finding(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_b"],
                finding_type="occupancy",
                description="Crowded lobby",
                event_time=datetime.now(UTC),
            )

    async def test_cross_tenant_evidence_package_rejected(self, migrated_db) -> None:
        """A finding cannot reference another tenant's evidence package."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        package_b = await _insert_evidence_package(
            url, tenant_id=ids["tenant_b"], venue_id=ids["venue_b"]
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_finding(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                finding_type="occupancy",
                description="Crowded lobby",
                event_time=datetime.now(UTC),
                evidence_package_id=package_b,
            )

    async def test_cross_tenant_recommendation_finding_link_rejected(self, migrated_db) -> None:
        """A recommendation cannot cite another tenant's finding."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_b = await _insert_finding(
            url,
            tenant_id=ids["tenant_b"],
            venue_id=ids["venue_b"],
            finding_type="occupancy",
            description="Tenant B lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        rec_a = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            await _link_recommendation_finding(
                url, recommendation_id=rec_a, finding_id=finding_b, tenant_id=ids["tenant_a"]
            )

    async def test_cross_tenant_opportunity_rejected(self, migrated_db) -> None:
        """A recommendation cannot reference another tenant's opportunity."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        opp_b = await _insert_opportunity(
            url,
            tenant_id=ids["tenant_b"],
            venue_id=ids["venue_b"],
            description="Tenant B opportunity",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_recommendation(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                description="Open a lane",
                opportunity_id=opp_b,
            )

    # --- orphan prevention ---

    async def test_finding_delete_cascades_link(self, migrated_db) -> None:
        """Deleting a finding removes its recommendation links (no orphans)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=1)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=event_time,
        )
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        await _link_recommendation_finding(
            url, recommendation_id=rec_id, finding_id=finding_id, tenant_id=ids["tenant_a"]
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM findings WHERE finding_id = :fid"), {"fid": finding_id}
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM recommendation_findings") == 0
        assert await _scalar(url, "SELECT count(*) FROM recommendations") == 1

    async def test_recommendation_delete_cascades_link(self, migrated_db) -> None:
        """Deleting a recommendation removes its finding links (no orphans)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=1)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=event_time,
        )
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        await _link_recommendation_finding(
            url, recommendation_id=rec_id, finding_id=finding_id, tenant_id=ids["tenant_a"]
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM recommendations WHERE recommendation_id = :rid"),
                    {"rid": rec_id},
                )
        finally:
            await engine.dispose()
        assert await _scalar(url, "SELECT count(*) FROM recommendation_findings") == 0
        assert await _scalar(url, "SELECT count(*) FROM findings") == 1


# =============================================================================
# Task 6.10 — alert & approval storage invariants (migration 012)
# =============================================================================


async def _insert_alert(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    alert_type: str,
    title: str,
    description: str,
    event_time: datetime,
    severity: str = "info",
    finding_id: uuid.UUID | None = None,
    recommendation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert an alerts row (admin role bypasses RLS).

    Returns the generated alert_id.
    """
    engine = _query_engine(url)
    alert_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO alerts "
                    "(alert_id, tenant_id, venue_id, alert_type, severity, title, "
                    "description, event_time, finding_id, recommendation_id) "
                    "VALUES (:aid, :tid, :vid, :at, :sev, :title, :desc, :et, "
                    ":fid, :rid)"
                ),
                {
                    "aid": alert_id,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "at": alert_type,
                    "sev": severity,
                    "title": title,
                    "desc": description,
                    "et": event_time,
                    "fid": finding_id,
                    "rid": recommendation_id,
                },
            )
    finally:
        await engine.dispose()
    return alert_id


async def _insert_user(url: str, user_id: uuid.UUID, name: str, email: str) -> None:
    """Insert a users row (users is a global catalog — no tenant)."""
    engine = _query_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users (user_id, display_name, email) VALUES (:id, :n, :e)"),
                {"id": user_id, "n": name, "e": email},
            )
    finally:
        await engine.dispose()


async def _insert_approval_request(
    url: str,
    *,
    tenant_id: uuid.UUID,
    recommendation_id: uuid.UUID,
    requested_by: uuid.UUID,
    requested_at: datetime,
) -> uuid.UUID:
    """Insert an approval_requests row (admin role bypasses RLS).

    Returns the generated request_id.
    """
    engine = _query_engine(url)
    request_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(request_id, tenant_id, recommendation_id, requested_by, "
                    "requested_at) VALUES (:req, :tid, :rid, :actor, :at)"
                ),
                {
                    "req": request_id,
                    "tid": tenant_id,
                    "rid": recommendation_id,
                    "actor": requested_by,
                    "at": requested_at,
                },
            )
    finally:
        await engine.dispose()
    return request_id


class TestAlertApprovalSchema:
    """Task 6.10 invariants: explicit state transitions (legal + illegal),
    alert ownership, approval request/actor/subject/state/timestamps,
    duplicate approval handling, tenant isolation, migration."""

    # --- alert insertion + ownership ---

    async def test_alert_insertion_round_trip(self, migrated_db) -> None:
        """A fully-populated alert persists and reads back."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=5)
        alert_id = await _insert_alert(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            alert_type="occupancy",
            title="Lobby above threshold",
            description="Lobby occupancy exceeded the threshold",
            event_time=event_time,
            severity="high",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT alert_type, severity, title, description, "
                            "event_time, status, schema_version, created_at "
                            "FROM alerts WHERE alert_id = :aid"
                        ),
                        {"aid": alert_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.alert_type == "occupancy"
        assert row.severity == "high"
        assert row.title == "Lobby above threshold"
        assert row.description == "Lobby occupancy exceeded the threshold"
        assert row.event_time == event_time
        assert row.status == "raised", "default status must be raised"
        assert row.schema_version == "1.0"
        assert row.created_at is not None

    async def test_alert_links_finding_source(self, migrated_db) -> None:
        """An alert can reference its source finding via a real composite FK."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        alert_id = await _insert_alert(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            alert_type="occupancy",
            title="Lobby busy",
            description="Lobby busy",
            event_time=datetime.now(UTC),
            finding_id=finding_id,
        )
        assert (
            await _scalar(url, f"SELECT finding_id FROM alerts WHERE alert_id = '{alert_id}'::uuid")
            == finding_id
        )

    async def test_alert_both_sources_rejected(self, migrated_db) -> None:
        """An alert cannot reference both a finding and a recommendation."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_id = await _insert_finding(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            finding_type="occupancy",
            description="Crowded lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        with pytest.raises(IntegrityError, match="source_single"):
            await _insert_alert(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                alert_type="occupancy",
                title="Lobby busy",
                description="Lobby busy",
                event_time=datetime.now(UTC),
                finding_id=finding_id,
                recommendation_id=rec_id,
            )

    # --- alert state transitions (explicit lifecycle) ---

    async def test_alert_legal_transitions(self, migrated_db) -> None:
        """raised -> acknowledged -> resolved is a legal lifecycle path."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        alert_id = await _insert_alert(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            alert_type="occupancy",
            title="Lobby busy",
            description="Lobby busy",
            event_time=datetime.now(UTC),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE alerts SET status = 'acknowledged' WHERE alert_id = :aid"),
                    {"aid": alert_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(url, f"SELECT status FROM alerts WHERE alert_id = '{alert_id}'::uuid")
            == "acknowledged"
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE alerts SET status = 'resolved' WHERE alert_id = :aid"),
                    {"aid": alert_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(url, f"SELECT status FROM alerts WHERE alert_id = '{alert_id}'::uuid")
            == "resolved"
        )

    async def test_alert_illegal_transition_rejected(self, migrated_db) -> None:
        """A resolved alert cannot be re-opened — the trigger rejects it."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        alert_id = await _insert_alert(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            alert_type="occupancy",
            title="Lobby busy",
            description="Lobby busy",
            event_time=datetime.now(UTC),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE alerts SET status = 'resolved' WHERE alert_id = :aid"),
                    {"aid": alert_id},
                )
                # Savepoint: the trigger failure aborts only the savepoint,
                # never the outer transaction (asyncpg aborted-tx semantics).
                with pytest.raises(Exception, match="illegal alert status transition"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text("UPDATE alerts SET status = 'raised' WHERE alert_id = :aid"),
                            {"aid": alert_id},
                        )
        finally:
            await engine.dispose()
        # The rejected transition left the alert resolved.
        assert (
            await _scalar(url, f"SELECT status FROM alerts WHERE alert_id = '{alert_id}'::uuid")
            == "resolved"
        )

    async def test_alert_skip_acknowledge_allowed(self, migrated_db) -> None:
        """raised -> expired is legal (auto-expiry without acknowledgement)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        alert_id = await _insert_alert(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            alert_type="occupancy",
            title="Lobby busy",
            description="Lobby busy",
            event_time=datetime.now(UTC),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE alerts SET status = 'expired' WHERE alert_id = :aid"),
                    {"aid": alert_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(url, f"SELECT status FROM alerts WHERE alert_id = '{alert_id}'::uuid")
            == "expired"
        )

    async def test_alert_acknowledged_expired_allowed(self, migrated_db) -> None:
        """acknowledged -> expired is legal (expiry after acknowledgement)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        alert_id = await _insert_alert(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            alert_type="occupancy",
            title="Lobby busy",
            description="Lobby busy",
            event_time=datetime.now(UTC),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE alerts SET status = 'acknowledged' WHERE alert_id = :aid"),
                    {"aid": alert_id},
                )
                await conn.execute(
                    text("UPDATE alerts SET status = 'expired' WHERE alert_id = :aid"),
                    {"aid": alert_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(url, f"SELECT status FROM alerts WHERE alert_id = '{alert_id}'::uuid")
            == "expired"
        )

    # --- approval requests: request/actor/subject/state/timestamps ---

    async def test_approval_request_round_trip(self, migrated_db) -> None:
        """A request persists request id, actor, subject, state, timestamps."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor = uuid.uuid4()
        await _insert_user(url, actor, "Approver", f"approver_{actor.hex[:8]}@example.com")
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        requested_at = datetime.now(UTC) - timedelta(minutes=5)
        request_id = await _insert_approval_request(
            url,
            tenant_id=ids["tenant_a"],
            recommendation_id=rec_id,
            requested_by=actor,
            requested_at=requested_at,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT recommendation_id, requested_by, status, "
                            "requested_at, resolved_at, reason, schema_version "
                            "FROM approval_requests WHERE request_id = :req"
                        ),
                        {"req": request_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.recommendation_id == rec_id
        assert row.requested_by == actor
        assert row.status == "pending", "default status must be pending"
        assert row.requested_at == requested_at
        assert row.resolved_at is None
        assert row.reason is None
        assert row.schema_version == "1.0"

    async def test_approval_legal_transition_sets_resolved_at(self, migrated_db) -> None:
        """pending -> approved resolves the request and stamps resolved_at."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor = uuid.uuid4()
        await _insert_user(url, actor, "Approver", f"approver_{actor.hex[:8]}@example.com")
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        request_id = await _insert_approval_request(
            url,
            tenant_id=ids["tenant_a"],
            recommendation_id=rec_id,
            requested_by=actor,
            requested_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE approval_requests SET status = 'approved' WHERE request_id = :req"
                    ),
                    {"req": request_id},
                )
        finally:
            await engine.dispose()
        row = await _scalar(
            url,
            f"SELECT status FROM approval_requests WHERE request_id = '{request_id}'::uuid",
        )
        assert row == "approved"
        resolved_at = await _scalar(
            url,
            f"SELECT resolved_at FROM approval_requests WHERE request_id = '{request_id}'::uuid",
        )
        assert resolved_at is not None, "resolved_at must be stamped on approval"

    async def test_approval_illegal_transition_rejected(self, migrated_db) -> None:
        """An approved request cannot be re-opened or re-decided (pending)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor = uuid.uuid4()
        await _insert_user(url, actor, "Approver", f"approver_{actor.hex[:8]}@example.com")
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        request_id = await _insert_approval_request(
            url,
            tenant_id=ids["tenant_a"],
            recommendation_id=rec_id,
            requested_by=actor,
            requested_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE approval_requests SET status = 'approved' WHERE request_id = :req"
                    ),
                    {"req": request_id},
                )
                # Savepoint: the trigger failure aborts only the savepoint.
                with pytest.raises(Exception, match="illegal approval status transition"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE approval_requests SET status = 'rejected' "
                                "WHERE request_id = :req"
                            ),
                            {"req": request_id},
                        )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url, f"SELECT status FROM approval_requests WHERE request_id = '{request_id}'::uuid"
            )
            == "approved"
        )

    # --- duplicate approval handling ---

    async def test_approval_cancelled_is_terminal(self, migrated_db) -> None:
        """pending -> cancelled is legal; a cancelled request is terminal
        (cancelled -> approved is rejected)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor = uuid.uuid4()
        await _insert_user(url, actor, "Approver", f"approver_{actor.hex[:8]}@example.com")
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        request_id = await _insert_approval_request(
            url,
            tenant_id=ids["tenant_a"],
            recommendation_id=rec_id,
            requested_by=actor,
            requested_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE approval_requests SET status = 'cancelled' WHERE request_id = :req"
                    ),
                    {"req": request_id},
                )
                with pytest.raises(Exception, match="illegal approval status transition"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE approval_requests SET status = 'approved' "
                                "WHERE request_id = :req"
                            ),
                            {"req": request_id},
                        )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url, f"SELECT status FROM approval_requests WHERE request_id = '{request_id}'::uuid"
            )
            == "cancelled"
        )

    async def test_duplicate_terminal_decision_rejected(self, migrated_db) -> None:
        """At most one terminal decision per request — the partial unique
        index rejects a second decision row."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor = uuid.uuid4()
        await _insert_user(url, actor, "Approver", f"approver_{actor.hex[:8]}@example.com")
        rec_id = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            description="Open a lane",
        )
        request_id = await _insert_approval_request(
            url,
            tenant_id=ids["tenant_a"],
            recommendation_id=rec_id,
            requested_by=actor,
            requested_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO approval_decisions "
                        "(decision_id, request_id, tenant_id, actor_id, decision) "
                        "VALUES (:did, :req, :tid, :actor, 'approved')"
                    ),
                    {
                        "did": uuid.uuid4(),
                        "req": request_id,
                        "tid": ids["tenant_a"],
                        "actor": actor,
                    },
                )
                # Savepoint: the unique-index failure aborts only the
                # savepoint, never the first decision insert.
                with pytest.raises(IntegrityError, match="uq_approval_decisions_terminal"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO approval_decisions "
                                "(decision_id, request_id, tenant_id, actor_id, decision) "
                                "VALUES (:did, :req, :tid, :actor, 'rejected')"
                            ),
                            {
                                "did": uuid.uuid4(),
                                "req": request_id,
                                "tid": ids["tenant_a"],
                                "actor": actor,
                            },
                        )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM approval_decisions WHERE request_id = '{request_id}'::uuid",
            )
            == 1
        )

    # --- tenancy ---

    async def test_alert_cross_tenant_venue_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_alert(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_b"],
                alert_type="occupancy",
                title="Lobby busy",
                description="Lobby busy",
                event_time=datetime.now(UTC),
            )

    async def test_alert_cross_tenant_finding_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        finding_b = await _insert_finding(
            url,
            tenant_id=ids["tenant_b"],
            venue_id=ids["venue_b"],
            finding_type="occupancy",
            description="Tenant B lobby",
            event_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_alert(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                alert_type="occupancy",
                title="Lobby busy",
                description="Lobby busy",
                event_time=datetime.now(UTC),
                finding_id=finding_b,
            )

    async def test_approval_cross_tenant_subject_rejected(self, migrated_db) -> None:
        """A request cannot reference another tenant's recommendation."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor = uuid.uuid4()
        await _insert_user(url, actor, "Approver", f"approver_{actor.hex[:8]}@example.com")
        rec_b = await _insert_recommendation(
            url,
            tenant_id=ids["tenant_b"],
            venue_id=ids["venue_b"],
            description="Tenant B lane",
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_approval_request(
                url,
                tenant_id=ids["tenant_a"],
                recommendation_id=rec_b,
                requested_by=actor,
                requested_at=datetime.now(UTC),
            )

    # --- invalid schema ---

    async def test_alert_empty_title_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="title_not_empty"):
            await _insert_alert(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                alert_type="occupancy",
                title="  ",
                description="Lobby busy",
                event_time=datetime.now(UTC),
            )


# =============================================================================
# Task 6.11 — integration storage invariants (migration 013)
# =============================================================================


async def _insert_integration(
    url: str,
    *,
    tenant_id: uuid.UUID,
    venue_id: uuid.UUID,
    provider_type: str,
    provider_name: str,
    status: str = "pending",
    config_metadata: dict | list | None = None,
    secret_ref: str | None = None,
    external_identifier: str | None = None,
) -> uuid.UUID:
    """Insert an integrations row (admin role bypasses RLS).

    Returns the generated integration_id.
    """
    engine = _query_engine(url)
    integration_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO integrations "
                    "(integration_id, tenant_id, venue_id, provider_type, "
                    "provider_name, status, config_metadata, secret_ref, "
                    "external_identifier) "
                    "VALUES (:iid, :tid, :vid, :pt, :pn, :st, "
                    "CAST(:cm AS jsonb), :sr, :eid)"
                ),
                {
                    "iid": integration_id,
                    "tid": tenant_id,
                    "vid": venue_id,
                    "pt": provider_type,
                    "pn": provider_name,
                    "st": status,
                    "cm": json.dumps(config_metadata) if config_metadata else None,
                    "sr": secret_ref,
                    "eid": external_identifier,
                },
            )
    finally:
        await engine.dispose()
    return integration_id


class TestIntegrationSchema:
    """Task 6.11 invariants: identity/ownership, provider/type, status
    lifecycle (legal + illegal), secrets posture, duplicate provider
    constraint, invalid state, tenant isolation, migration."""

    # --- insertion + identity/ownership ---

    async def test_integration_insertion_round_trip(self, migrated_db) -> None:
        """A fully-populated integration persists and reads back."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            config_metadata={"endpoint": "https://pos.example.com"},
            secret_ref="LIGHTSPEED_API_KEY",
            external_identifier="store-42",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT provider_type, provider_name, status, "
                            "config_metadata, secret_ref, external_identifier, "
                            "schema_version, created_at FROM integrations "
                            "WHERE integration_id = :iid"
                        ),
                        {"iid": integration_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.provider_type == "pos"
        assert row.provider_name == "lightspeed"
        assert row.status == "pending", "default status must be pending"
        assert row.config_metadata == {"endpoint": "https://pos.example.com"}
        assert row.secret_ref == "LIGHTSPEED_API_KEY"
        assert row.external_identifier == "store-42"
        assert row.schema_version == "1.0"
        assert row.created_at is not None

    async def test_integration_tenant_venue_ownership(self, migrated_db) -> None:
        """Tenant/venue ownership is direct — cross-tenant venue rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="foreign key"):
            await _insert_integration(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_b"],
                provider_type="pos",
                provider_name="lightspeed",
            )

    # --- secrets posture ---

    async def test_no_secret_value_stored(self, migrated_db) -> None:
        """The schema has no column for a credential value — only a ref."""
        url = migrated_db["url"]
        cols = await _scalar(
            url,
            "SELECT string_agg(column_name, ',') FROM information_schema.columns "
            "WHERE table_name = 'integrations'",
        )
        assert cols is not None
        for secret_column in ("api_key", "password", "token", "credential"):
            assert secret_column not in str(cols).split(",")
        assert "secret_ref" in str(cols).split(",")

    async def test_secret_terms_rejected_in_config_metadata(self, migrated_db) -> None:
        """Config metadata rejects secret-like keys — first-segment semantics
        (audit contract): secret_key is blocked, api_key is allowed."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="metadata_no_secrets"):
            await _insert_integration(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                provider_type="pos",
                provider_name="lightspeed",
                config_metadata={"secret_key": "super-secret"},
            )
        # 'api_key' first segment is 'api' — allowed, matching the audit
        # contract's validator semantics.
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            config_metadata={"api_key_ref": "LIGHTSPEED_API_KEY"},
        )
        assert integration_id is not None

    async def test_secret_ref_reference_allowed(self, migrated_db) -> None:
        """A secret REFERENCE (a name/key, not the value) is stored fine."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pms",
            provider_name="oracle",
            secret_ref="PMS_CREDENTIAL_REF",
        )
        assert (
            await _scalar(
                url,
                f"SELECT secret_ref FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            == "PMS_CREDENTIAL_REF"
        )

    # --- status lifecycle (explicit transitions) ---

    async def test_status_legal_transitions(self, migrated_db) -> None:
        """pending -> active -> disabled -> active is a legal lifecycle path."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE integrations SET status = 'active' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            == "active"
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE integrations SET status = 'disabled' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            == "disabled"
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE integrations SET status = 'active' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            == "active"
        )

    async def test_status_illegal_transition_rejected(self, migrated_db) -> None:
        """An active integration cannot jump straight to pending — the
        trigger rejects it."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE integrations SET status = 'active' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
                with pytest.raises(Exception, match="illegal integration status transition"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE integrations SET status = 'pending' "
                                "WHERE integration_id = :iid"
                            ),
                            {"iid": integration_id},
                        )
        finally:
            await engine.dispose()
        # The rejected transition left the integration active.
        assert (
            await _scalar(
                url,
                f"SELECT status FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            == "active"
        )

    async def test_error_to_active_allowed(self, migrated_db) -> None:
        """error -> active is legal (recovery)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE integrations SET status = 'error' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
                await conn.execute(
                    text("UPDATE integrations SET status = 'active' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            == "active"
        )

    # --- duplicate provider constraints ---

    async def test_duplicate_active_provider_rejected(self, migrated_db) -> None:
        """A tenant cannot have two ACTIVE integrations of the same provider."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        first = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            status="active",
        )
        assert first is not None
        with pytest.raises(IntegrityError, match="uq_integrations_active_provider"):
            await _insert_integration(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                provider_type="pos",
                provider_name="lightspeed",
                status="active",
            )

    async def test_disabled_duplicate_allowed(self, migrated_db) -> None:
        """A non-active integration of the same provider does not collide
        with the active one (partial unique index on active only)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            status="active",
        )
        # A second, non-active row is fine (e.g. a pending re-config).
        second = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            status="pending",
        )
        assert second is not None
        # Different provider names are always allowed.
        other = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="clover",
            status="active",
        )
        assert other is not None

    async def test_same_provider_different_tenant_allowed(self, migrated_db) -> None:
        """The duplicate constraint is tenant-scoped."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            status="active",
        )
        other = await _insert_integration(
            url,
            tenant_id=ids["tenant_b"],
            venue_id=ids["venue_b"],
            provider_type="pos",
            provider_name="lightspeed",
            status="active",
        )
        assert other is not None

    # --- invalid integration state ---

    async def test_invalid_status_rejected(self, migrated_db) -> None:
        """A status outside the lifecycle enum is rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO integrations "
                            "(integration_id, tenant_id, venue_id, provider_type, "
                            "provider_name, status) "
                            "VALUES (:iid, :tid, :vid, 'pos', 'lightspeed', 'bogus')"
                        ),
                        {
                            "iid": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_invalid_provider_type_rejected(self, migrated_db) -> None:
        """A provider outside the adapter-family enum is rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(DBAPIError, match="invalid input value for enum"):
                    await conn.execute(
                        text(
                            "INSERT INTO integrations "
                            "(integration_id, tenant_id, venue_id, provider_type, "
                            "provider_name) VALUES (:iid, :tid, :vid, 'bogus', 'x')"
                        ),
                        {
                            "iid": uuid.uuid4(),
                            "tid": ids["tenant_a"],
                            "vid": ids["venue_a"],
                        },
                    )
        finally:
            await engine.dispose()

    async def test_empty_provider_name_rejected(self, migrated_db) -> None:
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="provider_name_not_empty"):
            await _insert_integration(
                url,
                tenant_id=ids["tenant_a"],
                venue_id=ids["venue_a"],
                provider_type="pos",
                provider_name="   ",
            )

    # --- config metadata boundary (non-object + nested) ---

    async def test_non_object_config_metadata_allowed(self, migrated_db) -> None:
        """Non-object metadata (array/scalar) is accepted with a defined
        result — jsonb_object_keys only applies to objects, so the helper
        returns false rather than raising an opaque error."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            config_metadata=["endpoint-a", "endpoint-b"],
        )
        assert integration_id is not None
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO integrations "
                        "(integration_id, tenant_id, venue_id, provider_type, "
                        "provider_name, config_metadata) "
                        "VALUES (:iid, :tid, :vid, 'pos', 'x', CAST(:meta AS jsonb))"
                    ),
                    {
                        "iid": uuid.uuid4(),
                        "tid": ids["tenant_a"],
                        "vid": ids["venue_a"],
                        "meta": json.dumps([1, 2, 3]),
                    },
                )
        finally:
            await engine.dispose()

    async def test_nested_secret_key_allowed_by_check(self, migrated_db) -> None:
        """The metadata CHECK is a TOP-LEVEL-KEY tripwire only (documented in
        the migration header): nested objects are not recursed, matching the
        audit contract's first-segment semantics. This test locks in that
        boundary — the application layer is the real guard."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            config_metadata={"connection": {"password": "nested-value"}},
        )
        assert integration_id is not None

    # --- duplicate provider: activation via UPDATE ---

    async def test_duplicate_activation_via_update_rejected(self, migrated_db) -> None:
        """Activating a second same-provider row fails with a unique
        violation — the transition alone is legal, only the partial unique
        index blocks it (the app layer must translate this error)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            status="active",
        )
        second = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
            status="pending",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # Transition pending -> active is legal for the trigger, but
                # the partial unique index rejects the second ACTIVE row.
                with pytest.raises(IntegrityError, match="uq_integrations_active_provider"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE integrations SET status = 'active' "
                                "WHERE integration_id = :iid"
                            ),
                            {"iid": second},
                        )
        finally:
            await engine.dispose()
        # The failed activation left the row pending.
        assert (
            await _scalar(
                url,
                f"SELECT status FROM integrations WHERE integration_id = '{second}'::uuid",
            )
            == "pending"
        )

    # --- timestamps ---

    async def test_updated_at_set_on_status_transition(self, migrated_db) -> None:
        """The transition trigger stamps updated_at when status changes."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        integration_id = await _insert_integration(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            provider_type="pos",
            provider_name="lightspeed",
        )
        assert (
            await _scalar(
                url,
                f"SELECT updated_at FROM integrations WHERE integration_id = '{integration_id}'::uuid",
            )
            is None
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE integrations SET status = 'active' WHERE integration_id = :iid"),
                    {"iid": integration_id},
                )
        finally:
            await engine.dispose()
        updated_at = await _scalar(
            url,
            f"SELECT updated_at FROM integrations WHERE integration_id = '{integration_id}'::uuid",
        )
        assert updated_at is not None, "updated_at must be stamped on transition"
        assert updated_at.tzinfo is not None


# =============================================================================
# Task 6.12 — audit, outbox & inbox storage invariants (migration 014)
# =============================================================================


async def _insert_audit(
    url: str,
    *,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    action_category: str,
    membership_id: uuid.UUID | None = None,
    venue_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
    metadata: dict | None = None,
) -> uuid.UUID:
    """Insert an audit_events row (admin role). Returns audit_id."""
    engine = _query_engine(url)
    audit_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit_events "
                    "(audit_id, actor_id, tenant_id, membership_id, venue_id, "
                    "action, action_category, correlation_id, metadata) "
                    "VALUES (:aid, :actor, :tid, :mid, :vid, :action, :cat, "
                    ":corr, CAST(:meta AS jsonb))"
                ),
                {
                    "aid": audit_id,
                    "actor": actor_id,
                    "tid": tenant_id,
                    "mid": membership_id,
                    "vid": venue_id,
                    "action": action,
                    "cat": action_category,
                    "corr": correlation_id,
                    "meta": json.dumps(metadata) if metadata else None,
                },
            )
    finally:
        await engine.dispose()
    return audit_id


async def _insert_outbox(
    url: str,
    *,
    event_id: uuid.UUID,
    tenant_id: uuid.UUID,
    event_type: str,
    payload: dict | None = None,
    status: str = "pending",
) -> uuid.UUID:
    """Insert an outbox_events row (admin role). Returns outbox_id."""
    engine = _query_engine(url)
    outbox_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(outbox_id, event_id, tenant_id, event_type, payload, status) "
                    "VALUES (:oid, :eid, :tid, :et, CAST(:payload AS jsonb), :st)"
                ),
                {
                    "oid": outbox_id,
                    "eid": event_id,
                    "tid": tenant_id,
                    "et": event_type,
                    "payload": json.dumps(payload or {"class_name": "person"}),
                    "st": status,
                },
            )
    finally:
        await engine.dispose()
    return outbox_id


async def _insert_inbox(
    url: str,
    *,
    tenant_id: uuid.UUID,
    source: str,
    source_message_id: str,
    payload: dict | None = None,
) -> uuid.UUID:
    """Insert an inbox_messages row (admin role). Returns inbox_id."""
    engine = _query_engine(url)
    inbox_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO inbox_messages "
                    "(inbox_id, tenant_id, source, source_message_id, payload) "
                    "VALUES (:iid, :tid, :src, :smid, CAST(:payload AS jsonb))"
                ),
                {
                    "iid": inbox_id,
                    "tid": tenant_id,
                    "src": source,
                    "smid": source_message_id,
                    "payload": json.dumps(payload or {"event": "checkout"}),
                },
            )
    finally:
        await engine.dispose()
    return inbox_id


class TestAuditOutboxInboxSchema:
    """Task 6.12 invariants: trusted audit identity, append-only audit,
    atomic outbox transaction, outbox uniqueness, inbox deduplication,
    idempotent processing, tenant identity, migration."""

    # --- AUDIT: trusted identity + append-only ---

    async def test_audit_insertion_round_trip(self, migrated_db) -> None:
        """A fully-populated audit event persists with its trusted identity."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        actor_id = uuid.uuid4()
        audit_id = await _insert_audit(
            url,
            actor_id=actor_id,
            tenant_id=ids["tenant_a"],
            membership_id=uuid.uuid4(),
            venue_id=ids["venue_a"],
            action="tenant.update",
            action_category="tenant",
            correlation_id="req-123",
            metadata={"reason": "billing change"},
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT actor_id, tenant_id, membership_id, venue_id, "
                            "action, action_category, correlation_id, metadata, "
                            "timestamp, schema_version FROM audit_events "
                            "WHERE audit_id = :aid"
                        ),
                        {"aid": audit_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.actor_id == actor_id
        assert row.tenant_id == ids["tenant_a"]
        assert row.venue_id == ids["venue_a"]
        assert row.action == "tenant.update"
        assert row.action_category == "tenant"
        assert row.correlation_id == "req-123"
        assert row.metadata == {"reason": "billing change"}
        assert row.schema_version == "1.0"
        assert row.timestamp is not None
        assert row.timestamp.tzinfo is not None, "timestamp must be timezone-aware"

    async def test_audit_actor_identity_is_required(self, migrated_db) -> None:
        """Every audit record identifies its trusted actor — actor_id is
        NOT NULL (never client-supplied, always from ActorContext)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="actor_id"):
                    await conn.execute(
                        text(
                            "INSERT INTO audit_events "
                            "(audit_id, actor_id, tenant_id, action, "
                            "action_category) VALUES (:aid, NULL, :tid, 'x', 'user')"
                        ),
                        {"aid": uuid.uuid4(), "tid": ids["tenant_a"]},
                    )
        finally:
            await engine.dispose()

    async def test_audit_metadata_secret_terms_rejected(self, migrated_db) -> None:
        """Audit metadata never stores secrets — first-segment semantics:
        secret_key blocked, api_key allowed."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        with pytest.raises(IntegrityError, match="metadata_no_secrets"):
            await _insert_audit(
                url,
                actor_id=uuid.uuid4(),
                tenant_id=ids["tenant_a"],
                action="user.login",
                action_category="authentication",
                metadata={"secret_key": "super-secret"},
            )
        # api_key's first segment is 'api' — allowed (audit contract).
        audit_id = await _insert_audit(
            url,
            actor_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            action="user.login",
            action_category="authentication",
            metadata={"api_key_ref": "some-ref"},
        )
        assert audit_id is not None

    async def test_audit_no_secret_columns(self, migrated_db) -> None:
        """No credential/token column exists in the audit schema."""
        url = migrated_db["url"]
        cols = await _scalar(
            url,
            "SELECT string_agg(column_name, ',') FROM information_schema.columns "
            "WHERE table_name = 'audit_events'",
        )
        assert cols is not None
        for secret_column in ("password", "token", "api_key", "credential", "secret"):
            assert secret_column not in str(cols).split(",")

    # --- OUTBOX: atomic transaction + uniqueness ---

    async def test_outbox_commits_atomically_with_domain_state(self, migrated_db) -> None:
        """Domain state + outbox row COMMIT atomically (transactional
        outbox): the business change and its outbox row appear together,
        nothing published before commit."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        domain_tenant_id = uuid.uuid4()
        event_id = uuid.uuid4()
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # The business change (a new tenant) and its outbox row in
                # ONE transaction — both or neither.
                await conn.execute(
                    text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                    {"id": domain_tenant_id, "name": "Atomicity Tenant"},
                )
                await conn.execute(
                    text(
                        "INSERT INTO outbox_events "
                        "(outbox_id, event_id, tenant_id, event_type, payload) "
                        "VALUES (:oid, :eid, :tid, 'tenant.created', "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "eid": event_id,
                        "tid": ids["tenant_a"],
                        "payload": json.dumps({"tenant_id": str(domain_tenant_id)}),
                    },
                )
        finally:
            await engine.dispose()
        # Both the domain row and the outbox row are durable together.
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM tenants WHERE tenant_id = '{domain_tenant_id}'::uuid",
            )
            == 1
        )
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM outbox_events WHERE event_id = '{event_id}'::uuid",
            )
            == 1
        )

    async def test_outbox_rolls_back_with_domain_state(self, migrated_db) -> None:
        """If the transaction rolls back, the outbox row is discarded with
        the domain change — an uncommitted event is never published."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        domain_tenant_id = uuid.uuid4()
        event_id = uuid.uuid4()
        engine = _query_engine(url)
        try:
            with pytest.raises(RuntimeError, match="boom"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("INSERT INTO tenants (tenant_id, name) VALUES (:id, :name)"),
                        {"id": domain_tenant_id, "name": "Rollback Tenant"},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO outbox_events "
                            "(outbox_id, event_id, tenant_id, event_type, payload) "
                            "VALUES (:oid, :eid, :tid, 'tenant.created', "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "oid": uuid.uuid4(),
                            "eid": event_id,
                            "tid": ids["tenant_a"],
                            "payload": json.dumps({"tenant_id": str(domain_tenant_id)}),
                        },
                    )
                    raise RuntimeError("boom")
        finally:
            await engine.dispose()
        # engine.begin() rolled back — NEITHER the domain row NOR the outbox
        # row exists (atomic pairing).
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM tenants WHERE tenant_id = '{domain_tenant_id}'::uuid",
            )
            == 0
        )
        assert (
            await _scalar(
                url,
                f"SELECT count(*) FROM outbox_events WHERE event_id = '{event_id}'::uuid",
            )
            == 0
        )

    async def test_outbox_worker_claim_lease(self, migrated_db) -> None:
        """The worker claim pattern: a claim sets claimed_by/claimed_until
        while moving pending -> processing; an expired lease can be
        re-claimed via processing -> pending (crash recovery)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        outbox_id = await _insert_outbox(
            url,
            event_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            event_type="operational.event",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE outbox_events SET status = 'processing', "
                        "claimed_by = 'worker-1', "
                        "claimed_until = now() + interval '30 seconds' "
                        "WHERE outbox_id = :oid"
                    ),
                    {"oid": outbox_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT claimed_by FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            == "worker-1"
        )
        # Lease expiry: the poller releases the claim back to pending.
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE outbox_events SET status = 'pending', "
                        "claimed_by = NULL, claimed_until = NULL "
                        "WHERE outbox_id = :oid"
                    ),
                    {"oid": outbox_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            == "pending"
        )
        assert (
            await _scalar(
                url,
                f"SELECT claimed_by IS NULL FROM outbox_events "
                f"WHERE outbox_id = '{outbox_id}'::uuid",
            )
            is True
        )

    async def test_outbox_unique_event_id(self, migrated_db) -> None:
        """Idempotent delivery — at most one outbox row per event_id."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_id = uuid.uuid4()
        await _insert_outbox(
            url,
            event_id=event_id,
            tenant_id=ids["tenant_a"],
            event_type="operational.event",
        )
        with pytest.raises(IntegrityError, match="uq_outbox_events_event_id"):
            await _insert_outbox(
                url,
                event_id=event_id,
                tenant_id=ids["tenant_a"],
                event_type="operational.event",
            )

    async def test_outbox_status_transitions(self, migrated_db) -> None:
        """pending -> processing -> published is legal; published is terminal
        (no re-publish)."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        outbox_id = await _insert_outbox(
            url,
            event_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            event_type="operational.event",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE outbox_events SET status = 'processing' WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
                await conn.execute(
                    text("UPDATE outbox_events SET status = 'published' WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
                # published is terminal — re-claiming is illegal.
                with pytest.raises(Exception, match="illegal outbox status transition"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE outbox_events SET status = 'processing' "
                                "WHERE outbox_id = :oid"
                            ),
                            {"oid": outbox_id},
                        )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            == "published"
        )
        # The trigger stamped published_at.
        assert (
            await _scalar(
                url,
                f"SELECT published_at IS NOT NULL FROM outbox_events "
                f"WHERE outbox_id = '{outbox_id}'::uuid",
            )
            is True
        )

    async def test_outbox_failed_retry_cycle(self, migrated_db) -> None:
        """failed -> pending -> processing is a legal retry path."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        outbox_id = await _insert_outbox(
            url,
            event_id=uuid.uuid4(),
            tenant_id=ids["tenant_a"],
            event_type="operational.event",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE outbox_events SET status = 'failed' WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
                await conn.execute(
                    text("UPDATE outbox_events SET status = 'pending' WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
                await conn.execute(
                    text("UPDATE outbox_events SET status = 'processing' WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            == "processing"
        )

    async def test_outbox_tenant_identity_recorded(self, migrated_db) -> None:
        """The outbox records the tenant for scoping/claims."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        outbox_id = await _insert_outbox(
            url,
            event_id=uuid.uuid4(),
            tenant_id=ids["tenant_b"],
            event_type="operational.event",
        )
        assert (
            await _scalar(
                url,
                f"SELECT tenant_id FROM outbox_events WHERE outbox_id = '{outbox_id}'::uuid",
            )
            == ids["tenant_b"]
        )

    # --- INBOX: deduplication + idempotent processing ---

    async def test_inbox_insertion_round_trip(self, migrated_db) -> None:
        """A received inbound message persists with its tenant identity."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        inbox_id = await _insert_inbox(
            url,
            tenant_id=ids["tenant_a"],
            source="pos.lightspeed",
            source_message_id="msg-1",
            payload={"event": "checkout"},
        )
        row = await _scalar(
            url,
            f"SELECT source || '|' || source_message_id FROM inbox_messages "
            f"WHERE inbox_id = '{inbox_id}'::uuid",
        )
        assert row == "pos.lightspeed|msg-1"
        assert (
            await _scalar(
                url,
                f"SELECT tenant_id FROM inbox_messages WHERE inbox_id = '{inbox_id}'::uuid",
            )
            == ids["tenant_a"]
        )

    async def test_inbox_duplicate_delivery_rejected(self, migrated_db) -> None:
        """Duplicate delivery of the same (source, source_message_id) is
        detected by the unique key and rejected."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        await _insert_inbox(
            url,
            tenant_id=ids["tenant_a"],
            source="pos.lightspeed",
            source_message_id="msg-dup",
        )
        with pytest.raises(IntegrityError, match="uq_inbox_messages_source_message_id"):
            await _insert_inbox(
                url,
                tenant_id=ids["tenant_a"],
                source="pos.lightspeed",
                source_message_id="msg-dup",
            )

    async def test_inbox_idempotent_processing(self, migrated_db) -> None:
        """A message is claimed, processed, and stamped — and a re-delivery
        of the same message is safely detected as a duplicate."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        inbox_id = await _insert_inbox(
            url,
            tenant_id=ids["tenant_a"],
            source="webhook.pos",
            source_message_id="msg-proc",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # Claim + process the same row (worker lifecycle).
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'processing' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'processed' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
                # A duplicate delivery of the SAME message cannot re-enter.
                with pytest.raises(IntegrityError, match="uq_inbox_messages_source_message_id"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO inbox_messages "
                                "(inbox_id, tenant_id, source, source_message_id, payload) "
                                "VALUES (:iid, :tid, 'webhook.pos', 'msg-proc', "
                                "CAST(:payload AS jsonb))"
                            ),
                            {
                                "iid": uuid.uuid4(),
                                "tid": ids["tenant_a"],
                                "payload": json.dumps({"event": "checkout"}),
                            },
                        )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM inbox_messages WHERE inbox_id = '{inbox_id}'::uuid",
            )
            == "processed"
        )
        # The trigger stamped processed_at.
        assert (
            await _scalar(
                url,
                f"SELECT processed_at IS NOT NULL FROM inbox_messages "
                f"WHERE inbox_id = '{inbox_id}'::uuid",
            )
            is True
        )

    async def test_inbox_terminal_state_immutable(self, migrated_db) -> None:
        """processed is terminal — a processed message cannot be re-claimed."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        inbox_id = await _insert_inbox(
            url,
            tenant_id=ids["tenant_a"],
            source="webhook.pos",
            source_message_id="msg-term",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                # Legal path: pending -> processing -> processed.
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'processing' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'processed' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
                # processed is terminal — re-claiming is illegal.
                with pytest.raises(Exception, match="illegal inbox status transition"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE inbox_messages SET status = 'pending' WHERE inbox_id = :iid"
                            ),
                            {"iid": inbox_id},
                        )
        finally:
            await engine.dispose()

    async def test_inbox_failed_retry_cycle(self, migrated_db) -> None:
        """failed -> pending -> processing is a legal retry path."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        inbox_id = await _insert_inbox(
            url,
            tenant_id=ids["tenant_a"],
            source="webhook.pos",
            source_message_id="msg-retry",
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'failed' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'pending' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
                await conn.execute(
                    text("UPDATE inbox_messages SET status = 'processing' WHERE inbox_id = :iid"),
                    {"iid": inbox_id},
                )
        finally:
            await engine.dispose()
        assert (
            await _scalar(
                url,
                f"SELECT status FROM inbox_messages WHERE inbox_id = '{inbox_id}'::uuid",
            )
            == "processing"
        )


# =============================================================================
# Task 6.13 — constraint & index review (migration 015)
# =============================================================================


class TestConstraintIndexReview:
    """Task 6.13 invariants: redundant indexes are dropped, the covering
    composites remain, and the query patterns they served still work."""

    # --- redundant single-column indexes dropped by migration 015 ---

    async def test_redundant_camera_config_index_dropped(self, migrated_db) -> None:
        """camera_id-only lookups use uq_camera_configs_version — the
        single-column ix_camera_configs_camera_id is gone."""
        url = migrated_db["url"]
        indexes = await _all_indexes(url)
        assert "ix_camera_configs_camera_id" not in indexes
        assert "uq_camera_configs_version" in indexes

    async def test_redundant_analysis_config_index_dropped(self, migrated_db) -> None:
        """venue_id-only lookups use uq_analysis_configs_version — the
        single-column ix_analysis_configs_venue_id is gone."""
        url = migrated_db["url"]
        indexes = await _all_indexes(url)
        assert "ix_analysis_configs_venue_id" not in indexes
        assert "uq_analysis_configs_version" in indexes

    async def test_redundant_event_time_index_dropped(self, migrated_db) -> None:
        """Global event_time lookups use the hypertable PK (event_time,
        event_id) — the dedicated ix_operational_events_event_time is gone."""
        url = migrated_db["url"]
        indexes = await _all_indexes(url)
        assert "ix_operational_events_event_time" not in indexes
        assert "operational_events_pkey" in indexes

    # --- the query patterns still work without the dropped indexes ---

    async def test_camera_config_camera_lookup_still_works(self, migrated_db) -> None:
        """A camera_id-scoped config lookup (the pattern the composite serves)
        returns rows correctly after the index drop."""
        url = migrated_db["url"]
        ids = await _seed_config(url)
        await _insert_camera_config(
            url,
            camera_id=ids["camera_a"],
            venue_id=ids["venue_a"],
            tenant_id=ids["tenant_a"],
        )
        count = await _scalar(
            url,
            f"SELECT count(*) FROM camera_configs WHERE camera_id = '{ids['camera_a']}'::uuid",
        )
        assert count == 1

    async def test_event_time_range_lookup_still_works(self, migrated_db) -> None:
        """A global event_time range query (the pattern the PK serves) still
        returns rows after dropping ix_operational_events_event_time."""
        url = migrated_db["url"]
        ids = await _seed_events(url)
        event_time = datetime.now(UTC) - timedelta(minutes=1)
        await _insert_event(
            url,
            tenant_id=ids["tenant_a"],
            venue_id=ids["venue_a"],
            event_type="detection",
            source="camera",
            event_time=event_time,
        )
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM operational_events "
                            "WHERE event_time >= :lo AND event_time <= :hi"
                        ),
                        {
                            "lo": event_time - timedelta(minutes=5),
                            "hi": event_time + timedelta(minutes=5),
                        },
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        assert row == 1

    async def test_explains_use_covering_composite(self, migrated_db) -> None:
        """EXPLAIN confirms the camera_configs composite unique CAN serve
        camera_id-only queries.

        On tiny tables the planner may legitimately prefer a seq scan (index
        startup cost exceeds the near-zero scan cost), so the planner's
        index choice is forced with enable_seqscan = off — the assertion is
        then deterministic and proves the index is a usable access path.
        """
        url = migrated_db["url"]
        ids = await _seed_config(url)
        engine = _query_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SET enable_seqscan = off"))
                rows = (
                    (
                        await conn.execute(
                            text(
                                "EXPLAIN SELECT config_id FROM camera_configs "
                                "WHERE camera_id = :cam"
                            ),
                            {"cam": ids["camera_a"]},
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()
        plan = "\n".join(rows)
        assert "uq_camera_configs_version" in plan, plan
