"""Unit tests for the Task 7 bounded exponential backoff (Phase 8).

Verifies the pure retry-policy function: exponential growth per attempt,
hard cap, optional deterministic jitter, and input validation. The
worker integration tests verify the delay is persisted as available_at.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from backend.app.infrastructure.reliability.backoff import compute_backoff_delay


class TestExponentialGrowth:
    def test_first_attempt_uses_base(self) -> None:
        delay = compute_backoff_delay(1, base_seconds=1.0, max_seconds=300.0)
        assert delay == timedelta(seconds=1.0)

    def test_growth_is_exponential_without_jitter(self) -> None:
        base, maximum = 1.0, 300.0
        expected = {1: 1.0, 2: 2.0, 3: 4.0, 4: 8.0, 5: 16.0}
        for attempt, want in expected.items():
            got = compute_backoff_delay(attempt, base_seconds=base, max_seconds=maximum)
            assert got == timedelta(seconds=want), f"attempt={attempt}"

    def test_delay_is_bounded_by_max(self) -> None:
        base, maximum = 2.0, 10.0
        # Unbounded would be 2^5 = 64s — the cap must win.
        for attempt in (5, 10, 100):
            delay = compute_backoff_delay(attempt, base_seconds=base, max_seconds=maximum)
            assert delay <= timedelta(seconds=maximum)
            assert delay >= timedelta(seconds=maximum) - timedelta(seconds=1e-9)


class TestJitter:
    def test_jitter_within_bounds(self) -> None:
        rng = random.Random(42)
        base, maximum, jitter = 4.0, 300.0, 0.5
        for attempt in (1, 2, 3, 4):
            raw = min(maximum, base * (2 ** (attempt - 1)))
            for _ in range(200):
                delay = compute_backoff_delay(
                    attempt,
                    base_seconds=base,
                    max_seconds=maximum,
                    jitter=jitter,
                    rng=rng,
                )
                assert delay >= timedelta(seconds=raw * (1 - jitter))
                assert delay <= timedelta(seconds=raw * (1 + jitter))
                assert delay >= timedelta(seconds=0)

    def test_jitter_never_exceeds_cap(self) -> None:
        rng = random.Random(7)
        base, maximum, jitter = 60.0, 60.0, 0.9  # raw == cap already
        for _ in range(200):
            delay = compute_backoff_delay(
                5,
                base_seconds=base,
                max_seconds=maximum,
                jitter=jitter,
                rng=rng,
            )
            assert delay <= timedelta(seconds=maximum)

    def test_deterministic_with_seeded_rng(self) -> None:
        base, maximum, jitter = 1.0, 300.0, 0.2
        a = [
            compute_backoff_delay(
                3, base_seconds=base, max_seconds=maximum, jitter=jitter, rng=random.Random(123)
            )
            for _ in range(10)
        ]
        b = [
            compute_backoff_delay(
                3, base_seconds=base, max_seconds=maximum, jitter=jitter, rng=random.Random(123)
            )
            for _ in range(10)
        ]
        assert a == b

    def test_zero_jitter_is_deterministic(self) -> None:
        base, maximum = 1.0, 300.0
        delays = {
            compute_backoff_delay(4, base_seconds=base, max_seconds=maximum, jitter=0.0)
            for _ in range(50)
        }
        assert delays == {timedelta(seconds=8.0)}


class TestValidation:
    def test_attempt_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            compute_backoff_delay(0, base_seconds=1.0, max_seconds=300.0)

    def test_max_below_base_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_seconds"):
            compute_backoff_delay(1, base_seconds=10.0, max_seconds=5.0)

    def test_jitter_out_of_range_rejected(self) -> None:
        for bad in (-0.1, 1.0, 1.5):
            with pytest.raises(ValueError, match="jitter"):
                compute_backoff_delay(1, base_seconds=1.0, max_seconds=300.0, jitter=bad)
s   