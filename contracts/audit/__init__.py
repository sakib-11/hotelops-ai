"""Canonical audit identity contracts.

Future auditable operations can use AuditEvent to record:
    who (actor_id)
    where (tenant_id, venue_id)
    what (action)
    when (timestamp)
    request context (correlation_id)

Audit identity comes EXCLUSIVELY from trusted ActorContext.
Never trust actor_id from a request body as authoritative.
"""

from contracts.audit.models import AuditActionCategory, AuditEvent

__all__ = [
    "AuditActionCategory",
    "AuditEvent",
]
