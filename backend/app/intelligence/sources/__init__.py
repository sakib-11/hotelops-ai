"""Canonical video ingestion contract (Task 11, Phase 3).

Phase 3 delivers ONLY the contract/interface layer:

- ``FrameSource`` — the abstract ingestion boundary shared by live and
  recorded sources, with an enforced lifecycle state machine
  (CREATED → RUNNING → DRAINING → CLOSED, plus the terminal FAILED
  state), async iteration yielding canonical ``FramePacket`` values,
  monotonic frame indexing, decode-error accounting, and idempotent
  cancellation-safe resource release.
- ``FrameData`` / ``DecodeStatus`` — in-process decoded frame payload
  types used at the ingestion boundary (never serialized).
- ``FrameSourceError`` taxonomy — provider-independent exceptions.

``FileFrameSource`` (recorded video from Task 9 object storage) and
``RTSPFrameSource`` (live, behind the provider-isolated
``RtspTransport`` boundary with a bounded reconnect policy) produce the
SAME canonical ``FramePacket`` semantics through this contract.  The
``BoundedFrameQueue`` carries ``(FramePacket, FrameData)`` pairs
between sources and the CV pipeline with an explicit queue-full policy
(``BLOCK`` or ``DROP_OLDEST``).  The canonical ``FramePacket`` /
``VideoSession`` / ``VideoAsset`` contracts are reused from
``contracts.video`` — nothing here duplicates them.
"""

from backend.app.intelligence.sources.base import (
    DecodeStatus,
    FrameData,
    FrameSource,
    FrameSourceState,
)
from backend.app.intelligence.sources.decoder import DecodedFrame, FrameDecoder
from backend.app.intelligence.sources.exceptions import (
    FrameDecodeError,
    FrameSourceError,
    InvalidStateTransitionError,
    SourceNotOpenError,
    SourceTerminatedError,
)
from backend.app.intelligence.sources.file import FileFrameSource
from backend.app.intelligence.sources.queue import (
    BoundedFrameQueue,
    QueuedFrame,
    QueueFullPolicy,
    QueueStats,
)
from backend.app.intelligence.sources.rtsp import (
    ReconnectPolicy,
    RTSPFrameSource,
    RtspTransport,
    redact_rtsp_url,
)

__all__ = [
    "BoundedFrameQueue",
    "DecodeStatus",
    "DecodedFrame",
    "FileFrameSource",
    "FrameData",
    "FrameDecodeError",
    "FrameDecoder",
    "FrameSource",
    "FrameSourceError",
    "FrameSourceState",
    "InvalidStateTransitionError",
    "QueueFullPolicy",
    "QueueStats",
    "QueuedFrame",
    "RTSPFrameSource",
    "ReconnectPolicy",
    "RtspTransport",
    "SourceNotOpenError",
    "SourceTerminatedError",
    "redact_rtsp_url",
]
