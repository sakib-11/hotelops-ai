"""Centralized typed configuration using Pydantic Settings.

All application configuration is loaded from environment variables
via a single Settings model. No os.getenv calls in application code.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Path to the project's pyproject.toml (single version source).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"


def _load_project_version() -> str:
    """Read the project version from ``pyproject.toml`` (single source).

    Falls back to ``"0.0.0"`` if the file is missing or unreadable — the
    application must still start.  This is the only place that reads the
    version from the project file.
    """
    try:
        with _PYPROJECT.open("rb") as f:
            data = tomllib.load(f)
        version = data["project"]["version"]
        if isinstance(version, str) and version:
            return version
    except OSError, KeyError, tomllib.TOMLDecodeError, TypeError:
        logger.warning("failed to read project version from %s", _PYPROJECT)
    return "0.0.0"


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    # Service name for display / health / logging (the project name in
    # pyproject.toml is the canonical package name, not the display name).
    app_name: Annotated[str, Field(default="HotelOps AI", alias="APP_NAME")]
    app_env: Annotated[str, Field(default="development", alias="APP_ENV")]
    # Single version source: pyproject.toml by default, overridable via
    # APP_VERSION env var (build pipelines, CI).
    app_version: Annotated[str, Field(default_factory=_load_project_version, alias="APP_VERSION")]
    debug: Annotated[bool, Field(default=False, alias="DEBUG")]
    log_level: Annotated[str, Field(default="INFO", alias="LOG_LEVEL")]
    # Build-time metadata (optional; absent in development).
    build_commit: Annotated[str, Field(default="", alias="BUILD_COMMIT")]
    build_timestamp: Annotated[str, Field(default="", alias="BUILD_TIMESTAMP")]

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

    # --- Task 7: Outbox Publisher ---
    outbox_poll_interval: Annotated[float, Field(default=1.0, alias="OUTBOX_POLL_INTERVAL", gt=0)]
    outbox_lease_seconds: Annotated[int, Field(default=60, alias="OUTBOX_LEASE_SECONDS", ge=1)]
    outbox_max_attempts: Annotated[int, Field(default=10, alias="OUTBOX_MAX_ATTEMPTS", ge=1)]
    outbox_backoff_base: Annotated[float, Field(default=1.0, alias="OUTBOX_BACKOFF_BASE", gt=0)]
    outbox_backoff_max: Annotated[float, Field(default=300.0, alias="OUTBOX_BACKOFF_MAX", gt=0)]
    outbox_backoff_jitter: Annotated[
        float, Field(default=0.1, alias="OUTBOX_BACKOFF_JITTER", ge=0, lt=1)
    ]

    # --- Task 7: Inbox Consumer ---
    inbox_poll_interval: Annotated[float, Field(default=1.0, alias="INBOX_POLL_INTERVAL", gt=0)]
    inbox_lease_seconds: Annotated[int, Field(default=60, alias="INBOX_LEASE_SECONDS", ge=1)]
    inbox_max_attempts: Annotated[int, Field(default=10, alias="INBOX_MAX_ATTEMPTS", ge=1)]
    inbox_backoff_base: Annotated[float, Field(default=1.0, alias="INBOX_BACKOFF_BASE", gt=0)]
    inbox_backoff_max: Annotated[float, Field(default=300.0, alias="INBOX_BACKOFF_MAX", gt=0)]
    inbox_backoff_jitter: Annotated[
        float, Field(default=0.1, alias="INBOX_BACKOFF_JITTER", ge=0, lt=1)
    ]

    # --- Task 7: Idempotency ---
    idempotency_lease_seconds: Annotated[
        int, Field(default=30, alias="IDEMPOTENCY_LEASE_SECONDS", ge=1)
    ]
    idempotency_wait_timeout_seconds: Annotated[
        float, Field(default=5.0, alias="IDEMPOTENCY_WAIT_TIMEOUT_SECONDS", gt=0)
    ]
    idempotency_wait_poll_seconds: Annotated[
        float, Field(default=0.05, alias="IDEMPOTENCY_WAIT_POLL_SECONDS", gt=0)
    ]

    # --- Task 7: Redis event transport ---
    redis_stream_events: Annotated[
        str, Field(default="hotelops:events", alias="REDIS_STREAM_EVENTS")
    ]
    redis_consumer_group: Annotated[
        str, Field(default="hotelops-workers", alias="REDIS_CONSUMER_GROUP")
    ]
    redis_stream_claim_idle_seconds: Annotated[
        int, Field(default=120, alias="REDIS_STREAM_CLAIM_IDLE_SECONDS", ge=1)
    ]

    # --- Task 8: Observability ---
    # Log output format: 'json' (one JSON object per line — the default,
    # consumable by log shippers/Grafana) or 'text' (human-readable
    # key=value for local development).
    observability_log_format: Annotated[
        str, Field(default="json", alias="OBSERVABILITY_LOG_FORMAT")
    ]
    # Metrics and tracing are OPT-IN: all telemetry is disabled by
    # default so the application starts and runs without any external
    # observability server. Enabling tracing only initializes the
    # OpenTelemetry SDK when the instrumentation is wired in; the OTLP
    # endpoint below is never contacted while disabled.
    observability_metrics_enabled: Annotated[
        bool, Field(default=False, alias="OBSERVABILITY_METRICS_ENABLED")
    ]
    observability_tracing_enabled: Annotated[
        bool, Field(default=False, alias="OBSERVABILITY_TRACING_ENABLED")
    ]
    # OpenTelemetry resource/service identity and OTLP export endpoint
    # (HTTP/protobuf). Only meaningful when tracing is enabled.
    otel_service_name: Annotated[str, Field(default="hotelops-ai", alias="OTEL_SERVICE_NAME")]
    otel_otlp_endpoint: Annotated[
        str, Field(default="http://localhost:4318", alias="OTEL_OTLP_ENDPOINT")
    ]
    # Root span sampling ratio (0.0 = trace nothing, 1.0 = trace all).
    otel_sample_ratio: Annotated[
        float, Field(default=0.1, alias="OTEL_SAMPLE_RATIO", ge=0.0, le=1.0)
    ]

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
    object_storage_use_ssl: Annotated[bool, Field(default=False, alias="OBJECT_STORAGE_USE_SSL")]

    # --- Task 9: Media Lifecycle ---
    # Presigned access TTL (initiate, complete, download, multipart parts).
    media_presigned_url_ttl_seconds: Annotated[
        int, Field(default=900, alias="MEDIA_PRESIGNED_URL_TTL_SECONDS", ge=60, le=86400)
    ]
    # Suggested part size for client multipart uploads (S3 minimum 5 MiB).
    media_multipart_part_size_bytes: Annotated[
        int,
        Field(
            default=64 * 1024 * 1024, alias="MEDIA_MULTIPART_PART_SIZE_BYTES", ge=5 * 1024 * 1024
        ),
    ]
    # Hard cap on the number of multipart parts (S3 limit is 10,000).
    media_max_parts: Annotated[int, Field(default=10000, alias="MEDIA_MAX_PARTS", ge=1, le=10000)]
    # An UPLOADING record abandoned longer than this is swept to FAILED.
    media_upload_timeout_seconds: Annotated[
        int, Field(default=86400, alias="MEDIA_UPLOAD_TIMEOUT_SECONDS", ge=60)
    ]
    # Server-side SHA-256 recomputation during verification.
    media_checksum_verification_enabled: Annotated[
        bool, Field(default=True, alias="MEDIA_CHECKSUM_VERIFICATION_ENABLED")
    ]
    # Objects larger than this skip full-stream checksum recomputation
    # (0 = always verify). Size + content validation always apply.
    media_checksum_verification_max_bytes: Annotated[
        int, Field(default=0, alias="MEDIA_CHECKSUM_VERIFICATION_MAX_BYTES", ge=0)
    ]
    # Magic-byte content validation on the verify step.
    media_content_validation_enabled: Annotated[
        bool, Field(default=True, alias="MEDIA_CONTENT_VALIDATION_ENABLED")
    ]
    # Cleanup worker sweep cadence and batch size.
    media_cleanup_poll_interval: Annotated[
        float, Field(default=60.0, alias="MEDIA_CLEANUP_POLL_INTERVAL", gt=0)
    ]
    media_cleanup_batch_size: Annotated[
        int, Field(default=50, alias="MEDIA_CLEANUP_BATCH_SIZE", ge=1, le=500)
    ]
    # Grace period before a record whose object is missing is marked FAILED
    # (a transient provider hiccup must never nuke a valid record).
    media_missing_object_grace_seconds: Annotated[
        int, Field(default=86400, alias="MEDIA_MISSING_OBJECT_GRACE_SECONDS", ge=300)
    ]
    # Orphan-object deletion is OFF by default: reconciliation only
    # reports Type-A orphans unless an operator explicitly enables
    # deletion AND the object has aged past the grace period.
    media_orphan_object_deletion_enabled: Annotated[
        bool, Field(default=False, alias="MEDIA_ORPHAN_OBJECT_DELETION_ENABLED")
    ]
    media_orphan_object_grace_seconds: Annotated[
        int, Field(default=604800, alias="MEDIA_ORPHAN_OBJECT_GRACE_SECONDS", ge=3600)
    ]

    # --- Task 17.11: Evidence processing worker ---
    # The async evidence worker (PollingWorker) drives EvidenceRefs
    # through the durable state machine (Task 17.10): queue → claim
    # (lease) → resolve → extract → verify → upload → package →
    # finalize. Retry/backoff/dead-letter reuse the Task 7 reliability
    # primitives (compute_backoff_delay, bounded attempts); the lease
    # reclaims crashed claims exactly like the outbox publisher.
    evidence_worker_poll_interval: Annotated[
        float, Field(default=1.0, alias="EVIDENCE_WORKER_POLL_INTERVAL", gt=0)
    ]
    evidence_worker_batch_size: Annotated[
        int, Field(default=10, alias="EVIDENCE_WORKER_BATCH_SIZE", ge=1, le=500)
    ]
    evidence_worker_lease_seconds: Annotated[
        int, Field(default=60, alias="EVIDENCE_WORKER_LEASE_SECONDS", ge=1)
    ]
    evidence_worker_max_attempts: Annotated[
        int, Field(default=5, alias="EVIDENCE_WORKER_MAX_ATTEMPTS", ge=1)
    ]
    evidence_worker_backoff_base: Annotated[
        float, Field(default=1.0, alias="EVIDENCE_WORKER_BACKOFF_BASE", gt=0)
    ]
    evidence_worker_backoff_max: Annotated[
        float, Field(default=300.0, alias="EVIDENCE_WORKER_BACKOFF_MAX", gt=0)
    ]
    evidence_worker_backoff_jitter: Annotated[
        float, Field(default=0.1, alias="EVIDENCE_WORKER_BACKOFF_JITTER", ge=0, lt=1)
    ]
    # A REQUESTED/QUEUED ref abandoned longer than this is EXPIRED (the
    # state machine's EXPIRED terminal state — never silently dropped).
    evidence_worker_request_timeout_seconds: Annotated[
        int, Field(default=86400, alias="EVIDENCE_WORKER_REQUEST_TIMEOUT_SECONDS", ge=60)
    ]

    # --- Task 12: Detection — model artifact/version governance ---
    # The single governed model definition for object detection.  The
    # YOLOv8Adapter consumes the approved model via the model registry
    # (ModelRegistry.from_settings) and never invents identity, version
    # or artifact paths.  Artifact URIs are environment-independent
    # references — no developer-specific paths, no model binaries in
    # source control.
    detection_model_id: Annotated[
        str, Field(default="yolo-person-detector", alias="DETECTION_MODEL_ID")
    ]
    detection_model_name: Annotated[str, Field(default="yolov8n", alias="DETECTION_MODEL_NAME")]
    detection_model_version: Annotated[str, Field(default="8.1.0", alias="DETECTION_MODEL_VERSION")]
    detection_model_family: Annotated[str, Field(default="yolov8", alias="DETECTION_MODEL_FAMILY")]
    detection_runtime: Annotated[str, Field(default="ultralytics", alias="DETECTION_RUNTIME")]
    detection_artifact_uri: Annotated[
        str, Field(default="memory://default/yolov8n.pt", alias="DETECTION_ARTIFACT_URI")
    ]
    detection_artifact_sha256: Annotated[
        str,
        Field(default="0" * 64, alias="DETECTION_ARTIFACT_SHA256", min_length=64, max_length=64),
    ]
    detection_device: Annotated[str, Field(default="auto", alias="DETECTION_DEVICE")]
    # Comma-separated class names of the configured model (the adapter
    # validates the loaded artifact's class table against these).
    detection_class_names: Annotated[
        str, Field(default="person,bag", alias="DETECTION_CLASS_NAMES")
    ]

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

    @field_validator("observability_log_format")
    @classmethod
    def validate_observability_log_format(cls, v: str) -> str:
        """Restrict OBSERVABILITY_LOG_FORMAT to known values."""
        allowed = {"text", "json"}
        if v.lower() not in allowed:
            msg = f"OBSERVABILITY_LOG_FORMAT must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v.lower()

    @field_validator("object_storage_bucket")
    @classmethod
    def validate_object_storage_bucket(cls, v: str) -> str:
        """Validate S3-compliant bucket name."""
        cleaned = v.strip().lower()
        if not cleaned or len(cleaned) < 3 or len(cleaned) > 63:
            msg = f"OBJECT_STORAGE_BUCKET must be between 3 and 63 characters, got '{v}'"
            raise ValueError(msg)
        return cleaned

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
