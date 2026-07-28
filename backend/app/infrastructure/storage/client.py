"""Object storage infrastructure boundary.

S3-compatible storage client managed through the application lifespan.
No video upload, evidence upload, signed URLs, or lifecycle rules here.
"""

from __future__ import annotations

import logging
from typing import Self

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from backend.app.infrastructure.config import Settings

logger = logging.getLogger(__name__)


class StorageClient:
    """Object storage infrastructure client.

    Provides connectivity checks against S3-compatible storage.
    Does not provide user-facing storage operations.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: boto3.client | None = None

    async def initialize(self) -> Self:
        """Create the S3 client.

        Call during application startup.
        """
        if self._client is not None:
            logger.warning("Storage client already initialized")
            return self

        # boto3 client creation is synchronous but lightweight
        self._client = boto3.client(
            "s3",
            endpoint_url=self._settings.object_storage_endpoint,
            aws_access_key_id=self._settings.object_storage_access_key,
            aws_secret_access_key=self._settings.object_storage_secret_key,
            region_name=self._settings.object_storage_region,
            config=BotoConfig(
                connect_timeout=5,
                read_timeout=5,
                retries={"max_attempts": 1},
            ),
        )
        logger.info("Storage client initialized")
        return self

    async def check_connectivity(self) -> bool:
        """Verify storage connectivity by checking bucket existence.

        Uses a lightweight HEAD-style operation. Does not upload files.
        """
        if self._client is None:
            return False
        try:
            bucket = self._settings.object_storage_bucket
            self._client.head_bucket(Bucket=bucket)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                logger.warning("Storage bucket '%s' does not exist", bucket)
                return False
            logger.exception("Storage connectivity check failed")
            return False
        except BotoCoreError, EndpointConnectionError:
            logger.exception("Storage connectivity check failed")
            return False

    async def close(self) -> None:
        """Close the storage client.

        Call during application shutdown.
        """
        if self._client is not None:
            self._client.close()
            self._client = None
            logger.info("Storage client closed")
