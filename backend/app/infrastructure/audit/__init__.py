"""Audit infrastructure for HotelOps AI.

Builds AuditEvent records from trusted ActorContext.
Audit identity is NEVER derived from client-provided data.
"""

from backend.app.infrastructure.audit.context import AuditEventBuilder

__all__ = [
    "AuditEventBuilder",
]
