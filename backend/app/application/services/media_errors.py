"""Media lifecycle service error types (Task 9.8-9.12).

Mapped to HTTP status codes in the API layer:
  MediaNotFoundError  → 404
  MediaConflictError  → 409 (wrong lifecycle state / concurrent race)
  MediaValidationError → 422 (content or integrity verification failed)
  MediaProtectedError  → 403 (preservation/legal hold)
"""

from __future__ import annotations


class MediaNotFoundError(Exception):
    """Raised when a media record does not exist or is not in scope."""

    def __init__(self, detail: str = "Media not found") -> None:
        self.detail = detail
        super().__init__(detail)


class MediaConflictError(Exception):
    """Raised when an operation is invalid for the current lifecycle state."""

    def __init__(
        self, detail: str = "Media operation conflicts with current lifecycle state"
    ) -> None:
        self.detail = detail
        super().__init__(detail)


class MediaValidationError(Exception):
    """Raised when content or integrity validation fails (media → FAILED)."""

    def __init__(self, detail: str = "Media validation failed") -> None:
        self.detail = detail
        super().__init__(detail)


class MediaProtectedError(Exception):
    """Raised when deletion is refused for a preserved/held record."""

    def __init__(
        self, detail: str = "Media is under a preservation hold and cannot be deleted"
    ) -> None:
        self.detail = detail
        super().__init__(detail)
