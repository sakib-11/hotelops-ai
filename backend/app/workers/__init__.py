"""Task 7 background workers.

Processes run standalone (``python -m backend.app.workers.outbox_publisher``):

  outbox_publisher — claims durable outbox rows and publishes them to the
                     Redis stream (lease + bounded backoff + dead-letter).
  inbox_ingress    — relays Redis stream messages into the transactional
                     inbox (consumer group + PEL recovery, deduplicated).
  inbox_consumer   — claims inbox rows and runs the registered business
                     effect atomically with the processed transition.
  media_cleanup    — Task 9 retention/expiry sweeps and orphan-object
                     reconciliation (bounded, auditable, idempotent).
  operational_effects — Task 18.11: the slice's effect-handler registry
                     (delivered operational event → durable evidence
                     request via Task 18.9), wired into the existing
                     inbox consumer.

PostgreSQL is the source of truth (ADR-003); Redis is transport only
(ADR-004).
"""

from backend.app.workers.base import PollingWorker
from backend.app.workers.inbox_consumer import EffectHandler, InboxConsumerWorker
from backend.app.workers.inbox_ingress import InboxIngressBridge
from backend.app.workers.media_cleanup import MediaCleanupWorker
from backend.app.workers.operational_effects import (
    OPERATIONAL_EFFECT_TYPES,
    build_operational_effect_handlers,
)
from backend.app.workers.outbox_publisher import OutboxPublisherWorker

__all__ = [
    "OPERATIONAL_EFFECT_TYPES",
    "EffectHandler",
    "InboxConsumerWorker",
    "InboxIngressBridge",
    "MediaCleanupWorker",
    "OutboxPublisherWorker",
    "PollingWorker",
    "build_operational_effect_handlers",
]
