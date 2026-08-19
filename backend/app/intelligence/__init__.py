"""Intelligence domain (Task 11): video ingestion and the CV boundary.

- ``sources`` — canonical ingestion contract (``FrameSource``), recorded
  (``FileFrameSource``) and live (``RTSPFrameSource``) sources, the
  ``BoundedFrameQueue``, and the ``FrameDecoder``/``RtspTransport``
  provider boundaries.
- ``pipeline`` — the source-agnostic pump (``FramePipeline``) that runs
  ANY ``FrameSource`` through the bounded queue into the downstream
  ``FrameConsumer`` (CV) boundary; live and recorded ingestion are
  indistinguishable downstream by design (ADR-005).
"""

from backend.app.intelligence.pipeline import FrameConsumer, FramePipeline

__all__ = ["FrameConsumer", "FramePipeline"]
