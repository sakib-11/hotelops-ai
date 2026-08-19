"""Task 7 reliability primitives — backoff policy and error taxonomy."""

from backend.app.infrastructure.reliability.backoff import compute_backoff_delay
from backend.app.infrastructure.reliability.exceptions import (
    DuplicateEventError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyKeyError,
    InboxReceiveError,
    NonRetryableError,
    PublishError,
    ReliabilityError,
    RetryableError,
)

__all__ = [
    "DuplicateEventError",
    "IdempotencyConflictError",
    "IdempotencyInProgressError",
    "IdempotencyKeyError",
    "InboxReceiveError",
    "NonRetryableError",
    "PublishError",
    "ReliabilityError",
    "RetryableError",
    "compute_backoff_delay",
]
