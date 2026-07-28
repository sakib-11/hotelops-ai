"""Unit tests for StorageClient.

These tests use mocks/stubs, not a real S3/MinIO instance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.storage.client import StorageClient


class TestStorageClient:
    """Tests for StorageClient."""

    @pytest.mark.asyncio
    async def test_initialize_and_close(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = StorageClient(settings)

        assert client._client is None

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3

            await client.initialize()
            assert client._client is not None
            mock_boto.assert_called_once()

            await client.close()
            mock_s3.close.assert_called_once()
            assert client._client is None

    @pytest.mark.asyncio
    async def test_check_connectivity_when_not_initialized(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = StorageClient(settings)

        result = await client.check_connectivity()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_connectivity_success(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = StorageClient(settings)

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3

            await client.initialize()
            result = await client.check_connectivity()
            assert result is True
            mock_s3.head_bucket.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_connectivity_bucket_not_found(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = StorageClient(settings)

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
            mock_s3.head_bucket.side_effect = ClientError(error_response, "HeadBucket")
            mock_boto.return_value = mock_s3

            await client.initialize()
            result = await client.check_connectivity()
            assert result is False

    @pytest.mark.asyncio
    async def test_check_connectivity_endpoint_unreachable(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = StorageClient(settings)

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_s3.head_bucket.side_effect = EndpointConnectionError(
                endpoint_url="http://localhost:9000",
            )
            mock_boto.return_value = mock_s3

            await client.initialize()
            result = await client.check_connectivity()
            assert result is False
