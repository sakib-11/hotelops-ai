#!/usr/bin/env python3
"""Task 18.20 — FIRST VERTICAL SLICE ENTERPRISE GATE RUNNER.

Executes the repository's canonical checks plus the Task 18 vertical-slice
suite and prints the task's FINAL REPORT (one PASS/FAIL line per component
gate). The authoritative per-component verdicts come from the REAL test
suites: the 18.2-18.18 vertical-slice tests (including the 18.20 gate in
tests/unit/test_vertical_slice_gate.py), the contract suite, the security
suite, and the integration suite (skipped without infrastructure).

Checks run:

    unit + contract + security + e2e pytest suites (grouped per component)
    integration pytest suite       (skips cleanly without INTEGRATION_TESTS)
    ruff format --check .          (canonical format gate)
    ruff check .                   (canonical lint gate)
    mypy backend/                  (canonical type gate)
    scripts/check_migrations.py    (Task 6.14 offline migration gate)
    desktop vitest + tsc + prettier (the Task 18.13 Tauri card surface)

Nothing here weakens or re-implements a test: the script only ORCHESTRATES
the existing suites and aggregates their exit codes. Exit code 0 = every
gate PASSED (Task 18 COMPLETE); 1 = at least one gate FAILED (Task 18
BLOCKED — the ISSUES section names the failing component, its root cause
evidence, and the affected task).

Usage:
    .venv/bin/python scripts/task18_gate.py [--no-desktop] [--pytest-args ARGS]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Component → the dedicated test files that prove it (the task's gates).
# ---------------------------------------------------------------------------

UNIT = "tests/unit"
SECURITY_DIR = "tests/security"
CONTRACT_DIR = "tests/contract"
INTEGRATION_DIR = "tests/integration"

GATE_FILE = "tests/unit/test_vertical_slice_gate.py"

# Component → (extra pytest args, test paths). Marker expressions must be
# separate argv items — they can never be embedded inside a path string.
_COMPONENTS: dict[str, tuple[list[str], list[str]]] = {
    "VERTICAL SLICE": ([], [GATE_FILE]),
    "INGESTION": (
        [],
        [
            f"{UNIT}/test_file_frame_source.py",
            f"{UNIT}/test_rtsp_frame_source.py",
            f"{UNIT}/test_frame_source_contract.py",
            f"{UNIT}/test_vertical_slice_fixture.py",
            f"{UNIT}/test_ingestion_failure_paths.py",
            f"{UNIT}/test_pipeline_integration.py",
            f"{UNIT}/test_frame_queue.py",
        ],
    ),
    "YOLO": (
        [],
        [
            f"{UNIT}/test_yolo_adapter.py",
            f"{UNIT}/test_vertical_slice_detection.py",
            f"{UNIT}/test_detector_contract.py",
            f"{UNIT}/test_detection_golden_regression.py",
            f"{UNIT}/test_detection_failure_paths.py",
            f"{UNIT}/test_model_registry.py",
            f"{UNIT}/test_normalization.py",
            f"{UNIT}/test_inference_execution_policy.py",
        ],
    ),
    "TRACKING": (
        [],
        [
            f"{UNIT}/test_tracking.py",
            f"{UNIT}/test_vertical_slice_tracking.py",
        ],
    ),
    "SPATIAL": (
        [],
        [
            f"{UNIT}/test_vertical_slice_spatial.py",
            f"{UNIT}/test_spatial_evaluation.py",
            f"{UNIT}/test_spatial_mapping.py",
            f"{UNIT}/test_geometry_foundation.py",
            f"{UNIT}/test_geometry_contract.py",
            f"{UNIT}/test_line_crossing.py",
        ],
    ),
    "TEMPORAL": (
        [],
        [
            f"{UNIT}/test_vertical_slice_occupancy.py",
            f"{UNIT}/test_occupancy_fsm.py",
            f"{UNIT}/test_dwell_fsm.py",
            f"{UNIT}/test_waiting_fsm.py",
            f"{UNIT}/test_enter_exit_fsm.py",
            f"{UNIT}/test_temporal_foundation.py",
            f"{UNIT}/test_movement_foundation.py",
            f"{UNIT}/test_hysteresis_qualification.py",
            f"{UNIT}/test_movement_classification.py",
        ],
    ),
    "RULE": (
        [],
        [
            f"{UNIT}/test_vertical_slice_rule.py",
            f"{UNIT}/test_rule_engine.py",
            f"{UNIT}/test_rule_registry.py",
            f"{UNIT}/test_rule_versioning.py",
            f"{UNIT}/test_occupancy_session_rule.py",
            f"{UNIT}/test_dwell_threshold_rule.py",
            f"{UNIT}/test_queue_candidate_rule.py",
            f"{UNIT}/test_service_gap_candidate_rule.py",
            f"{UNIT}/test_turnover_delay_rule.py",
            f"{UNIT}/test_data_quality_rule.py",
        ],
    ),
    "DATABASE": (
        [],
        [
            f"{UNIT}/test_database.py",
            f"{UNIT}/test_vertical_slice_persistence.py",
            f"{UNIT}/test_idempotency_model.py",
            f"{UNIT}/test_audit_outbox_inbox_models.py",
            f"{UNIT}/test_migration_governance.py",
            f"{UNIT}/test_alembic_config.py",
        ],
    ),
    "OUTBOX": (
        [],
        [
            f"{UNIT}/test_vertical_slice_outbox.py",
            f"{UNIT}/test_reliability_backoff.py",
        ],
    ),
    "EVIDENCE": (
        [],
        [
            f"{UNIT}/test_vertical_slice_evidence.py",
            f"{UNIT}/test_evidence_worker.py",
            f"{UNIT}/test_evidence_package.py",
            f"{UNIT}/test_evidence_state_machine.py",
            f"{UNIT}/test_evidence_extraction.py",
            f"{UNIT}/test_evidence_models.py",
            f"{UNIT}/test_evidence_retention.py",
            f"{UNIT}/test_evidence_request_builder.py",
            f"{UNIT}/test_evidence_authorization.py",
            f"{UNIT}/test_object_storage_extractor.py",
            f"{UNIT}/test_evidence_observability.py",
        ],
    ),
    "AUTH": (
        [],
        [
            f"{UNIT}/test_auth.py",
            f"{UNIT}/test_rbac.py",
            f"{UNIT}/test_actor_context.py",
            f"{UNIT}/test_scope.py",
            f"{UNIT}/test_websocket_auth.py",
            f"{SECURITY_DIR}/test_adversarial_auth.py",
        ],
    ),
    "API": (
        [],
        [
            f"{UNIT}/test_vertical_slice_api.py",
            f"{UNIT}/test_dependencies.py",
            f"{UNIT}/test_health.py",
        ],
    ),
    "OBSERVABILITY": (
        [],
        [
            f"{UNIT}/test_vertical_slice_telemetry.py",
            f"{UNIT}/test_tracing.py",
            f"{UNIT}/test_logging.py",
            f"{UNIT}/test_observability_middleware.py",
            f"{UNIT}/test_observability_actor_context.py",
            f"{UNIT}/test_observability_verification.py",
        ],
    ),
    "IDEMPOTENCY": (
        [],
        [
            f"{UNIT}/test_vertical_slice_replay.py",
            f"{UNIT}/test_idempotency_service.py",
            f"{UNIT}/test_idempotency_model.py",
        ],
    ),
    "FAILURE RECOVERY": (
        [],
        [
            f"{UNIT}/test_vertical_slice_failures.py",
            f"{SECURITY_DIR}/test_task7_isolation.py",
        ],
    ),
    "PROVENANCE": (
        [],
        [
            f"{UNIT}/test_evidence_provenance_verification.py",
            f"{CONTRACT_DIR}/test_evidence_ref_contract.py",
        ],
    ),
    "SECURITY": ([], [SECURITY_DIR]),
    "UNIT TESTS": (["-m", "unit"], [UNIT]),
    "CONTRACT TESTS": ([], [CONTRACT_DIR]),
    "INTEGRATION TESTS": ([], [INTEGRATION_DIR]),
    "E2E TEST": (["-m", "e2e"], [UNIT]),
    "REGRESSION": ([], [UNIT, CONTRACT_DIR, SECURITY_DIR]),
}

# ---------------------------------------------------------------------------
# Static canonical checks (outside pytest).
# ---------------------------------------------------------------------------

_STATIC_CHECKS: dict[str, list[str]] = {
    "FORMAT CHECK": ["ruff", "format", "--check", "."],
    "LINT CHECK": ["ruff", "check", "."],
    "TYPECHECK": ["mypy", "backend/"],
    "MIGRATION GATE": [sys.executable, "scripts/check_migrations.py"],
}


@dataclass(frozen=True)
class Result:
    """One gate verdict + the evidence tail (the first failing lines)."""

    name: str
    passed: bool
    evidence: str


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run one check, capturing output (unconditioned, no shell)."""
    return subprocess.run(
        argv,
        cwd=str(cwd or REPO_ROOT),
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def _pytest(files: list[str], extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    argv += extra or []
    argv += files
    return _run(argv)


def _evidence(proc: subprocess.CompletedProcess[str], limit: int = 6) -> str:
    """The most relevant output tail for the report (exit code + last lines)."""
    out = proc.stdout.strip().splitlines()
    err = proc.stderr.strip().splitlines()
    tail = (out or err)[-limit:]
    return "\n".join(tail) if tail else f"exit={proc.returncode}"


def run_component_gate(name: str, extra: list[str], files: list[str]) -> Result:
    proc = _pytest(extra, files)
    return Result(name, proc.returncode == 0, _evidence(proc))


def run_static_gate(name: str, argv: list[str]) -> Result:
    proc = _run(argv)
    return Result(name, proc.returncode == 0, _evidence(proc))


def run_desktop_gate() -> list[Result]:
    results: list[Result] = []
    desktop = REPO_ROOT / "desktop"
    for name, argv in (
        ("TAURI (vitest)", ["npm", "test", "--", "--run"]),
        ("TAURI (tsc)", ["npm", "run", "typecheck"]),
        ("TAURI (prettier)", ["npm", "run", "format:check"]),
    ):
        proc = _run(argv, cwd=desktop)
        results.append(Result(name, proc.returncode == 0, _evidence(proc)))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-desktop",
        action="store_true",
        help="Skip the desktop (Tauri) gates — useful on machines without Node",
    )
    args = parser.parse_args(argv)

    results: list[Result] = []

    # Component gates (the task's report lines).
    for name, (extra, files) in _COMPONENTS.items():
        results.append(run_component_gate(name, extra, files))

    # Static canonical checks.
    for name, argv in _STATIC_CHECKS.items():
        results.append(run_static_gate(name, argv))

    # Desktop (Task 18.13 Tauri card) gates.
    if not args.no_desktop:
        results.extend(run_desktop_gate())

    # -----------------------------------------------------------------------
    # FINAL REPORT (the task's format).
    # -----------------------------------------------------------------------
    failed = [r for r in results if not r.passed]
    all_pass = not failed

    print("=" * 64)
    print("TASK 18.20 — FIRST VERTICAL SLICE ENTERPRISE GATE — FINAL REPORT")
    print("=" * 64)
    print(f"TASK 18 STATUS: {'COMPLETE' if all_pass else 'BLOCKED'}")
    print()
    for result in results:
        verdict = "PASS" if result.passed else "FAIL"
        print(f"{result.name:>22}: {verdict}")
    print()
    if failed:
        print("ISSUES:")
        for result in failed:
            print(f"- component: {result.name}")
            print(f"  evidence: {result.evidence.replace(chr(10), ' | ')}")
            print(
                "  correction required: fix the failing component before Task 18 "
                "can be declared COMPLETE"
            )
        print()
        print("Task 18 is BLOCKED until every FAIL above passes.")
        return 1

    print("ISSUES: none — every mandatory gate passes.")
    print()
    print(
        "One fixture flows through the entire architecture; no boundary is "
        "bypassed; all tests pass."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
