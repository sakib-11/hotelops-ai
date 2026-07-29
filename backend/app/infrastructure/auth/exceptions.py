"""Authentication exceptions with proper HTTP semantics.

401 UNAUTHENTICATED — missing or invalid credential
403 FORBIDDEN — authenticated but insufficient authorization (future)
"""

from __future__ import annotations


class AuthenticationError(Exception):
    """Raised when authentication fails.

    HTTP mapping: 401 Unauthenticated.
    Includes missing credentials, invalid tokens, expired tokens,
    tampered tokens, unknown users, and disabled users.
    """

    def __init__(self, detail: str = "Not authenticated") -> None:
        self.detail = detail
        super().__init__(detail)


class AuthorizationError(Exception):
    """Raised when authentication succeeds but authorization fails.

    HTTP mapping: 403 Forbidden.
    Reserved for Task 5.5+ RBAC enforcement.
    """

    def __init__(self, detail: str = "Forbidden") -> None:
        self.detail = detail
        super().__init__(detail)
