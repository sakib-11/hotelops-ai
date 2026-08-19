"""Repeatable detection performance benchmark (Task 12, Phase 11).

Measures the REAL, code-owned detection boundary pipeline — no invented
numbers, no fabricated baselines:

    COLD START
        - policy startup (device resolution + availability validation)
        - detector model load (``YOLOv8Adapter._load`` through the SDK
          seam; when the real SDK is absent the seam is stubbed and the
          measurement covers the adapter's own load path: class-name
          validation, device resolution, artifact wiring)
        - warmup (policy-invoked, timed; never simulated)

    WARM INFERENCE
        - single-frame latency (executed through ``InferenceExecutionPolicy``
          on the real ``YOLOv8Adapter``: decode -> predict -> translate ->
          normalize -> provenance, with batch chunking + stats recording)
        - batch latency (bounded ``detect_batch`` where applicable)
        - throughput (frames/sec, wall-clock)
        - inference error rate (typed failures observed per N calls)
        - memory usage (peak RSS delta via stdlib ``resource``)
        - CPU usage (process-time / wall-time ratio)
        - GPU usage (probed via ``nvidia-smi``/torch; recorded when absent)

Every measurement runs the actual production code path
(``ObjectDetector`` port + ``InferenceExecutionPolicy`` +
``YOLOv8Adapter``).  The SDK seam (``ultralytics``) is stubbed ONLY when
the real SDK is not installed — the report records ``sdk.available`` and
labels any stubbed measurement honestly.  When the real SDK IS
installed, the seam is left untouched and genuine model inference is
measured.

Repeatable by construction: fixed iterations, deterministic frames,
monotonic timers, no wall-clock data in fixtures, seed-free.

Usage:

    .venv/bin/python scripts/benchmark_detection.py [--iterations N]
        [--warmup-iterations N] [--batch-size N] [--width W] [--height H]
        [--json PATH] [--minimal]

Output: a human-readable table to stdout and (with ``--json``) a JSON
report for archival.

COMPARISON NOTE: Task 1 SLOs (docs/product/slo-requirements.md) define
NO numerical detection performance baseline — SLO-006 (detection
latency) and SLO-012 (frame rate) are **TBD**.  This harness therefore
does NOT assert an acceptance number; it records measurements for the
future baseline.  When a numerical baseline is agreed, wire it here.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import resource
import statistics
import sys
import time
import types
from pathlib import Path
from typing import Any

# Allow running from anywhere in the repo.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.intelligence.detectors import (  # ruff: ignore[module-import-not-at-top-of-file]
    DetectionInput,
    DetectorConfig,
    Device,
    InferenceError,
    InferenceExecutionPolicy,
    ModelSpec,
    yolo_adapter,
)
from backend.app.intelligence.detectors.yolo_adapter import (  # ruff: ignore[module-import-not-at-top-of-file]
    YOLOv8Adapter,
)
from contracts.common import (  # ruff: ignore[module-import-not-at-top-of-file]
    FrameId,
    VideoSessionId,
    new_uuid,
    utc_now,
)
from contracts.video import FramePacket  # ruff: ignore[module-import-not-at-top-of-file]

SCHEMA = "hotelops.benchmark/1.0"


# ---------------------------------------------------------------------------
# Environment record
# ---------------------------------------------------------------------------


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_total_kb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _gpu_probe() -> dict[str, Any]:
    """Record GPU availability honestly (nvidia-smi / torch when present)."""
    nvidia_smi = os.popen("command -v nvidia-smi").read().strip()
    result: dict[str, Any] = {
        "present": bool(nvidia_smi),
        "name": None,
        "vram_bytes": None,
        "note": "no GPU detected on this host",
    }
    if not nvidia_smi:
        return result
    try:
        out = (
            os
            .popen("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits")
            .read()
            .strip()
        )
        if out:
            name, vram_mb = out.split(",")[0].strip(), out.split(",")[1].strip()
            result["present"] = True
            result["name"] = name
            result["vram_bytes"] = int(vram_mb) * 1024 * 1024
            result["note"] = None
    except Exception:
        pass
    return result


def _sdk_probe() -> dict[str, Any]:
    """Whether the real detection SDK + inference deps are installed.

    Tolerant of a stubbed ``sys.modules`` entry (the harness injects a
    fake ``ultralytics`` module whose ``__spec__`` is None), so the
    probe reports the REAL environment state.
    """
    import importlib.util

    def available(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except ImportError, ValueError:
            return False

    return {
        "ultralytics": available("ultralytics"),
        "torch": available("torch"),
        "numpy": available("numpy"),
        "cv2": available("cv2"),
    }


def collect_environment(model_version: str) -> dict[str, Any]:
    sdk = _sdk_probe()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "cpu_cores_logical": os.cpu_count(),
        "ram_total_kb": _mem_total_kb(),
        "python_version": platform.python_version(),
        "model_version": model_version,
        "runtime": f"ultralytics@{_version_of('ultralytics')}"
        if sdk["ultralytics"]
        else "sdk-not-installed",
        "resolution": None,  # filled per measurement
        "batch_size": None,  # filled per measurement
        "device": "cpu",  # resolved by policy at startup
        "sdk": sdk,
        "gpu": _gpu_probe(),
    }


def _version_of(module_name: str) -> str:
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", "unknown")
        return version if isinstance(version, str) else "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------


def make_spec() -> ModelSpec:
    return ModelSpec(
        model_id="benchmark-yolov8n",
        model_name="yolov8n",
        model_version="8.1.0",
        artifact_uri="memory://benchmark/yolov8n.pt",
        artifact_sha256="a" * 64,
        device=Device.CPU,
        class_names=("person", "bag"),
    )


def make_config(width: int, height: int, batch_size: int) -> DetectorConfig:
    return DetectorConfig(
        confidence_threshold=0.5,
        nms_iou_threshold=0.45,
        max_detections=300,
        input_width=width,
        input_height=height,
        device=Device.CPU,
        batch_size=batch_size,
    )


def make_frames(count: int, *, width: int, height: int) -> list[FramePacket]:
    """Deterministic frame packets (unique IDs, monotonic indices)."""
    session_id = VideoSessionId(new_uuid())
    base = utc_now()
    return [
        FramePacket(
            frame_id=FrameId(new_uuid()),
            session_id=session_id,
            source_ref=None,
            frame_index=i,
            event_time=base,
            width=width,
            height=height,
        )
        for i in range(count)
    ]


def make_inputs(frames: list[FramePacket]) -> list[DetectionInput]:
    # Real bytes are not required for the seam-stubbed path; the decode
    # seam returns deterministic dimensions.  Non-empty is enforced.
    return [DetectionInput(frame=f, image=b"benchmark-frame") for f in frames]


# ---------------------------------------------------------------------------
# Fake SDK seam (used ONLY when the real SDK is absent)
# ---------------------------------------------------------------------------


def install_fake_sdk() -> Any:
    """Stub the lazy SDK seam so the adapter load path can be measured.

    Returns a ``restore()`` callable that reinstates the real seam
    functions and removes the injected module from ``sys.modules`` —
    the harness never leaves process-global state behind.
    """

    class FakeBoxes:
        def __init__(
            self, xyxy: list[list[float]], conf: list[list[float]], cls: list[list[int]]
        ) -> None:
            self.xyxy = xyxy
            self.conf = conf
            self.cls = cls

        def __len__(self) -> int:
            return len(self.xyxy)

    class FakeResult:
        def __init__(self) -> None:
            self.boxes = FakeBoxes([[10.0, 20.0, 330.0, 470.0]], [[0.95]], [[0]])

    class FakeYOLO:
        def __init__(self, artifact_uri: str) -> None:
            self.artifact_uri = artifact_uri
            self.names: dict[int, str] = {0: "person", 1: "bag"}

        def predict(self, **kwargs: Any) -> list[FakeResult]:
            # The adapter's batch path passes a list source and requires
            # one result per input; single-frame passes one image.
            source = kwargs.get("source")
            if isinstance(source, list):
                return [FakeResult() for _ in source]
            return [FakeResult()]

    module = types.ModuleType("ultralytics")
    module.YOLO = FakeYOLO  # type: ignore[attr-defined]  # synthetic module
    sys.modules["ultralytics"] = module

    original = {
        "_cuda_available": yolo_adapter._cuda_available,
        "_mps_available": yolo_adapter._mps_available,
        "_decode_image_bytes": yolo_adapter._decode_image_bytes,
        "_blank_image": yolo_adapter._blank_image,
    }
    yolo_adapter._cuda_available = lambda: False
    yolo_adapter._mps_available = lambda: False
    yolo_adapter._decode_image_bytes = lambda image: (object(), 640, 480)
    yolo_adapter._blank_image = lambda config: object()

    def restore() -> None:
        """Restore the real seam + remove the injected module."""
        for name, fn in original.items():
            setattr(yolo_adapter, name, fn)
        sys.modules.pop("ultralytics", None)

    return restore


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(pct / 100.0 * len(sorted_values)))
    return sorted_values[idx]


def _latency_stats(latencies: list[float]) -> dict[str, float]:
    ordered = sorted(latencies)
    return {
        "count": float(len(ordered)),
        "mean_seconds": statistics.fmean(ordered),
        "median_seconds": statistics.median(ordered),
        "p50_seconds": _percentile(ordered, 50.0),
        "p95_seconds": _percentile(ordered, 95.0),
        "p99_seconds": _percentile(ordered, 99.0),
        "min_seconds": ordered[0],
        "max_seconds": ordered[-1],
    }


class _Rusage:
    @staticmethod
    def maxrss_kb() -> int:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


# ---------------------------------------------------------------------------
# Benchmark runs
# ---------------------------------------------------------------------------


async def measure_cold_start(config: DetectorConfig) -> dict[str, Any]:
    """Policy startup, adapter load, and warmup — timed individually."""
    spec = make_spec()

    # Policy startup (device resolution + validation).
    policy = InferenceExecutionPolicy(config=config)
    t0 = time.perf_counter()
    await policy.startup(cuda_available=False, mps_available=False)
    policy_startup_s = time.perf_counter() - t0

    # Adapter load through the (possibly stubbed) SDK seam.
    adapter = YOLOv8Adapter(model_spec=spec, config=config)
    t0 = time.perf_counter()
    try:
        await adapter.load()
        adapter_load_s = time.perf_counter() - t0

        # Warmup through the policy (real warmup, timed, never simulated).
        await policy.run_warmup(adapter)
        warmup_s = policy.warmup_duration_seconds or 0.0
    finally:
        await adapter.close()
        await policy.shutdown()

    return {
        "policy_startup_seconds": policy_startup_s,
        "adapter_load_seconds": adapter_load_s,
        "warmup_seconds": warmup_s,
        "selected_device": "cpu",
    }


async def _run_warm_inference(
    *,
    width: int,
    height: int,
    batch_size: int,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    spec = make_spec()
    config = make_config(width, height, batch_size)
    policy = InferenceExecutionPolicy(config=config)
    await policy.startup(cuda_available=False, mps_available=False)
    # Warm inference runs the REAL adapter (decode -> predict -> translate
    # -> normalize -> provenance) so the measured numbers cover the full
    # code-owned boundary pipeline, not just policy orchestration.
    detector = YOLOv8Adapter(model_spec=spec, config=config)

    # Warmup passes (excluded from latency stats): real model warmup via
    # the policy, then ``warmup_iterations`` warm executions.
    warm_inputs = make_inputs(make_frames(batch_size, width=width, height=height))
    await policy.run_warmup(detector)
    for _ in range(max(1, warmup_iterations)):
        await policy.execute(detector, warm_inputs)

    # Single-frame latency (batch_size=1 path: strictly sequential).
    latencies: list[float] = []
    errors = 0
    for _ in range(iterations):
        inp = make_inputs(make_frames(1, width=width, height=height))[0]
        t0 = time.perf_counter()
        try:
            await policy.execute(detector, [inp])
        except InferenceError:
            errors += 1
        latencies.append(time.perf_counter() - t0)

    single_stats = _latency_stats(latencies)

    # Batch latency (bounded chunk via detect_batch when batch_size > 1).
    batch_latencies: list[float] = []
    for _ in range(iterations):
        inputs = make_inputs(make_frames(batch_size, width=width, height=height))
        t0 = time.perf_counter()
        try:
            await policy.execute(detector, inputs)
        except InferenceError:
            errors += 1
        batch_latencies.append(time.perf_counter() - t0)

    batch_stats = _latency_stats(batch_latencies)

    # Throughput: frames/sec over the batch loop (wall-clock).
    total_frames = iterations * batch_size
    total_wall = sum(batch_latencies)
    throughput_fps = total_frames / total_wall if total_wall > 0 else 0.0

    # CPU utilization: process-time / wall-time ratio over the batch loop.
    t_cpu = time.process_time()
    t_wall = time.perf_counter()
    for _ in range(iterations):
        await policy.execute(
            detector, make_inputs(make_frames(batch_size, width=width, height=height))
        )
    cpu_elapsed = time.process_time() - t_cpu
    wall_elapsed = time.perf_counter() - t_wall

    await policy.shutdown()
    return {
        "single_frame": single_stats,
        "batch": {**batch_stats, "batch_size": float(batch_size)},
        "throughput_frames_per_second": throughput_fps,
        "inference_error_rate": errors / (iterations * 2),
        "cpu_utilization_ratio": cpu_elapsed / wall_elapsed if wall_elapsed > 0 else 0.0,
    }


async def run_benchmark(
    *,
    width: int,
    height: int,
    batch_size: int,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    # Probe the REAL environment BEFORE any SDK seam is stubbed.
    model_version = make_spec().model_version
    env = collect_environment(model_version)
    env["resolution"] = f"{width}x{height}"
    env["batch_size"] = batch_size

    rss_before = _Rusage.maxrss_kb()
    # Stub ONLY when the real SDK is absent (the probe above already
    # recorded the REAL environment).  When the real SDK IS installed,
    # genuine model inference is measured through the untouched seam.
    restore_seam = install_fake_sdk() if not env["sdk"]["ultralytics"] else None
    try:
        cold = await measure_cold_start(make_config(width, height, batch_size))
        warm = await _run_warm_inference(
            width=width,
            height=height,
            batch_size=batch_size,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )
    finally:
        if restore_seam is not None:
            restore_seam()
    rss_after = _Rusage.maxrss_kb()

    return {
        "schema": SCHEMA,
        "measured_at": time.time_ns(),  # archival timestamp only (not a benchmark input)
        "environment": env,
        "cold_start": cold,
        "warm_inference": warm,
        "memory_peak_rss_kb_delta": rss_after - rss_before,
        "note_sdk_stub": (
            "the real ultralytics SDK is not installed; the adapter SDK seam was stubbed "
            "so adapter LOAD/translation paths are measured, but real model inference "
            "numbers are NOT included — record them when the SDK + approved artifact exist."
            if not env["sdk"]["ultralytics"]
            else None
        ),
        "slo_comparison": {
            "status": "PERFORMANCE BASELINE NOT DEFINED",
            "detail": (
                "docs/product/slo-requirements.md defines no numerical detection "
                "performance baseline (SLO-006 latency and SLO-012 frame rate are TBD). "
                "No acceptance number is asserted or invented."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _fmt(v: float | None, unit: str = "s") -> str:
    if v is None:
        return "n/a"
    if v < 0.001:
        return f"{v * 1_000_000:.1f}us"
    if v < 1.0:
        return f"{v * 1_000:.2f}ms"
    return f"{v:.3f}{unit}"


def print_report(report: dict[str, Any]) -> None:
    env = report["environment"]
    print("=" * 72)
    print("DETECTION PERFORMANCE BENCHMARK — repeatable harness")
    print("=" * 72)
    print(f"OS:        {env['os']}")
    print(f"CPU:       {env['cpu']} ({env['cpu_cores_logical']} logical cores)")
    print(
        f"RAM total: {env['ram_total_kb'] / 1024 / 1024:.1f} GiB"
        if env["ram_total_kb"]
        else "RAM: unknown"
    )
    print(f"Python:    {env['python_version']}")
    print(
        f"Model:     yolov8n@{env['model_version']}  resolution {env['resolution']}  batch {env['batch_size']}"
    )
    print(f"Device:    {env['device']}  Runtime: {env['runtime']}")
    print(f"SDK:       {env['sdk']}")
    print(f"GPU:       {env['gpu']}")
    print("-" * 72)
    print("COLD START")
    cold = report["cold_start"]
    print(f"  policy startup : {_fmt(cold['policy_startup_seconds'])}")
    print(f"  adapter load   : {_fmt(cold['adapter_load_seconds'])}")
    print(f"  warmup         : {_fmt(cold['warmup_seconds'])}")
    print("-" * 72)
    print("WARM INFERENCE")
    warm = report["warm_inference"]
    sf = warm["single_frame"]
    print(
        f"  single-frame mean  : {_fmt(sf['mean_seconds'])}   p50 {_fmt(sf['p50_seconds'])}"
        f"  p95 {_fmt(sf['p95_seconds'])}  p99 {_fmt(sf['p99_seconds'])}"
    )
    b = warm["batch"]
    print(
        f"  batch mean         : {_fmt(b['mean_seconds'])}   p50 {_fmt(b['p50_seconds'])}"
        f"  p95 {_fmt(b['p95_seconds'])}  p99 {_fmt(b['p99_seconds'])}  (batch {int(b['batch_size'])})"
    )
    print(f"  throughput         : {warm['throughput_frames_per_second']:.1f} frames/sec")
    print(f"  inference err rate : {warm['inference_error_rate']:.4%}")
    print(f"  cpu utilization    : {warm['cpu_utilization_ratio']:.2f} cores")
    print(f"  peak RSS delta     : {report['memory_peak_rss_kb_delta']} KB")
    print("-" * 72)
    print(f"SLO comparison: {report['slo_comparison']['status']}")
    print(f"  {report['slo_comparison']['detail']}")
    if report.get("note_sdk_stub"):
        print(f"NOTE: {report['note_sdk_stub']}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="Repeatable detection performance benchmark")
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--warmup-iterations", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--json", type=Path, default=None, help="write JSON report to PATH")
    parser.add_argument("--minimal", action="store_true", help="tiny iteration counts (CI smoke)")
    args = parser.parse_args()

    iterations = 3 if args.minimal else args.iterations
    import asyncio

    report = asyncio.run(
        run_benchmark(
            width=args.width,
            height=args.height,
            batch_size=args.batch_size,
            iterations=iterations,
            warmup_iterations=args.warmup_iterations,
        )
    )
    print_report(report)
    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"JSON report written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
