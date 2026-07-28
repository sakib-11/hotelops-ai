"""Database infrastructure — PostgreSQL + TimescaleDB connectivity."""

from backend.app.infrastructure.database.client import DatabaseClient

__all__ = ["DatabaseClient"]
