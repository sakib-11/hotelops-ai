"""Unit tests for the Alembic migration configuration (Task 6.2).

Offline tests — no database required. Verify the migration scripts are
discoverable, ORM metadata registers on the shared Base.metadata, and the
revision chain is intact.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

import backend.app.infrastructure.database.models  # ruff: ignore[unused-import]  (registers Base.metadata)
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "database" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

EXPECTED_MIGRATION_HEAD = "019_temporal_fact_persistence"
EXPECTED_IDENTITY_TABLES = frozenset({
    "tenants",
    "venues",
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "memberships",
    "membership_venues",
})
EXPECTED_CONFIG_TABLES = frozenset({
    "camera_configs",
    "analysis_configs",
})
EXPECTED_EVENT_TABLES = frozenset({
    "operational_events",
})
EXPECTED_EVIDENCE_TABLES = frozenset({
    "evidence_refs",
    "evidence_packages",
    "package_evidence_refs",
})
EXPECTED_MEDIA_TABLES = frozenset({
    "media_assets",
})
EXPECTED_ANALYTICS_TABLES = frozenset({
    "metrics",
    "opportunities",
    "opportunity_metrics",
    "opportunity_evidence_refs",
})
EXPECTED_AI_TABLES = frozenset({
    "findings",
    "recommendations",
    "recommendation_findings",
})
EXPECTED_ALERT_APPROVAL_TABLES = frozenset({
    "alerts",
    "approval_requests",
    "approval_decisions",
})
EXPECTED_INTEGRATION_TABLES = frozenset({
    "integrations",
})
EXPECTED_AUDIT_OUTBOX_INBOX_TABLES = frozenset({
    "audit_events",
    "outbox_events",
    "inbox_messages",
})
EXPECTED_IDEMPOTENCY_TABLES = frozenset({
    "idempotency_records",
})


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg)


class TestAlembicConfiguration:
    """Alembic must point at the repository's migration scripts."""

    def test_script_location_resolves(self) -> None:
        cfg = Config(str(ALEMBIC_INI))
        location = cfg.get_main_option("script_location")
        assert location == "database/migrations"
        assert (REPO_ROOT / location).is_dir()

    def test_migration_scripts_are_discoverable(self) -> None:
        versions = MIGRATIONS_DIR / "versions"
        assert (versions / "001_create_identity_tables.py").is_file()
        assert (versions / "002_enable_rls.py").is_file()
        assert (versions / "003_membership_venue_scope.py").is_file()
        assert (versions / "004_tenancy_check_constraints.py").is_file()
        assert (versions / "005_video_domain_schema.py").is_file()
        assert (versions / "006_video_rls.py").is_file()
        assert (versions / "007_operational_config_schema.py").is_file()
        assert (versions / "008_operational_events.py").is_file()
        assert (versions / "009_evidence_persistence.py").is_file()
        assert (versions / "010_analytics_storage.py").is_file()
        assert (versions / "011_ai_domain_storage.py").is_file()
        assert (versions / "012_alert_approval_storage.py").is_file()
        assert (versions / "013_integration_storage.py").is_file()
        assert (versions / "014_audit_outbox_inbox.py").is_file()
        assert (versions / "015_constraint_index_review.py").is_file()
        assert (versions / "016_outbox_retry_idempotency.py").is_file()
        assert (versions / "017_media_metadata_schema.py").is_file()


class TestMetadataRegistration:
    """Importing the models package registers every table on Base.metadata."""

    def test_identity_models_registered_on_shared_metadata(self) -> None:
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_IDENTITY_TABLES

    def test_media_models_registered_on_shared_metadata(self) -> None:
        """Task 9.6 — media_assets registers on the shared registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_MEDIA_TABLES

    def test_config_models_registered_on_shared_metadata(self) -> None:
        """Task 6.5 — config tables register on the shared metadata registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_CONFIG_TABLES

    def test_event_models_registered_on_shared_metadata(self) -> None:
        """Task 6.6 — the event table registers on the shared metadata registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_EVENT_TABLES

    def test_evidence_models_registered_on_shared_metadata(self) -> None:
        """Task 6.7 — the evidence tables register on the shared metadata registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_EVIDENCE_TABLES

    def test_analytics_models_registered_on_shared_metadata(self) -> None:
        """Task 6.8 — the analytics tables register on the shared metadata registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_ANALYTICS_TABLES

    def test_ai_models_registered_on_shared_metadata(self) -> None:
        """Task 6.9 — the AI domain tables register on the shared metadata registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_AI_TABLES

    def test_alert_approval_models_registered_on_shared_metadata(self) -> None:
        """Task 6.10 — the alert/approval tables register on the shared registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_ALERT_APPROVAL_TABLES

    def test_integration_models_registered_on_shared_metadata(self) -> None:
        """Task 6.11 — the integrations table registers on the shared registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_INTEGRATION_TABLES

    def test_audit_outbox_inbox_models_registered_on_shared_metadata(self) -> None:
        """Task 6.12 — the audit/outbox/inbox tables register on the shared
        registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_AUDIT_OUTBOX_INBOX_TABLES

    def test_idempotency_models_registered_on_shared_metadata(self) -> None:
        """Task 7 — the idempotency_records table registers on the shared
        registry."""
        tables = set(Base.metadata.tables)
        assert tables >= EXPECTED_IDEMPOTENCY_TABLES


class TestMigrationDiscovery:
    """The repository exposes exactly one, known migration head."""

    def test_single_head(self) -> None:
        heads = _script_directory().get_heads()
        assert len(heads) == 1, f"Expected a single migration head, got {heads}"

    def test_head_matches_documented_expected_head(self) -> None:
        assert _script_directory().get_current_head() == EXPECTED_MIGRATION_HEAD

    def test_revision_chain_is_linear(self) -> None:
        sd = _script_directory()
        downs = [r.down_revision for r in sd.walk_revisions()]
        assert len(downs) == len(set(downs)), "Branching migration chain detected"
        assert downs[-1] is None


class TestDatabaseUrlConfiguration:
    """The default database URL comes from the single configuration system."""

    def test_settings_database_url_default(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.database_url == (
            "postgresql+asyncpg://hotelops:CHANGE_ME@localhost:5433/hotelops"
        )

    def test_settings_aliases_map_to_url(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            POSTGRES_HOST="db.internal",
            POSTGRES_PORT=5544,
            POSTGRES_DB="ops",
            POSTGRES_USER="svc",
            POSTGRES_PASSWORD="secret",
        )
        assert settings.database_url == "postgresql+asyncpg://svc:secret@db.internal:5544/ops"
