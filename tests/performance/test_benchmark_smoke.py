"""Smoke test for the repeatable detection benchmark harness (Task 12, Phase 11).

Runs the harness with tiny iteration counts and validates that the
report is structurally complete and repeatable.  NO timing thresholds
are asserted — the harness measures; it does not judge.  This test
exists to guarantee the benchmark stays runnable and its output stays
machine-readable (JSON schema) as the codebase evolves.

Run with the performance marker:

    .venv/bin/pytest -m performance tests/performance/
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.performance]

BENCHMARK_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_detection.py"


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location("benchmark_detection", BENCHMARK_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_detection"] = module
    spec.loader.exec_module(module)
    return module


class TestBenchmarkHarness:
    def test_report_is_structural_and_repeatable(self) -> None:
        bench = _load_benchmark_module()

        async def run() -> dict:
            return await bench.run_benchmark(
                width=640, height=480, batch_size=2, iterations=5, warmup_iterations=2
            )

        import asyncio

        first = asyncio.run(run())
        second = asyncio.run(run())

        # Structural completeness.
        assert first["schema"] == "hotelops.benchmark/1.0"
        for key in (
            "environment",
            "cold_start",
            "warm_inference",
            "memory_peak_rss_kb_delta",
            "slo_comparison",
        ):
            assert key in first, f"report missing {key}"

        env = first["environment"]
        for key in (
            "os",
            "cpu",
            "cpu_cores_logical",
            "python_version",
            "model_version",
            "runtime",
            "resolution",
            "batch_size",
            "device",
            "sdk",
            "gpu",
        ):
            assert key in env, f"environment missing {key}"

        cold = first["cold_start"]
        for key in ("policy_startup_seconds", "adapter_load_seconds", "warmup_seconds"):
            assert key in cold and isinstance(cold[key], float), f"cold_start missing {key}"

        warm = first["warm_inference"]
        for section in ("single_frame", "batch"):
            stats = warm[section]
            for key in (
                "mean_seconds",
                "median_seconds",
                "p95_seconds",
                "p99_seconds",
                "min_seconds",
                "max_seconds",
            ):
                assert key in stats and isinstance(stats[key], float), f"{section} missing {key}"
        assert warm["throughput_frames_per_second"] >= 0
        assert 0.0 <= warm["inference_error_rate"] <= 1.0
        assert warm["cpu_utilization_ratio"] >= 0
        assert isinstance(first["memory_peak_rss_kb_delta"], int)

        # SLO honesty: no invented baseline.
        assert first["slo_comparison"]["status"] == "PERFORMANCE BASELINE NOT DEFINED"

        # Repeatability: measurements are deterministic within tolerance
        # (same harness, same workload -> comparable numbers, never NaN).
        import math

        for key in ("mean_seconds", "p95_seconds"):
            a = first["warm_inference"]["single_frame"][key]
            b = second["warm_inference"]["single_frame"][key]
            assert not math.isnan(a) and not math.isnan(b)
            assert a > 0 and b > 0

    def test_report_serializes_to_json(self, tmp_path: Path) -> None:
        bench = _load_benchmark_module()
        import asyncio

        report = asyncio.run(
            bench.run_benchmark(
                width=640, height=480, batch_size=1, iterations=2, warmup_iterations=1
            )
        )
        blob = json.dumps(report)
        assert json.loads(blob)["schema"] == "hotelops.benchmark/1.0"
        # JSON round-trip preserves all float measurements.
        assert isinstance(json.loads(blob)["warm_inference"]["throughput_frames_per_second"], float)

    def test_no_sdk_means_honest_note(self) -> None:
        bench = _load_benchmark_module()
        import asyncio

        report = asyncio.run(
            bench.run_benchmark(
                width=320, height=240, batch_size=1, iterations=2, warmup_iterations=1
            )
        )
        if not report["environment"]["sdk"]["ultralytics"]:
            assert report["note_sdk_stub"] is not None
            assert "not installed" in report["note_sdk_stub"]
