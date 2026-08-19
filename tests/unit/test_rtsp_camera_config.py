"""Unit tests for RTSP Camera Configuration (Task 19.3).

Tests all required scenarios:
- Valid configuration
- Missing endpoint
- Invalid endpoint
- Missing credential
- Unauthorized camera
- Disabled camera
- Wrong tenant
- Wrong venue
- Secret redaction
"""

from __future__ import annotations

import pytest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from backend.app.application.services.rtsp_camera_config import (
    CameraConfigError,
    CameraDisabledError,
    CameraNotFoundError,
    CameraProfileNotFoundError,
    CameraProtocolError,
    ConfigurationNotFoundError,
    ConfigurationNotPublishedError,
    MissingEndpointError,
    QueueFullPolicy,
    RtspCameraConfigResolver,
    RtspCameraRuntimeConfig,
    RtspReconnectPolicy,
    CameraStreamProfile,
    StreamNotFoundError,
    redact_rtsp_url,
    extract_credentials_from_url,
)
from backend.app.infrastructure.database.models.config import CameraConfigModel
from backend.app.infrastructure.database.models.video import CameraModel, VideoStreamModel
from contracts.common import CameraId, ConfigurationVersionId, TenantId, VenueId
from contracts.video.models import SourceType


# Test data
TENANT_ID = TenantId(uuid4())
VENUE_ID = VenueId(uuid4())
CAMERA_ID = CameraId(uuid4())
STREAM_ID = uuid4()
CONFIG_VERSION_ID = ConfigurationVersionId(uuid4())


class TestRedactRtspUrl:
    """Test credential redaction from RTSP URLs."""

    def test_credentials_stripped(self) -> None:
        url = "rtsp://admin:secret@cam1.local:554/live"
        expected = "rtsp://cam1.local:554/live"
        assert redact_rtsp_url(url) == expected

    def test_clean_url_unchanged(self) -> None:
        url = "rtsp://cam1.local:554/live"
        assert redact_rtsp_url(url) == url

    def test_password_only_stripped(self) -> None:
        url = "rtsp://:secret@cam1.local:554/live"
        expected = "rtsp://cam1.local:554/live"
        assert redact_rtsp_url(url) == expected

    def test_username_only_stripped(self) -> None:
        url = "rtsp://admin@cam1.local:554/live"
        expected = "rtsp://cam1.local:554/live"
        assert redact_rtsp_url(url) == expected

    def test_complex_url_with_path_and_query(self) -> None:
        url = "rtsp://user:pass@cam.local:554/path/to/stream?param=value"
        expected = "rtsp://cam.local:554/path/to/stream?param=value"
        assert redact_rtsp_url(url) == expected


class TestExtractCredentials:
    """Test credential extraction from RTSP URLs."""

    def test_extract_both(self) -> None:
        url = "rtsp://admin:secret@cam1.local:554/live"
        username, password = extract_credentials_from_url(url)
        assert username == "admin"
        assert password == "secret"

    def test_extract_username_only(self) -> None:
        url = "rtsp://admin@cam1.local:554/live"
        username, password = extract_credentials_from_url(url)
        assert username == "admin"
        assert password is None

    def test_extract_password_only(self) -> None:
        url = "rtsp://:secret@cam1.local:554/live"
        username, password = extract_credentials_from_url(url)
        assert username == "" or username is None
        assert password == "secret"

    def test_no_credentials(self) -> None:
        url = "rtsp://cam1.local:554/live"
        username, password = extract_credentials_from_url(url)
        assert username is None
        assert password is None


class TestRtspReconnectPolicy:
    """Test reconnect policy validation."""

    def test_valid_policy(self) -> None:
        policy = RtspReconnectPolicy(
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=60.0,
            jitter=0.1,
        )
        assert policy.max_attempts == 5

    def test_invalid_max_attempts(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            RtspReconnectPolicy(max_attempts=0, base_delay_seconds=1.0, max_delay_seconds=2.0)

    def test_invalid_base_delay(self) -> None:
        with pytest.raises(ValueError, match="base_delay_seconds must be > 0"):
            RtspReconnectPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=2.0)

    def test_invalid_max_delay(self) -> None:
        with pytest.raises(ValueError, match="max_delay_seconds.*must be >="):
            RtspReconnectPolicy(max_attempts=1, base_delay_seconds=2.0, max_delay_seconds=1.0)

    def test_invalid_jitter(self) -> None:
        with pytest.raises(ValueError, match="jitter must satisfy 0 <= jitter < 1"):
            RtspReconnectPolicy(max_attempts=1, base_delay_seconds=1.0, max_delay_seconds=2.0, jitter=1.0)


class TestCameraStreamProfile:
    """Test camera stream profile."""

    def test_valid_profile(self) -> None:
        profile = CameraStreamProfile(
            profile_id="cam1",
            camera_reference="CAM-001",
            resolution_width=1920,
            resolution_height=1080,
            fps=Decimal("30.0"),
            codec="h264",
            image_orientation=0,
            analysis_enabled=True,
            frame_rate=Decimal("15.0"),
            detection_sensitivity=Decimal("0.5"),
            effective_fps=Decimal("15.0"),
        )
        assert profile.profile_id == "cam1"
        assert profile.effective_fps == Decimal("15.0")


class TestRtspCameraRuntimeConfig:
    """Test runtime config validation."""

    def test_valid_config(self) -> None:
        profile = CameraStreamProfile(
            profile_id="cam1",
            camera_reference="CAM-001",
            resolution_width=1920,
            resolution_height=1080,
        )
        config = RtspCameraRuntimeConfig(
            camera_id=CAMERA_ID,
            tenant_id=TENANT_ID,
            venue_id=VENUE_ID,
            stream_id=str(STREAM_ID),
            configuration_version_id=CONFIG_VERSION_ID,
            rtsp_endpoint="rtsp://cam1.local:554/live",
            stream_profile=profile,
        )
        assert config.camera_id == CAMERA_ID
        assert config.rtsp_endpoint == "rtsp://cam1.local:554/live"

    def test_invalid_connection_timeout(self) -> None:
        profile = CameraStreamProfile(
            profile_id="cam1",
            camera_reference="CAM-001",
            resolution_width=1920,
            resolution_height=1080,
        )
        with pytest.raises(ValueError, match="connection_timeout_seconds must be > 0"):
            RtspCameraRuntimeConfig(
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
                stream_id=str(STREAM_ID),
                configuration_version_id=CONFIG_VERSION_ID,
                rtsp_endpoint="rtsp://cam1.local:554/live",
                stream_profile=profile,
                connection_timeout_seconds=0,
            )

    def test_invalid_queue_size(self) -> None:
        profile = CameraStreamProfile(
            profile_id="cam1",
            camera_reference="CAM-001",
            resolution_width=1920,
            resolution_height=1080,
        )
        with pytest.raises(ValueError, match="queue_max_size must be >= 1"):
            RtspCameraRuntimeConfig(
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
                stream_id=str(STREAM_ID),
                configuration_version_id=CONFIG_VERSION_ID,
                rtsp_endpoint="rtsp://cam1.local:554/live",
                stream_profile=profile,
                queue_max_size=0,
            )


class TestRtspCameraConfigResolver:
    """Test the configuration resolver."""

    @pytest.fixture
    def mock_session(self):
        return AsyncMock()

    @pytest.fixture
    def mock_settings(self):
        # Settings requires some env vars; use a mock instead
        settings = MagicMock()
        return settings

    @pytest.fixture
    def resolver(self, mock_settings):
        return RtspCameraConfigResolver(settings=mock_settings)

    @pytest.fixture
    def mock_camera(self):
        camera = MagicMock(spec=CameraModel)
        camera.camera_id = CAMERA_ID
        camera.tenant_id = TENANT_ID
        camera.venue_id = VENUE_ID
        camera.name = "Test Camera"
        camera.status = "active"
        camera.protocol = "rtsp"
        return camera

    @pytest.fixture
    def mock_stream(self):
        stream = MagicMock(spec=VideoStreamModel)
        stream.stream_id = STREAM_ID
        stream.camera_id = CAMERA_ID
        stream.tenant_id = TENANT_ID
        stream.name = "Test Stream"
        stream.status = "active"
        stream.source_url = "rtsp://admin:secret@cam1.local:554/live"
        return stream

    @pytest.fixture
    def mock_camera_config(self):
        config = MagicMock(spec=CameraConfigModel)
        config.config_id = uuid4()
        config.camera_id = CAMERA_ID
        config.tenant_id = TENANT_ID
        config.venue_id = VENUE_ID
        config.status = "active"
        config.version = 1
        config.analysis_enabled = True
        config.frame_rate = Decimal("15.0")
        config.width = 1920
        config.height = 1080
        config.detection_sensitivity = Decimal("0.5")
        config.parameters = {
            "connection_timeout_seconds": 10.0,
            "heartbeat_interval_seconds": 30.0,
            "stale_threshold_seconds": 60.0,
            "reconnect_max_attempts": 5,
            "reconnect_base_delay_seconds": 2.0,
            "reconnect_max_delay_seconds": 60.0,
            "reconnect_jitter": 0.1,
            "queue_max_size": 16,
            "queue_full_policy": "drop_oldest",
        }
        return config

    @pytest.fixture
    def mock_published_version(self):
        version = MagicMock()
        version.configuration_version_id = CONFIG_VERSION_ID
        version.configuration_id = uuid4()
        version.venue_id = VENUE_ID
        version.tenant_id = TENANT_ID
        version.version = 1
        version.status = "published"
        version.cameras = [
            MagicMock(
                camera_id=CAMERA_ID,
                profile_id="cam1",
                camera_reference="CAM-001",
                resolution_width=1920,
                resolution_height=1080,
                fps=Decimal("30.0"),
                codec="h264",
                image_orientation=0,
            )
        ]
        return version

    @pytest.mark.asyncio
    async def test_valid_configuration(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_stream,
        mock_camera_config,
        mock_published_version,
    ):
        """Test successful resolution of valid configuration."""
        # Setup mock session to return our objects
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = mock_stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with patch(
            "backend.app.domain.configuration.service.ConfigurationService"
        ) as mock_config_service:
            mock_service = AsyncMock()
            mock_service.resolve_current_published = AsyncMock(return_value=mock_published_version)
            mock_config_service.return_value = mock_service

            config = await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

            assert config is not None
            assert config.camera_id == CAMERA_ID
            assert config.tenant_id == TENANT_ID
            assert config.venue_id == VENUE_ID
            assert config.configuration_version_id == CONFIG_VERSION_ID
            # Credentials must be redacted
            assert config.rtsp_endpoint == "rtsp://cam1.local:554/live"
            assert "secret" not in config.rtsp_endpoint
            assert "admin" not in config.rtsp_endpoint
            assert config.enabled is True
            assert config.queue_full_policy == QueueFullPolicy.DROP_OLDEST

    @pytest.mark.asyncio
    async def test_missing_endpoint(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_camera_config,
        mock_published_version,
    ):
        """Test missing RTSP endpoint raises error."""
        stream = MagicMock(spec=VideoStreamModel)
        stream.stream_id = STREAM_ID
        stream.camera_id = CAMERA_ID
        stream.tenant_id = TENANT_ID
        stream.name = "Test Stream"
        stream.status = "active"
        stream.source_url = None  # Missing endpoint

        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with patch(
            "backend.app.domain.configuration.service.ConfigurationService"
        ) as mock_config_service:
            mock_service = AsyncMock()
            mock_service.resolve_current_published = AsyncMock(return_value=mock_published_version)
            mock_config_service.return_value = mock_service

            with pytest.raises(MissingEndpointError, match="has no RTSP endpoint"):
                await resolver.resolve(
                    mock_session,
                    camera_id=CAMERA_ID,
                    tenant_id=TENANT_ID,
                    venue_id=VENUE_ID,
                )

    @pytest.mark.asyncio
    async def test_unauthorized_camera_wrong_tenant(
        self,
        resolver,
        mock_session,
    ):
        """Test camera not found for wrong tenant."""
        other_tenant = TenantId(uuid4())
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=camera_result)

        with pytest.raises(CameraNotFoundError, match="not found for tenant"):
            await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=other_tenant,
                venue_id=VENUE_ID,
            )

    @pytest.mark.asyncio
    async def test_unauthorized_camera_wrong_venue(
        self,
        resolver,
        mock_session,
    ):
        """Test camera not found for wrong venue."""
        other_venue = VenueId(uuid4())
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=camera_result)

        with pytest.raises(CameraNotFoundError, match="not found for tenant"):
            await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=other_venue,
            )

    @pytest.mark.asyncio
    async def test_disabled_camera(
        self,
        resolver,
        mock_session,
        mock_camera,
    ):
        """Test disabled camera raises error."""
        mock_camera.status = "inactive"
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        mock_session.execute = AsyncMock(return_value=camera_result)

        with pytest.raises(CameraDisabledError, match="not active"):
            await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

    @pytest.mark.asyncio
    async def test_wrong_protocol(
        self,
        resolver,
        mock_session,
        mock_camera,
    ):
        """Test non-RTSP camera raises error."""
        mock_camera.protocol = "onvif"
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        mock_session.execute = AsyncMock(return_value=camera_result)

        with pytest.raises(CameraProtocolError, match="protocol is onvif, expected RTSP"):
            await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

    @pytest.mark.asyncio
    async def test_no_active_stream(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_camera_config,
    ):
        """Test no active video stream raises error."""
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = None
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with pytest.raises(StreamNotFoundError, match="No active video stream"):
            await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

    @pytest.mark.asyncio
    async def test_no_active_camera_config(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_stream,
    ):
        """Test no active camera configuration raises error."""
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = mock_stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = None
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with pytest.raises(ConfigurationNotFoundError, match="No active configuration"):
            await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

    @pytest.mark.asyncio
    async def test_no_published_configuration(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_stream,
        mock_camera_config,
    ):
        """Test no published configuration raises error."""
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = mock_stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with patch(
            "backend.app.domain.configuration.service.ConfigurationService"
        ) as mock_config_service:
            mock_service = AsyncMock()
            mock_service.resolve_current_published = AsyncMock(return_value=None)  # No published version
            mock_config_service.return_value = mock_service

            with pytest.raises(ConfigurationNotPublishedError, match="No published configuration"):
                await resolver.resolve(
                    mock_session,
                    camera_id=CAMERA_ID,
                    tenant_id=TENANT_ID,
                    venue_id=VENUE_ID,
                )

    @pytest.mark.asyncio
    async def test_camera_not_in_published_config(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_stream,
        mock_camera_config,
    ):
        """Test camera not found in published configuration raises error."""
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = mock_stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        # Published version without this camera
        published_version = MagicMock()
        published_version.configuration_version_id = CONFIG_VERSION_ID
        published_version.cameras = []  # Empty cameras

        with patch(
            "backend.app.domain.configuration.service.ConfigurationService"
        ) as mock_config_service:
            mock_service = AsyncMock()
            mock_service.resolve_current_published = AsyncMock(return_value=published_version)
            mock_config_service.return_value = mock_service

            with pytest.raises(CameraProfileNotFoundError, match="not found in published configuration"):
                await resolver.resolve(
                    mock_session,
                    camera_id=CAMERA_ID,
                    tenant_id=TENANT_ID,
                    venue_id=VENUE_ID,
                )

    @pytest.mark.asyncio
    async def test_credentials_never_in_config(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_stream,
        mock_camera_config,
        mock_published_version,
    ):
        """Verify credentials never appear in resolved config."""
        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = mock_stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with patch(
            "backend.app.domain.configuration.service.ConfigurationService"
        ) as mock_config_service:
            mock_service = AsyncMock()
            mock_service.resolve_current_published = AsyncMock(return_value=mock_published_version)
            mock_config_service.return_value = mock_service

            config = await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

            # Verify no credentials in any field
            assert "admin" not in str(config)
            assert "secret" not in str(config)
            assert "password" not in str(config).lower()
            assert "credential" not in str(config).lower()

            # Verify endpoint is redacted
            assert config.rtsp_endpoint == "rtsp://cam1.local:554/live"

    @pytest.mark.asyncio
    async def test_get_credentials_for_stream(
        self,
        resolver,
        mock_stream,
    ):
        """Test credential extraction for transport use only."""
        username, password = resolver.get_credentials_for_stream(mock_stream)
        assert username == "admin"
        assert password == "secret"

    @pytest.mark.asyncio
    async def test_get_credentials_no_credentials(
        self,
        resolver,
    ):
        """Test credential extraction when none present."""
        stream = MagicMock(spec=VideoStreamModel)
        stream.source_url = "rtsp://cam1.local:554/live"

        username, password = resolver.get_credentials_for_stream(stream)
        assert username is None
        assert password is None

    @pytest.mark.asyncio
    async def test_default_values_used_when_params_missing(
        self,
        resolver,
        mock_session,
        mock_camera,
        mock_stream,
        mock_published_version,
    ):
        """Test defaults used when camera_config.parameters is None."""
        mock_camera_config = MagicMock(spec=CameraConfigModel)
        mock_camera_config.config_id = uuid4()
        mock_camera_config.camera_id = CAMERA_ID
        mock_camera_config.tenant_id = TENANT_ID
        mock_camera_config.venue_id = VENUE_ID
        mock_camera_config.status = "active"
        mock_camera_config.version = 1
        mock_camera_config.analysis_enabled = True
        mock_camera_config.frame_rate = Decimal("15.0")
        mock_camera_config.width = 1920
        mock_camera_config.height = 1080
        mock_camera_config.detection_sensitivity = Decimal("0.5")
        mock_camera_config.parameters = None  # No parameters

        camera_result = MagicMock()
        camera_result.scalar_one_or_none.return_value = mock_camera
        
        stream_result = MagicMock()
        stream_result.scalar_one_or_none.return_value = mock_stream
        
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_camera_config
        
        mock_session.execute = AsyncMock(side_effect=[camera_result, stream_result, config_result])

        with patch(
            "backend.app.domain.configuration.service.ConfigurationService"
        ) as mock_config_service:
            mock_service = AsyncMock()
            mock_service.resolve_current_published = AsyncMock(return_value=mock_published_version)
            mock_config_service.return_value = mock_service

            config = await resolver.resolve(
                mock_session,
                camera_id=CAMERA_ID,
                tenant_id=TENANT_ID,
                venue_id=VENUE_ID,
            )

            # Verify defaults
            assert config.connection_timeout_seconds == 10.0
            assert config.heartbeat_interval_seconds == 30.0
            assert config.stale_threshold_seconds == 60.0
            assert config.reconnect_policy.max_attempts == 5
            assert config.queue_max_size == 16
            assert config.queue_full_policy == QueueFullPolicy.DROP_OLDEST


if __name__ == "__main__":
    pytest.main([__file__, "-v"])