"""Exception taxonomy for the multi-object tracking boundary (Task 13).

Mirrors the project's provider-isolation convention (detectors,
``sources``, ``storage``): downstream business logic depends only on
these types, never on a tracker SDK's error types.

Semantics:

- ``TrackingError`` is the base for every tracking failure.  It is the
  direct analog of ``DetectionError`` in the Task 12 boundary.
- ``TrackOrderError`` is a STRUCTURAL failure: the tracker received
  frames out of order (duplicate frame, frame-index regression, or
  timestamp regression).  The tracker refuses to silently corrupt its
  state.
- ``TrackScopeError`` is a STRUCTURAL failure: an input frame does not
  belong to the tracker's bound session/source — tracker state is
  strictly isolated per session, so cross-session input is rejected.
- ``TrackClassSwitchError`` is a POLICY failure: a track would switch
  object class without the explicit ``allow_class_switch`` policy.
- ``TrackingExecutionError`` is a RUNTIME failure of the tracker
  backend (missing SDK, initialization failure, inference failure,
  malformed backend output).  The backend exception is attached as
  ``cause`` and never crosses the boundary unwrapped.

``NO TRACKS`` (an empty update result) is a VALID outcome and is never
represented as an exception — it is distinct from ``TRACKER FAILURE``
(a ``TrackingExecutionError``).
"""

from __future__ import annotations


class TrackingError(Exception):
    """Base exception for all object-tracking errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class TrackOrderError(TrackingError):
    """Frames arrived out of order (duplicate/regressed index or time)."""


class TrackScopeError(TrackingError):
    """A frame does not belong to this tracker's session/source scope."""


class TrackClassSwitchError(TrackingError):
    """A track would switch object class without the explicit policy."""


class TrackingExecutionError(TrackingError):
    """The tracker backend failed at runtime (typed, never leaked raw)."""


__all__ = [
    "TrackClassSwitchError",
    "TrackOrderError",
    "TrackScopeError",
    "TrackingError",
    "TrackingExecutionError",
]
