"""SQLAlchemy declarative base for HotelOps AI ORM models.

All database models inherit from this base to ensure a single
metadata registry for Alembic auto-generation and consistent
table naming conventions.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all HotelOps AI ORM models.

    All models share this single metadata registry for Alembic
    auto-generation and consistent table naming conventions.
    """

    pass
