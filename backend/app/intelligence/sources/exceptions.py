"""Exception taxonomy for the Task 11 ingestion boundary.

Mirrors the project's provider-isolation convention (see
``backend/app/infrastructure/storage/exceptions.py`` and
``backend/app/infrastructure/reliability/exceptions.py``): the frame
source contract and its consumers depend only on these types, never on
decoder/transport/provider SDK error types.

Fatal vs non-fatal semantics:

- ``FrameDecodeError`` is NON-fatal at the frame level — the source
  counts it (``FrameSource.decode_errors``), skips the frame, and
  continues.  A sustained run of consecutive decode failures terminates
  the source (``FrameSource.max_consecutive_decode_errors``).
- ``SourceTerminatedError`` is FATAL — the source reached its terminal
  ``FAILED`` state and will not produce further frames.
- ``SourceNotOpenError`` / ``InvalidStateTransitionError`` protect the
  lifecycle contract (resource ownership semantics).
"""

from __future__ import annotations


class FrameSourceError(Exception):
    """Base exception for all frame source errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class SourceNotOpenError(FrameSourceError):
    """Iteration attempted while the source is not in a frame-producing state.

    Raised when ``__anext__`` is called before ``open()`` or after the
    source was closed/terminated.
    """


class SourceTerminatedError(FrameSourceError):
    """The source reached its terminal FAILED state; no further frames will be produced."""


class FrameDecodeError(FrameSourceError):
    """A single frame could not be decoded from the source stream.

    Non-fatal: the source counts it and skips the frame.  Sustained
    consecutive decode failures terminate the source instead of looping
    forever on a corrupt stream.
    """


class InvalidStateTransitionError(FrameSourceError):
    """A lifecycle operation violated the FrameSource state machine."""


class QueueClosedError(FrameSourceError):
    """An operation was attempted on a shut-down bounded frame queue.

    Raised by ``put`` after shutdown (new production is rejected) and by
    ``get`` once a shut-down queue has been fully drained.
    """


class RtspConnectionError(FrameSourceError):
    """An RTSP transport connection could not be established or was lost.

    Raised by the ``RtspTransport`` adapter for connect/stream failures.
    The source applies its ``ReconnectPolicy`` for mid-stream losses and
    raises ``SourceTerminatedError`` once reconnection is exhausted.
    """


__all__ = [
    "FrameDecodeError",
    "FrameSourceError",
    "InvalidStateTransitionError",
    "QueueClosedError",
    "RtspConnectionError",
    "SourceNotOpenError",
    "SourceTerminatedError",
]
