"""Bootstrap test — proves pytest discovery and repository tooling works.

This test verifies that the Python development environment is correctly
configured. It does not test any HotelOps business functionality.
"""

import sys


def test_python_version() -> None:
    """Verify the Python environment meets minimum requirements."""
    assert sys.version_info >= (3, 14), f"Python 3.14+ required, got {sys.version_info}"


def test_imports() -> None:
    """Verify that required development tools are importable."""
    import mypy  # ruff: ignore[unused-import]
    import pytest  # ruff: ignore[unused-import]

    # ruff is a standalone tool, not importable as a library


def test_bootstrap_placeholder() -> None:
    """Placeholder: replace with real tests when implementing business logic."""
    assert True
