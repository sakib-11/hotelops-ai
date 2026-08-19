"""Shared pytest configuration for the unit test suite.

Applies the ``unit`` marker to every test collected under this directory
so that ``pytest -m unit`` selects the fast, isolated unit tests as
declared in pyproject.toml (``markers`` + ``strict_markers``).

Note: a module-level ``pytestmark`` here is NOT applied by pytest to
tests in subdirectories, so the marker is attached via the
``pytest_collection_modifyitems`` hook instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Attach the unit marker to every test collected in this directory."""
    root = Path(__file__).resolve().parent
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if root in item_path.parents:
            item.add_marker(pytest.mark.unit)
