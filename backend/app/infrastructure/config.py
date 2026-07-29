"""Centralized typed configuration using Pydantic Settings.

All application configuration is loaded from environment variables
via a single Settings model. No os.getenv calls in application code.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: Annotated[str, Field(default="HotelOps AI", alias="APP_NAME")]
    app_env: Annotated[str, Field(default="development", alias="APP_ENV")]
    app_version: Annotated[str, Field(default="0.1.0", alias="APP_VERSION")]
    debug: Annotated[bool, Field(default=False, alias="DEBUG")]
    log_level: Annotated[str, Field(default="INFO", alias="LOG_LEVEL")]

    # --- API ---
    api_host: Annotated[str, Field(default="127.0.0.1", alias="API_HOST")]
    api_port: Annotated[int, Field(default=8000, alias="API_PORT", ge=1024, le=65535)]

    # --- PostgreSQL ---
    postgres_host: Annotated[str, Field(default="localhost", alias="POSTGRES_HOST")]
    postgres_port: Annotated[int, Field(default=5433, alias="POSTGRES_PORT", ge=1, le=65535)]
    postgres_db: Annotated[str, Field(default="hotelops", alias="POSTGRES_DB")]
    postgres_user: Annotated[str, Field(default="hotelops", alias="POSTGRES_USER")]
    postgres_password: Annotated[str, Field(default="CHANGE_ME", alias="POSTGRES_PASSWORD")]

    # --- Redis ---
    redis_host: Annotated[str, Field(default="localhost", alias="REDIS_HOST")]
    redis_port: Annotated[int, Field(default=6380, alias="REDIS_PORT", ge=1, le=65535)]

    # --- Object Storage ---
    object_storage_endpoint: Annotated[
        str, Field(default="http://localhost:9000", alias="OBJECT_STORAGE_ENDPOINT")
    ]
    object_storage_bucket: Annotated[
        str, Field(default="hotelops-development", alias="OBJECT_STORAGE_BUCKET")
    ]
    object_storage_access_key: Annotated[
        str, Field(default="minioadmin", alias="OBJECT_STORAGE_ACCESS_KEY")
    ]
    object_storage_secret_key: Annotated[
        str, Field(default="minioadmin", alias="OBJECT_STORAGE_SECRET_KEY")
    ]
    object_storage_region: Annotated[str, Field(default="us-east-1", alias="OBJECT_STORAGE_REGION")]

    # --- JWT / Authentication ---
    secret_key: Annotated[str, Field(default="CHANGE_ME_IN_PRODUCTION", alias="SECRET_KEY")]
    jwt_algorithm: Annotated[str, Field(default="HS256", alias="JWT_ALGORITHM")]
    jwt_expiration_minutes: Annotated[
        int, Field(default=60, alias="JWT_EXPIRATION_MINUTES", ge=1, le=43200)
    ]

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        """Restrict APP_ENV to known values."""
        allowed = {"development", "staging", "production", "test"}
        if v.lower() not in allowed:
            msg = f"APP_ENV must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v.lower()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            msg = f"LOG_LEVEL must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v.upper()

    @property
    def database_url(self) -> str:
        """Construct async database URL without exposing in logs."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Construct Redis URL."""
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    def __repr__(self) -> str:
        """Never print secret values."""
        return (
            f"Settings(app_name={self.app_name!r}, app_env={self.app_env!r}, "
            f"api_host={self.api_host!r}, api_port={self.api_port})"
        )
