"""Application-level errors for the operational vertical-slice API (Task 18.12)."""

from __future__ import annotations


class OperationalNotFoundError(Exception):
    """Raised when an operational resource does not exist or is not in scope.

    Mirrors the media/configuration convention: a resource that is
    missing OR outside the actor's tenant/venue scope maps to a single
    404 — out-of-scope and nonexistent are indistinguishable, so
    authorization never leaks which tenant/venue a UUID belongs to.
    """

    def __init__(self, detail: str = "Operational resource not found") -> None:
        self.detail = detail
        super().__init__(detail)
