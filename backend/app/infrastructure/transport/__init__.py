"""Redis event transport adapters (ADR-004: Redis is transport, not truth)."""

from backend.app.infrastructure.transport.redis_streams import RedisStreamTransport

__all__ = ["RedisStreamTransport"]
