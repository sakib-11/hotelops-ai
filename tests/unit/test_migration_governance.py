"""Unit tests for the offline migration governance gate (Task 6.14).

The gate (scripts/check_migrations.py) runs WITHOUT a database and is the
first CI migration check. These tests verify it passes on the real
repository and that each detection path (broken syntax, multiple heads,
head mismatch, orphan/missing revisions, ordering problems) fails with a
specific report.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_migrations as gate  # ruff: ignore[module-import-not-at-top-of-file]

REAL_VERSIONS = REPO_ROOT / "database" / "migrations" / "versions"


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write migration scripts into a scratch versions/ directory."""
    versions = tmp_path / "versions"
    versions.mkdir()
    for name, body in files.items():
        (versions / name).write_text(body, encoding="utf-8")
    return versions


def _rev(rev: str, down: str | None) -> str:
    """A minimal revision body; `down` is a revision id or None."""
    down_expr = "None" if down is None else f'"{down}"'
    return f'revision = "{rev}"\ndown_revision = {down_expr}\n'


class TestGatePassesOnRepository:
    """The real migration set is valid, single-headed, ordered, complete."""

    def test_real_repo_passes_all_checks(self) -> None:
        problems = gate.run_all(REAL_VERSIONS, gate.EXPECTED_MIGRATION_HEAD)
        assert problems == []

    def test_real_repo_has_single_head_matching_constant(self) -> None:
        sd = gate._script_directory(REAL_VERSIONS)
        assert sd.get_heads() == [gate.EXPECTED_MIGRATION_HEAD]


class TestSyntaxValidity:
    """CI requirement 1: migrations are syntactically valid."""

    def test_syntax_error_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {
                "001_a.py": _rev("001_a", None),
                "002_broken.py": "this is not valid python ::\n",
            },
        )
        problems = gate.run_all(versions, "002_broken")
        assert any("SYNTAX ERROR in 002_broken.py" in p for p in problems)


class TestSingleHead:
    """CI requirement 2: no unexpected multiple heads."""

    def test_multiple_heads_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {
                "001_a.py": _rev("001_a", None),
                "002_b.py": _rev("002_b", "001_a"),
                "002_c.py": _rev("002_c", "001_a"),
            },
        )
        problems = gate.run_all(versions, "002_b")
        assert any("MULTIPLE HEADS" in p for p in problems)


class TestExpectedHead:
    """CI requirement 6: migration head matches expected repository state."""

    def test_head_mismatch_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {"001_a.py": _rev("001_a", None)},
        )
        problems = gate.run_all(versions, "999_zzz")
        assert any("HEAD MISMATCH" in p for p in problems)

    def test_constant_matches_test_files(self) -> None:
        """The gate's constant must not drift from the test-suite constants."""
        import ast

        def extract(path: Path) -> str:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "EXPECTED_MIGRATION_HEAD":
                            return node.value.value  # type: ignore[no-any-return]
            raise AssertionError(f"no EXPECTED_MIGRATION_HEAD in {path}")

        unit_constant = extract(REPO_ROOT / "tests/unit/test_alembic_config.py")
        integration_constant = extract(REPO_ROOT / "tests/integration/test_migrations.py")
        assert gate.EXPECTED_MIGRATION_HEAD == unit_constant == integration_constant


class TestMissingRevisions:
    """Detect missing/orphan/broken revisions."""

    def test_orphan_file_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {
                "001_a.py": _rev("001_a", None),
                "not_a_migration.py": "x = 1\n",
            },
        )
        problems = gate.run_all(versions, "001_a")
        assert any("ORPHAN FILE: not_a_migration.py" in p for p in problems)

    def test_dangling_down_revision_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {
                "001_a.py": _rev("001_a", None),
                "002_b.py": _rev("002_b", "missing_parent"),
            },
        )
        problems = gate.run_all(versions, "002_b")
        assert any("BROKEN REVISION" in p or "MISSING" in p for p in problems)

    def test_import_time_failure_detected(self, tmp_path) -> None:
        """A file that parses but raises at import must be reported, not crash."""
        versions = _write(
            tmp_path,
            {
                "001_a.py": _rev("001_a", None),
                "002_b.py": _rev("002_b", "001_a"),
            },
        )
        # Valid Python, but raises at import time.
        (versions / "002_b.py").write_text(
            'revision = "002_b"\ndown_revision = "001_a"\nraise RuntimeError("boom")\n',
            encoding="utf-8",
        )
        problems = gate.run_all(versions, "002_b")
        assert any("BROKEN REVISION" in p for p in problems)


class TestOrdering:
    """Detect migration ordering problems (NNN_ prefixes must ascend)."""

    def test_out_of_order_prefixes_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {
                "002_b.py": _rev("002_b", None),
                "001_a.py": _rev("001_a", "002_b"),
            },
        )
        problems = gate.run_all(versions, "001_a")
        assert any("ORDERING PROBLEM" in p for p in problems)

    def test_non_numeric_prefix_detected(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {
                "001_a.py": _rev("001_a", None),
                "zz_b.py": _rev("zz_b", "001_a"),
            },
        )
        problems = gate.run_all(versions, "zz_b")
        assert any("ORDERING PROBLEM" in p for p in problems)


class TestMainExitCodes:
    """The CLI returns 0 on pass and 1 on any failure."""

    def test_exit_zero_on_pass(self) -> None:
        assert gate.main(["--versions-dir", str(REAL_VERSIONS)]) == 0

    def test_exit_one_on_failure(self, tmp_path) -> None:
        versions = _write(
            tmp_path,
            {"001_a.py": _rev("001_a", None)},
        )
        assert gate.main(["--versions-dir", str(versions), "--expected-head", "wrong"]) == 1
