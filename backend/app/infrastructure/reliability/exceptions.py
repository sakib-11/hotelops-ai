"""Exception taxonomy for the Task 7 reliability layer.

Retryable vs non-retryable failures are distinguished explicitly so the
outbox publisher and inbox consumer can decide between:

  - schedule a retry (bounded exponential backoff → available_at), or
  - move the row straight to DEAD_LETTER (terminal, never deleted).

Default policy: any unexpected exception is RETRYABLE (bounded — it
dead-letters once the retry budget is exhausted). ``NonRetryableError``
is the explicit opt-out for failures that can never succeed on retry
(e.g. a contract-invalid payload, which should never reach the worker
in the first place).
"""

from __future__ import annotations


class ReliabilityError(Exception):
    """Base class for all Task 7 reliability errors."""


class RetryableError(ReliabilityError):
    """A transient failure — the worker should schedule a retry.

    Used to wrap transport-level failures (Redis unreachable, timeouts,
    serialization glitches) so callers can classify without inspecting
    arbitrary exception types.
    """


class NonRetryableError(ReliabilityError):
    """A permanent failure — retrying can never succeed.

    Rows that raise this are moved directly to DEAD_LETTER regardless of
    the remaining retry budget.
    """


class PublishError(RetryableError):
    """Failed to publish an outbox event to the transport (Redis)."""


class InboxReceiveError(RetryableError):
    """Failed to persist an inbound message to the inbox."""


class DuplicateEventError(ReliabilityError):
    """An outbox event with the same event_id already exists.

    Raised by the outbox enqueue path when the unique
    uq_outbox_events_event_id constraint rejects a duplicate event —
    the caller treats this as an idempotent no-op, never an error state.
    """


class IdempotencyConflictError(ReliabilityError):
    """The idempotency key was already used with a DIFFERENT payload.

    Maps to HTTP 409 Conflict semantics at the API boundary: the second
    operation MUST NOT execute.
    """


class IdempotencyInProgressError(ReliabilityError):
    """A concurrent request holds the idempotency lease and did not
    complete within the bounded wait window.

    The caller may retry the request later; the in-progress record is
    still lease-protected and will complete or become reclaimable.
    """


class IdempotencyKeyError(ReliabilityError):
    """An invalid idempotency key (empty, too long, or bad characters)."""
