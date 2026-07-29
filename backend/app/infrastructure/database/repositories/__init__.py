"""Database repositories — scoped data access for HotelOps AI.

Repositories enforce tenant and venue scope at the query level.
All query methods accept an ActorContext and include tenant_id
and/or venue scope in WHERE clauses.
"""

from backend.app.infrastructure.database.repositories.identity import (
    TenantRepository,
    VenueRepository,
)

__all__ = [
    "TenantRepository",
    "VenueRepository",
]
