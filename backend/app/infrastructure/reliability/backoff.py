"""Reliability primitives for Task 7 workers (outbox/inbox/idempotency).

Bounded exponential backoff with jitter — the retry policy shared by the
outbox publisher and the inbox consumer. The computed delay is persisted
as a row's ``available_at`` by the repository layer, so the backoff is
durable (survives worker restarts) and the poller never computes retry
timing in memory.

Properties (tested in tests/unit/test_reliability_backoff.py):
  - base * 2^(attempt - 1)  — exponential growth per delivery attempt
  - capped at ``maximum``   — bounded, never grows without limit
  - ±``jitter`` factor      — full jitter against thundering herds
  - deterministic for a fixed seed (injectable RNG for tests)
"""

from __future__ import annotations

import random
from datetime import timedelta

# Cap the exponent so absurd attempt counters cannot overflow float.
_MAX_EXPONENT = 32


def compute_backoff_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter: float = 0.0,
    rng: random.Random | None = None,
) -> timedelta:
    """Bounded exponential backoff delay for the given delivery attempt.

    Args:
        attempt: The 1-based delivery attempt number (the first failure
            that schedules a retry passes attempt=1).
        base_seconds: The base delay for the first retry.
        max_seconds: The hard cap on the delay.
        jitter: Optional 0 <= jitter < 1 multiplicative jitter fraction
            applied around the exponential delay.
        rng: Optional seeded RNG for deterministic tests.

    Returns:
        The delay before the next attempt, at least zero.

    Raises:
        ValueError: If attempt < 1, or the bounds are inconsistent.
    """
    if attempt < 1:
        msg = f"attempt must be >= 1, got {attempt}"
        raise ValueError(msg)
    if max_seconds < base_seconds:
        msg = f"max_seconds ({max_seconds}) must be >= base_seconds ({base_seconds})"
        raise ValueError(msg)
    if not 0 <= jitter < 1:
        msg = f"jitter must satisfy 0 <= jitter < 1, got {jitter}"
        raise ValueError(msg)

    exponent = min(attempt - 1, _MAX_EXPONENT)
    delay = min(max_seconds, base_seconds * (2**exponent))
    if jitter > 0:
        rand = rng if rng is not None else random
        factor = 1.0 + rand.uniform(-jitter, jitter)
        delay *= factor
        # Jitter must never push the delay past the hard cap.
        delay = min(max_seconds, delay)
    return timedelta(seconds=max(0.0, delay))
