#!/usr/bin/env python3
"""Offline Alembic migration governance gate (Task 6.14).

Runs WITHOUT a database connection. CI executes this on every push/PR to
verify the migration-graph properties that can be checked statically:

  1. every migration script is syntactically valid Python
  2. the migration graph has exactly one head (no unexpected branches)
  3. the head equals the repository's expected head constant
  4. the revision chain is linear (each revision has a unique parent)
  5. no missing revisions (every versions/*.py registers a revision and
     every revision is reachable; no orphan files, no dangling parents)
  6. migration ordering (NNN_ filename prefixes ascend with the chain)

The database-backed checks (empty-DB upgrade, upgrade from the previous
head, `alembic check` drift, RLS, atomicity, rollback/roll-forward) live
in tests/integration/test_migrations.py and run in the CI `migrations`
job against a TimescaleDB service — see .github/workflows/ci.yml.

Migrations are NEVER auto-generated-and-merged: `alembic revision -m`
output (Makefile db-revision) is a draft that still requires human review
per governance doc Section 13 before merge.

Usage:
    python scripts/check_migrations.py [--versions-dir DIR] [--expected-head REV]

Exit code 0 = pass; 1 = at least one check failed (a report is printed).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "database" / "migrations" / "versions"
ALEMBIC_INI = REPO_ROOT / "database" / "alembic.ini"

# Single source of truth for CI. Bump deliberately when adding migrations
# (must match EXPECTED_MIGRATION_HEAD in tests/unit/test_alembic_config.py
# and tests/integration/test_migrations.py).
EXPECTED_MIGRATION_HEAD = "019_temporal_fact_persistence"


def _script_directory(versions_dir: Path) -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(versions_dir.parent))
    return ScriptDirectory.from_config(cfg)


# =============================================================================
# Checks
# =============================================================================


def check_syntax(versions_dir: Path) -> list[str]:
    """Every versions/*.py must be valid Python."""
    problems: list[str] = []
    for path in sorted(versions_dir.glob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"  SYNTAX ERROR in {path.name}: {exc}")
    return problems


def _defines_revision_attr(path: Path) -> bool:
    """True if the module assigns a `revision` attribute (plain or annotated).

    Alembic migration scripts declare `revision: str = "..."` (an
    AnnAssign node) or `revision = "..."` (a plain Assign node); both
    register the file as a revision.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if any(t.id == "revision" for t in targets):
                return True
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "revision"
        ):
            return True
    return False


def check_single_head(sd: ScriptDirectory) -> list[str]:
    try:
        heads = sd.get_heads()
    except CommandError as exc:
        # A branched graph makes get_heads() raise rather than return.
        return [f"  MULTIPLE HEADS (branched graph): {exc}"]
    except KeyError as exc:
        # A down_revision that points at a revision with no file raises
        # KeyError from inside the graph loader.
        return [f"  MISSING REVISION: down_revision {exc} is not present in the graph"]
    except Exception as exc:  # import-time failure in a migration module
        # ScriptDirectory imports modules lazily, so a file that parses but
        # raises at import (NameError, ImportError, runtime error) surfaces
        # here. Report it as a broken revision instead of crashing the gate.
        return [f"  BROKEN REVISION: cannot import a migration module: {exc}"]
    if len(heads) != 1:
        return [f"  MULTIPLE HEADS: expected 1, found {len(heads)}: {sorted(heads)}"]
    return []


def check_expected_head(sd: ScriptDirectory, expected: str) -> list[str]:
    try:
        actual = sd.get_current_head()
    except CommandError:
        # Branched graph — already reported by check_single_head.
        return []
    if actual != expected:
        return [
            f"  HEAD MISMATCH: repository head is {actual!r}, "
            f"expected {expected!r} (update EXPECTED_MIGRATION_HEAD "
            "and the migration itself, or bump the constant)"
        ]
    return []


def check_linear_chain(sd: ScriptDirectory) -> list[str]:
    try:
        revisions = list(sd.walk_revisions())
    except KeyError as exc:
        # Dangling down_revision — surfaced as a missing-revision report.
        return [f"  MISSING REVISION: down_revision {exc} is not present in the graph"]
    downs = [r.down_revision for r in revisions]
    if len(downs) != len(set(downs)):
        return ["  BRANCHING CHAIN: a revision is the parent of more than one migration"]
    if downs[-1] is not None:
        return ["  MISSING BASE: chain does not terminate at a single base revision"]
    bases = sd.get_bases()
    if len(bases) != 1:
        return [f"  MULTIPLE BASES: expected 1, found {len(bases)}: {sorted(bases)}"]
    return []


def check_no_missing_revisions(sd: ScriptDirectory, versions_dir: Path) -> list[str]:
    """Every versions/*.py registers a revision and every revision has a file.

    Detects: a .py dropped into versions/ that is not a revision (orphan
    file), or a revision whose module is missing/dangling.
    """
    problems: list[str] = []
    script_paths = sorted(p.name for p in versions_dir.glob("*.py"))

    # Revisions that appear in the graph but have no file (broken revision).
    for rev in sd.walk_revisions():
        if rev.path is None or not Path(rev.path).name.endswith(".py"):
            problems.append(f"  BROKEN REVISION: {rev.revision!r} has no migration file")

    # Files that never register a revision (orphan / misnamed).
    registered_names = {Path(r.path).name for r in sd.walk_revisions()}
    for name in script_paths:
        path = versions_dir / name
        if not _defines_revision_attr(path):
            problems.append(f"  ORPHAN FILE: {name} does not define a `revision`")
        elif name not in registered_names:
            problems.append(f"  UNREGISTERED REVISION: {name} not found in the graph")

    return problems


def check_ordering(sd: ScriptDirectory) -> list[str]:
    """NNN_ filename prefixes must ascend with the chain (base -> head)."""
    revisions = list(sd.walk_revisions())
    revisions.reverse()  # base -> head
    prefixes: list[int] = []
    for rev in revisions:
        if rev.path is None:
            continue
        name = Path(rev.path).name
        try:
            prefixes.append(int(name.split("_", 1)[0]))
        except ValueError:
            return [
                f"  ORDERING PROBLEM: {name} does not start with an NNN_ prefix "
                "(naming convention Section 4)"
            ]
    if prefixes != sorted(prefixes):
        return [f"  ORDERING PROBLEM: filename prefixes {prefixes} are not ascending"]
    if len(set(prefixes)) != len(prefixes):
        return ["  ORDERING PROBLEM: duplicate NNN_ filename prefixes"]
    return []


# =============================================================================
# Entrypoint
# =============================================================================


def run_all(versions_dir: Path, expected_head: str) -> list[str]:
    problems: list[str] = []
    # Syntax first: a broken file makes Alembic's ScriptDirectory raise,
    # so graph checks can only run once every file parses.
    problems += check_syntax(versions_dir)
    if problems:
        return problems

    # Orphan files (no `revision` attribute) also break ScriptDirectory
    # loading — report them specifically before attempting the graph.
    orphans = [p.name for p in sorted(versions_dir.glob("*.py")) if not _defines_revision_attr(p)]
    if orphans:
        for name in orphans:
            problems.append(f"  ORPHAN FILE: {name} does not define a `revision`")
        return problems

    try:
        sd = _script_directory(versions_dir)
    except CommandError as exc:  # broken revision that parses but fails to load
        problems.append(f"  BROKEN REVISION: cannot load migration graph: {exc}")
        return problems
    except Exception as exc:  # defensive: import-time failure during load
        problems.append(f"  BROKEN REVISION: cannot load migration graph: {exc}")
        return problems

    problems += check_single_head(sd)
    if problems:
        # A branched graph breaks every subsequent graph query — the
        # single-head failure is the actionable signal; stop here.
        return problems
    problems += check_expected_head(sd, expected_head)
    problems += check_linear_chain(sd)
    problems += check_no_missing_revisions(sd, versions_dir)
    problems += check_ordering(sd)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versions-dir",
        type=Path,
        default=VERSIONS_DIR,
        help="Path to the migrations/versions directory (default: repository's)",
    )
    parser.add_argument(
        "--expected-head",
        default=EXPECTED_MIGRATION_HEAD,
        help="Expected migration head (default: repository constant)",
    )
    args = parser.parse_args(argv)

    problems = run_all(args.versions_dir, args.expected_head)

    if problems:
        print("Migration governance gate FAILED:", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print(
        f"Migration governance gate PASSED: "
        f"{len(list(args.versions_dir.glob('*.py')))} migration files, "
        f"single head {args.expected_head}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
