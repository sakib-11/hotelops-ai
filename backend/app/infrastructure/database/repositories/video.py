"""Video domain repository (Task 6.4, 19.2).

Provides data access for cameras, streams, assets, and sessions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database.models.video import (
    CameraModel,
    VideoAssetModel,
    VideoSessionModel,
    VideoStreamModel,
)
from contracts.common import CameraId, TenantId, VenueId, VideoAssetId, VideoSessionId


class VideoSessionRepository:
    """Repository for VideoSession and related video domain entities."""

    async def create(
        self,
        session: AsyncSession,
        *,
        tenant_id: TenantId,
        venue_id: VenueId,
        source_type: str,
        camera_id: CameraId | None = None,
        asset_id: VideoAssetId | None = None,
        configuration_version_id: uuid.UUID | None = None,
        status: str = "active",
        started_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VideoSessionModel:
        """Create a new video session."""
        model = VideoSessionModel(
            venue_id=venue_id,
            tenant_id=tenant_id,
            source_type=source_type,
            camera_id=camera_id,
            asset_id=asset_id,
            configuration_version_id=configuration_version_id,
            status=status,
            started_at=started_at or datetime.now(UTC),
            metadata_=metadata,
        )
        session.add(model)
        await session.flush()
        return model

    async def get_for_actor(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        session_id: VideoSessionId | uuid.UUID,
    ) -> VideoSessionModel | None:
        """Get a session by ID, scoped to tenant."""
        stmt = select(VideoSessionModel).where(
            VideoSessionModel.session_id == session_id,
            VideoSessionModel.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_camera(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        camera_id: CameraId,
        *,
        status: str | None = None,
    ) -> list[VideoSessionModel]:
        """Get sessions for a camera, optionally filtered by status."""
        stmt = select(VideoSessionModel).where(
            VideoSessionModel.camera_id == camera_id,
            VideoSessionModel.tenant_id == tenant_id,
        )
        if status:
            stmt = stmt.where(VideoSessionModel.status == status)
        stmt = stmt.order_by(VideoSessionModel.started_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_live_sessions(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        venue_id: VenueId | None = None,
    ) -> list[VideoSessionModel]:
        """Get all active live sessions for a tenant (and optionally venue)."""
        stmt = select(VideoSessionModel).where(
            VideoSessionModel.tenant_id == tenant_id,
            VideoSessionModel.source_type == "live",
            VideoSessionModel.status.in_(("connecting", "active", "degraded", "reconnecting")),
        )
        if venue_id:
            stmt = stmt.where(VideoSessionModel.venue_id == venue_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        session: AsyncSession,
        session_id: VideoSessionId | uuid.UUID,
        tenant_id: TenantId,
        new_status: str,
        ended_at: datetime | None = None,
    ) -> bool:
        """Update session status. Returns True if updated."""
        session_model = await self.get_for_actor(session, tenant_id, session_id)
        if session_model is None:
            return False
        session_model.status = new_status
        if ended_at is not None:
            session_model.ended_at = ended_at
        await session.flush()
        return True

    # Camera methods

    async def get_camera(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        camera_id: CameraId,
    ) -> CameraModel | None:
        """Get a camera by ID, scoped to tenant."""
        stmt = select(CameraModel).where(
            CameraModel.camera_id == camera_id,
            CameraModel.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_cameras(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        venue_id: VenueId | None = None,
        *,
        status: str | None = None,
    ) -> list[CameraModel]:
        """List cameras for a tenant/venue."""
        stmt = select(CameraModel).where(CameraModel.tenant_id == tenant_id)
        if venue_id:
            stmt = stmt.where(CameraModel.venue_id == venue_id)
        if status:
            stmt = stmt.where(CameraModel.status == status)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # VideoStream methods

    async def get_stream(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        stream_id: uuid.UUID,
    ) -> VideoStreamModel | None:
        """Get a video stream by ID, scoped to tenant."""
        stmt = select(VideoStreamModel).where(
            VideoStreamModel.stream_id == stream_id,
            VideoStreamModel.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_streams(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        camera_id: CameraId | None = None,
        venue_id: VenueId | None = None,
        *,
        status: str | None = None,
    ) -> list[VideoStreamModel]:
        """List video streams."""
        stmt = select(VideoStreamModel).where(VideoStreamModel.tenant_id == tenant_id)
        if camera_id:
            stmt = stmt.where(VideoStreamModel.camera_id == camera_id)
        if venue_id:
            stmt = stmt.where(VideoStreamModel.venue_id == venue_id)
        if status:
            stmt = stmt.where(VideoStreamModel.status == status)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # VideoAsset methods

    async def get_asset(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        asset_id: VideoAssetId,
    ) -> VideoAssetModel | None:
        """Get a video asset by ID, scoped to tenant."""
        stmt = select(VideoAssetModel).where(
            VideoAssetModel.asset_id == asset_id,
            VideoAssetModel.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_asset(
        self,
        session: AsyncSession,
        *,
        tenant_id: TenantId,
        venue_id: VenueId,
        name: str,
        source_type: str,
        camera_id: CameraId | None = None,
        storage_uri: str | None = None,
        capture_time: datetime | None = None,
        duration_seconds: float | None = None,
        media_metadata: dict[str, Any] | None = None,
    ) -> VideoAssetModel:
        """Create a new video asset."""
        model = VideoAssetModel(
            venue_id=venue_id,
            tenant_id=tenant_id,
            name=name,
            source_type=source_type,
            camera_id=camera_id,
            storage_uri=storage_uri,
            capture_time=capture_time,
            duration_seconds=duration_seconds,
            media_metadata=media_metadata,
        )
        session.add(model)
        await session.flush()
        return model