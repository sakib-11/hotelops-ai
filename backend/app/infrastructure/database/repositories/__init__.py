"""Database repositories — scoped data access for HotelOps AI.

Repositories enforce tenant and venue scope at the query level.
All query methods accept an ActorContext and include tenant_id
and/or venue scope in WHERE clauses.
"""

from backend.app.infrastructure.database.repositories.identity import (
    TenantRepository,
    VenueRepository,
)
from backend.app.infrastructure.database.repositories.media import MediaRepository
from backend.app.infrastructure.database.repositories.operational import OperationalRepository
from backend.app.infrastructure.database.repositories.video import VideoSessionRepository

__all__ = [
    "MediaRepository",
    "OperationalRepository",
    "TenantRepository",
    "VenueRepository",
    "VideoSessionRepository",
]
