"""Provider-independent storage exceptions.

These exceptions isolate the domain and application layers from
cloud/vendor-specific SDK error types (e.g. BotoCoreError, ClientError).
"""

from __future__ import annotations


class StorageError(Exception):
    """Base exception for all storage subsystem errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class StorageUnavailableError(StorageError):
    """Raised when the object storage service is unreachable or timed out."""


class ObjectNotFoundError(StorageError):
    """Raised when an operation references a non-existent storage object."""

    def __init__(self, object_key: str, *, cause: Exception | None = None) -> None:
        super().__init__(f"Object not found: '{object_key}'", cause=cause)
        self.object_key = object_key


class ObjectAlreadyExistsError(StorageError):
    """Raised when attempting to create an object that already exists and overwrite is disabled."""

    def __init__(self, object_key: str, *, cause: Exception | None = None) -> None:
        super().__init__(f"Object already exists: '{object_key}'", cause=cause)
        self.object_key = object_key


class StorageIntegrityError(StorageError):
    """Raised when an object's size, checksum, or content verification fails."""

    def __init__(
        self,
        message: str,
        *,
        expected_checksum: str | None = None,
        actual_checksum: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.expected_checksum = expected_checksum
        self.actual_checksum = actual_checksum


class InvalidObjectKeyError(StorageError):
    """Raised when an object key fails path or hierarchy validation rules."""

    def __init__(self, object_key: str, reason: str) -> None:
        super().__init__(f"Invalid object key '{object_key}': {reason}")
        self.object_key = object_key
        self.reason = reason


class StorageOperationError(StorageError):
    """Raised when a storage read, write, or delete operation fails unexpectedly."""


class StorageConfigError(StorageError):
    """Raised when storage configuration is invalid or missing required values."""
