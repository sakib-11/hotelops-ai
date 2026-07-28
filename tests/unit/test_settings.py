"""Tests for Settings validation."""

from __future__ import annotations

import pytest

from backend.app.infrastructure.config import Settings


class TestSettingsValidation:
    """Tests for Settings model validation."""

    def test_default_development_settings(self) -> None:
        """Default settings should be development."""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.app_env == "development"
        assert settings.app_name == "HotelOps AI"
        assert settings.app_version == "0.1.0"

    def test_valid_env_values(self) -> None:
        """APP_ENV must be one of: development, staging, production, test."""
        for env in ("development", "staging", "production", "test"):
            settings = Settings(_env_file=None, APP_ENV=env)  # type: ignore[call-arg]
            assert settings.app_env == env

    def test_invalid_env_raises(self) -> None:
        """Invalid APP_ENV should raise ValueError."""
        with pytest.raises(ValueError, match="APP_ENV"):
            Settings(_env_file=None, APP_ENV="invalid")  # type: ignore[call-arg]

    def test_valid_log_levels(self) -> None:
        """Valid log levels should be accepted."""
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            settings = Settings(_env_file=None, LOG_LEVEL=level)  # type: ignore[call-arg]
            assert settings.log_level == level

    def test_invalid_log_level_raises(self) -> None:
        """Invalid log level should raise ValueError."""
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            Settings(_env_file=None, LOG_LEVEL="TRACE")  # type: ignore[call-arg]

    def test_port_validation(self) -> None:
        """Ports must be in valid range."""
        with pytest.raises(ValueError):
            Settings(_env_file=None, API_PORT=0)  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            Settings(_env_file=None, API_PORT=70000)  # type: ignore[call-arg]

    def test_database_url_property(self) -> None:
        """database_url property should construct correct URL."""
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            POSTGRES_USER="test_user",
            POSTGRES_PASSWORD="test_pass",
            POSTGRES_HOST="test_host",
            POSTGRES_PORT=5432,
            POSTGRES_DB="test_db",
        )
        url = settings.database_url
        assert "test_user" in url
        assert "test_pass" in url
        assert "test_host" in url
        assert "test_db" in url
        assert url.startswith("postgresql+asyncpg://")

    def test_redis_url_property(self) -> None:
        """redis_url property should construct correct URL."""
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            REDIS_HOST="redis-test",
            REDIS_PORT=6379,
        )
        url = settings.redis_url
        assert "redis-test" in url
        assert url.startswith("redis://")

    def test_repr_no_secrets(self) -> None:
        """String representation should not contain secrets."""
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            POSTGRES_PASSWORD="super_secret",
            OBJECT_STORAGE_SECRET_KEY="super_secret_key",
        )
        repr_str = repr(settings)
        assert "super_secret" not in repr_str
        assert "super_secret_key" not in repr_str
