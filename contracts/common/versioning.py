"""Canonical schema versioning for HotelOps AI contracts.

Contract version is separate from application version.
Current baseline: 1.0
"""

from __future__ import annotations

SCHEMA_VERSION: str = "1.0"
"""The current canonical contract schema version.

All contracts carry this version. Bump only for breaking changes
according to the compatibility policy documented in ADR-005.
"""

_SUPPORTED_VERSIONS: frozenset[str] = frozenset({"1.0"})


def validate_schema_version(version: str) -> str:
    """Validate that a schema version string is supported.

    Args:
        version: The schema version to validate (e.g. "1.0").

    Returns:
        The same version string if valid.

    Raises:
        ValueError: If the version is not in the supported set.
    """
    if version not in _SUPPORTED_VERSIONS:
        msg = f"Unsupported schema version: {version!r}. Supported: {sorted(_SUPPORTED_VERSIONS)}"
        raise ValueError(msg)
    return version


__all__ = [
    "SCHEMA_VERSION",
    "validate_schema_version",
]
