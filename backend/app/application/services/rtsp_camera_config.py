"""RTSP Camera Runtime Configuration (Task 19.3).

This module provides the runtime configuration required to connect a configured
camera via RTSP without leaking credentials.

Key principles:
- Reuses Task 10 CameraConfigModel (versioned, published configuration)
- Credentials are NEVER stored in DB, logs, FramePacket, EventEnvelope, API responses, telemetry, or error messages
- RTSP endpoint is stored in VideoStreamModel.source_url (may contain credentials)
- Credentials are resolved at runtime from environment/secrets manager
- Configuration version must be PUBLISHED
- Camera must belong to tenant and venue
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.models.config import CameraConfigModel
from backend.app.infrastructure.database.models.video import CameraModel, VideoStreamModel
from contracts.common import CameraId, ConfigurationVersionId, TenantId, VenueId
from contracts.video.models import SourceType


class QueueFullPolicy(StrEnum):
    """Queue-full policy for the bounded frame queue."""

    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"


@dataclass(frozen=True, slots=True)
class RtspReconnectPolicy:
    """Bounded exponential backoff policy for RTSP reconnection."""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay_seconds <= 0:
            raise ValueError(f"base_delay_seconds must be > 0, got {self.base_delay_seconds}")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                f"max_delay_seconds ({self.max_delay_seconds}) must be >= "
                f"base_delay_seconds ({self.base_delay_seconds})"
            )
        if not 0 <= self.jitter < 1:
            raise ValueError(f"jitter must satisfy 0 <= jitter < 1, got {self.jitter}")


@dataclass(frozen=True, slots=True)
class RtspCameraRuntimeConfig:
    """
    Resolved runtime configuration for an RTSP camera.

    This is the canonical configuration passed to the ingestion worker.
    It contains NO credentials - they are resolved separately at connection time.
    """

    # Identity
    camera_id: CameraId
    tenant_id: TenantId
    venue_id: VenueId
    stream_id: str  # VideoStreamModel.stream_id
    configuration_version_id: ConfigurationVersionId

    # RTSP connection (credential-free)
    rtsp_endpoint: str  # Host:port/path only, no userinfo
    stream_profile: "CameraStreamProfile"
    transport: str = "tcp"  # "tcp" | "udp" | "udp_multicast" | "http"

    # Connection parameters
    connection_timeout_seconds: float = 10.0
    heartbeat_interval_seconds: float = 30.0
    stale_threshold_seconds: float = 60.0

    # Reconnect policy
    reconnect_policy: RtspReconnectPolicy = RtspReconnectPolicy(
        max_attempts=5,
        base_delay_seconds=2.0,
        max_delay_seconds=60.0,
        jitter=0.1,
    )

    # Queue policy
    queue_max_size: int = 16
    queue_full_policy: QueueFullPolicy = QueueFullPolicy.DROP_OLDEST

    # State
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.connection_timeout_seconds <= 0:
            raise ValueError("connection_timeout_seconds must be > 0")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be > 0")
        if self.stale_threshold_seconds <= 0:
            raise ValueError("stale_threshold_seconds must be > 0")
        if self.queue_max_size < 1:
            raise ValueError("queue_max_size must be >= 1")


class CameraStreamProfile(BaseModel, frozen=True):
    """Stream profile derived from CameraConfigModel + CameraProfileModel."""

    model_config = {"extra": "forbid"}

    # From CameraProfileModel
    profile_id: str
    camera_reference: str
    resolution_width: int
    resolution_height: int
    fps: Decimal | None = None
    codec: str | None = None
    image_orientation: int = 0

    # From CameraConfigModel
    analysis_enabled: bool = True
    frame_rate: Decimal | None = None
    detection_sensitivity: Decimal | None = None

    # Derived
    effective_fps: Decimal | None = None

    @model_validator(mode="after")
    def _compute_effective_fps(self) -> CameraStreamProfile:
        """Effective FPS is config.frame_rate if set, else profile.fps."""
        # This would be set by the resolver; here we just ensure it's present
        return self


def redact_rtsp_url(url: str) -> str:
    """Strip userinfo (credentials) from an RTSP URL for safe logging.

    ``rtsp://user:secret@cam.local:554/stream`` → ``rtsp://cam.local:554/stream``.
    Non-credential URLs are returned unchanged.
    """
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def extract_credentials_from_url(url: str) -> tuple[str | None, str | None]:
    """Extract username and password from RTSP URL.

    Returns (username, password) or (None, None) if not present.
    """
    parts = urlsplit(url)
    return parts.username, parts.password


class RtspCameraConfigResolver:
    """
    Resolves the complete runtime configuration for an RTSP camera.

    This is the single entry point for obtaining camera runtime config.
    It enforces all validation rules and ensures credentials are never exposed.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def resolve(
        self,
        session,  # AsyncSession
        *,
        camera_id: CameraId,
        tenant_id: TenantId,
        venue_id: VenueId | None = None,
    ) -> RtspCameraRuntimeConfig:
        """
        Resolve runtime configuration for a camera.

        Validates:
        - Camera exists and belongs to tenant
        - Camera belongs to venue (if venue_id provided)
        - Camera is active
        - Camera protocol is RTSP
        - VideoStream exists with RTSP endpoint
        - CameraConfig exists and is active (published)
        - Configuration version is published

        Returns:
            RtspCameraRuntimeConfig with credential-free RTSP endpoint
        """
        from sqlalchemy import select

        # 1. Load camera with tenant/venue scope
        stmt = select(CameraModel).where(
            CameraModel.camera_id == camera_id,
            CameraModel.tenant_id == tenant_id,
        )
        if venue_id:
            stmt = stmt.where(CameraModel.venue_id == venue_id)

        result = await session.execute(stmt)
        camera = result.scalar_one_or_none()

        if camera is None:
            raise CameraNotFoundError(f"Camera {camera_id} not found for tenant {tenant_id}")

        if camera.status != "active":
            raise CameraDisabledError(f"Camera {camera_id} is not active (status: {camera.status})")

        if camera.protocol != "rtsp":
            raise CameraProtocolError(f"Camera {camera_id} protocol is {camera.protocol}, expected RTSP")

        # 2. Load video stream (RTSP endpoint)
        stmt = select(VideoStreamModel).where(
            VideoStreamModel.camera_id == camera_id,
            VideoStreamModel.tenant_id == tenant_id,
            VideoStreamModel.status == "active",
        )
        result = await session.execute(stmt)
        stream = result.scalar_one_or_none()

        if stream is None:
            raise StreamNotFoundError(f"No active video stream for camera {camera_id}")

        if not stream.source_url:
            raise MissingEndpointError(f"Video stream {stream.stream_id} has no RTSP endpoint")

        # 3. Load active camera configuration
        stmt = select(CameraConfigModel).where(
            CameraConfigModel.camera_id == camera_id,
            CameraConfigModel.tenant_id == tenant_id,
            CameraConfigModel.status == "active",
        )
        result = await session.execute(stmt)
        camera_config = result.scalar_one_or_none()

        if camera_config is None:
            raise ConfigurationNotFoundError(f"No active configuration for camera {camera_id}")

        # 4. Resolve configuration version (must be published)
        # The session pins to a specific published version
        # For live sessions, we need the current published version
        from backend.app.domain.configuration.service import ConfigurationService

        config_service = ConfigurationService()
        published_version = await config_service.resolve_current_published(
            session=session,
            actor=None,  # System resolution
            venue_id=camera.venue_id,
        )

        if published_version is None:
            raise ConfigurationNotPublishedError(f"No published configuration for venue {camera.venue_id}")

        configuration_version_id = ConfigurationVersionId(published_version.configuration_version_id)

        # 5. Extract stream profile from published version
        stream_profile = self._extract_stream_profile(
            published_version, camera_id, camera_config
        )

        # 6. Build credential-free RTSP endpoint
        redacted_endpoint = redact_rtsp_url(stream.source_url)

        # 7. Build reconnect policy from config or defaults
        reconnect_policy = self._build_reconnect_policy(camera_config)

        # 8. Build queue policy
        queue_max_size = camera_config.parameters.get("queue_max_size", 16) if camera_config.parameters else 16
        queue_full_policy_str = camera_config.parameters.get("queue_full_policy", "drop_oldest") if camera_config.parameters else "drop_oldest"
        queue_full_policy = QueueFullPolicy(queue_full_policy_str)

        return RtspCameraRuntimeConfig(
            camera_id=CameraId(camera.camera_id),
            tenant_id=TenantId(camera.tenant_id),
            venue_id=VenueId(camera.venue_id),
            stream_id=str(stream.stream_id),
            configuration_version_id=configuration_version_id,
            rtsp_endpoint=redacted_endpoint,
            stream_profile=stream_profile,
            connection_timeout_seconds=camera_config.parameters.get("connection_timeout_seconds", 10.0) if camera_config.parameters else 10.0,
            heartbeat_interval_seconds=camera_config.parameters.get("heartbeat_interval_seconds", 30.0) if camera_config.parameters else 30.0,
            stale_threshold_seconds=camera_config.parameters.get("stale_threshold_seconds", 60.0) if camera_config.parameters else 60.0,
            reconnect_policy=reconnect_policy,
            queue_max_size=queue_max_size,
            queue_full_policy=queue_full_policy,
            enabled=camera_config.analysis_enabled,
        )

    def _extract_stream_profile(
        self,
        published_version,
        camera_id: CameraId,
        camera_config: CameraConfigModel,
    ) -> CameraStreamProfile:
        """Extract stream profile from published configuration version."""
        # Find the camera profile matching this camera_id
        camera_profile = None
        for cam in published_version.cameras:
            if cam.camera_id == camera_id:
                camera_profile = cam
                break

        if camera_profile is None:
            raise CameraProfileNotFoundError(f"Camera {camera_id} not found in published configuration")

        # Compute effective FPS
        effective_fps = camera_config.frame_rate or camera_profile.fps

        return CameraStreamProfile(
            profile_id=camera_profile.profile_id,
            camera_reference=camera_profile.camera_reference,
            resolution_width=camera_profile.resolution_width,
            resolution_height=camera_profile.resolution_height,
            fps=camera_profile.fps,
            codec=camera_profile.codec,
            image_orientation=camera_profile.image_orientation,
            analysis_enabled=camera_config.analysis_enabled,
            frame_rate=camera_config.frame_rate,
            detection_sensitivity=camera_config.detection_sensitivity,
            effective_fps=effective_fps,
        )

    def _build_reconnect_policy(self, camera_config: CameraConfigModel) -> RtspReconnectPolicy:
        """Build reconnect policy from camera config parameters."""
        if not camera_config.parameters:
            return RtspReconnectPolicy(
                max_attempts=5,
                base_delay_seconds=2.0,
                max_delay_seconds=60.0,
                jitter=0.1,
            )

        params = camera_config.parameters
        return RtspReconnectPolicy(
            max_attempts=params.get("reconnect_max_attempts", 5),
            base_delay_seconds=params.get("reconnect_base_delay_seconds", 2.0),
            max_delay_seconds=params.get("reconnect_max_delay_seconds", 60.0),
            jitter=params.get("reconnect_jitter", 0.1),
        )

    def get_credentials_for_stream(
        self,
        stream: VideoStreamModel,
    ) -> tuple[str | None, str | None]:
        """
        Get credentials for a stream from the source_url.

        This is the ONLY place credentials are extracted from the URL.
        The returned credentials should be used ONLY for the RTSP transport
        connection and NEVER logged, stored, or exposed.
        """
        if not stream.source_url:
            return None, None
        return extract_credentials_from_url(stream.source_url)


# =============================================================================
# Exceptions
# =============================================================================


class CameraConfigError(Exception):
    """Base exception for camera configuration errors."""


class CameraNotFoundError(CameraConfigError):
    """Camera not found or not accessible by tenant."""


class CameraDisabledError(CameraConfigError):
    """Camera is not active."""


class CameraProtocolError(CameraConfigError):
    """Camera protocol is not RTSP."""


class StreamNotFoundError(CameraConfigError):
    """No active video stream for camera."""


class MissingEndpointError(CameraConfigError):
    """Video stream has no RTSP endpoint."""


class ConfigurationNotFoundError(CameraConfigError):
    """No active camera configuration found."""


class ConfigurationNotPublishedError(CameraConfigError):
    """No published configuration version for venue."""


class CameraProfileNotFoundError(CameraConfigError):
    """Camera not found in published configuration."""


__all__ = [
    "QueueFullPolicy",
    "RtspReconnectPolicy",
    "RtspCameraRuntimeConfig",
    "CameraStreamProfile",
    "RtspCameraConfigResolver",
    "redact_rtsp_url",
    "extract_credentials_from_url",
    "CameraConfigError",
    "CameraNotFoundError",
    "CameraDisabledError",
    "CameraProtocolError",
    "StreamNotFoundError",
    "MissingEndpointError",
    "ConfigurationNotFoundError",
    "ConfigurationNotPublishedError",
    "CameraProfileNotFoundError",
]