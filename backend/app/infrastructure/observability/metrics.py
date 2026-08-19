"""Prometheus metrics (Task 8.x, verified in Task 8.11).

One metrics configuration, owned by this module:

  - :func:`configure_metrics` is OPT-IN: with
    ``OBSERVABILITY_METRICS_ENABLED=false`` (the default) no metrics are
    registered, the middleware is a safe no-op, and no external server
    is required to start (consistent with the tracing module).
  - When enabled, a dedicated ``CollectorRegistry`` is created and HTTP
    request counters/histograms are registered. A ``/metrics`` endpoint
    serves the exposition format; nothing is exported elsewhere.
  - :func:`record_request` is called by the metrics middleware on every
    HTTP request (no-op while disabled). Only bounded labels are used
    (method + status code) — no high-cardinality attributes.

This is the single metrics mechanism in the application.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from backend.app.infrastructure.config import Settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised through the enabled path
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Histogram,
        generate_latest,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False

_enabled = False
_registry: Any = None
_http_requests: Any = None
_http_request_duration: Any = None
_evidence_metrics: dict[str, Any] = {}
_pipeline_metrics: dict[str, Any] = {}

# Task 17.12 — evidence pipeline counters (bounded, no high-cardinality
# labels). These are the ONLY evidence metrics; every firing point in the
# evidence layer records through ``record_evidence_metric``.
EVIDENCE_METRIC_REQUESTED = "evidence_requested"
EVIDENCE_METRIC_EXTRACTION_SUCCESS = "evidence_extraction_success"
EVIDENCE_METRIC_EXTRACTION_FAILURE = "evidence_extraction_failure"
EVIDENCE_METRIC_UPLOAD_SUCCESS = "evidence_upload_success"
EVIDENCE_METRIC_UPLOAD_FAILURE = "evidence_upload_failure"
EVIDENCE_METRIC_FINALIZED = "evidence_finalized"
EVIDENCE_METRIC_RETRY = "evidence_retry"
EVIDENCE_METRIC_EXPIRED = "evidence_expired"

# Task 18.18 — vertical-slice pipeline stage counters. These are the ONLY
# pipeline metrics; every stage records through ``record_pipeline_metric``
# at its real firing point (ingestion → detection → tracking → occupancy
# FSM → persistence → outbox → worker effect). Bounded, no labels.
PIPELINE_METRIC_FRAMES = "pipeline_frames"
PIPELINE_METRIC_DETECTIONS = "pipeline_detections"
PIPELINE_METRIC_TRACKS = "pipeline_tracks"
PIPELINE_METRIC_OCCUPANCY_EVENTS = "pipeline_occupancy_events"
PIPELINE_METRIC_PERSISTENCE = "pipeline_persistence"
PIPELINE_METRIC_OUTBOX = "pipeline_outbox"
PIPELINE_METRIC_WORKER = "pipeline_worker"


def configure_metrics(settings: Settings) -> bool:
    """Enable Prometheus metrics (idempotent, opt-in).

    Returns:
        True when metrics are enabled after this call.
    """
    global _enabled, _registry, _http_requests, _http_request_duration
    if _enabled:
        return True
    if not settings.observability_metrics_enabled or not _PROMETHEUS_AVAILABLE:
        _enabled = False
        return False
    _registry = CollectorRegistry()
    _http_requests = Counter(
        "http_requests_total",
        "Total HTTP requests handled, by method and response status",
        ["method", "status"],
        registry=_registry,
    )
    _http_request_duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["method"],
        registry=_registry,
    )
    _evidence_metrics.clear()
    for name in (
        EVIDENCE_METRIC_REQUESTED,
        EVIDENCE_METRIC_EXTRACTION_SUCCESS,
        EVIDENCE_METRIC_EXTRACTION_FAILURE,
        EVIDENCE_METRIC_UPLOAD_SUCCESS,
        EVIDENCE_METRIC_UPLOAD_FAILURE,
        EVIDENCE_METRIC_FINALIZED,
        EVIDENCE_METRIC_RETRY,
        EVIDENCE_METRIC_EXPIRED,
    ):
        _evidence_metrics[name] = Counter(
            name,
            f"Evidence pipeline counter: {name.replace('_', ' ')}",
            registry=_registry,
        )
    # Task 18.18 — vertical-slice pipeline stage counters.
    _pipeline_metrics.clear()
    for name in (
        PIPELINE_METRIC_FRAMES,
        PIPELINE_METRIC_DETECTIONS,
        PIPELINE_METRIC_TRACKS,
        PIPELINE_METRIC_OCCUPANCY_EVENTS,
        PIPELINE_METRIC_PERSISTENCE,
        PIPELINE_METRIC_OUTBOX,
        PIPELINE_METRIC_WORKER,
    ):
        _pipeline_metrics[name] = Counter(
            name,
            f"Vertical-slice pipeline counter: {name.replace('_', ' ')}",
            registry=_registry,
        )
    _enabled = True
    logger.info("Prometheus metrics enabled")
    return True


def record_evidence_metric(name: str) -> None:
    """Increment one evidence pipeline counter (no-op while disabled).

    The metric name must be one of the registered ``EVIDENCE_METRIC_*``
    constants — an unknown name is a programming error (a typo would
    otherwise silently drop telemetry), so it raises.

    Raises:
        ValueError: the name is not a registered evidence metric.
    """
    if not _enabled:
        return
    counter = _evidence_metrics.get(name)
    if counter is None:
        msg = f"unknown evidence metric: {name!r} (must be an EVIDENCE_METRIC_* constant)"
        raise ValueError(msg)
    counter.inc()


def enabled() -> bool:
    """True when metrics are configured and recording."""
    return _enabled


def record_pipeline_metric(name: str, amount: int = 1) -> None:
    """Increment one vertical-slice pipeline stage counter (no-op when disabled).

    The metric name must be one of the registered ``PIPELINE_METRIC_*``
    constants — an unknown name is a programming error (a typo would
    otherwise silently drop telemetry), so it raises. ``amount`` lets a
    stage record its observation count (e.g. detections per frame).

    Raises:
        ValueError: the name is not a registered pipeline metric.
    """
    if not _enabled:
        return
    counter = _pipeline_metrics.get(name)
    if counter is None:
        msg = f"unknown pipeline metric: {name!r} (must be a PIPELINE_METRIC_* constant)"
        raise ValueError(msg)
    counter.inc(amount)


def record_request(method: str, status: int, duration: float) -> None:
    """Record one HTTP request (no-op while disabled)."""
    if not _enabled:
        return
    method = method or "UNKNOWN"
    _http_requests.labels(method=method, status=str(status)).inc()
    _http_request_duration.labels(method=method).observe(duration)


def render() -> tuple[bytes, str]:
    """The Prometheus exposition body + content type for ``/metrics``.

    Returns empty bytes when metrics are disabled (the endpoint returns
    404 in that case — see the router).
    """
    if not _enabled:
        return b"", CONTENT_TYPE_LATEST
    return generate_latest(_registry), CONTENT_TYPE_LATEST


_METRICS_PATH = "/metrics"


class MetricsMiddleware:
    """Records a counter + histogram for every HTTP request.

    A safe no-op when metrics are disabled. Kept as a separate middleware
    (owning one responsibility), added after the request-context
    middleware so it measures the full request lifecycle. The ``/metrics``
    scrape itself is excluded so a scrape never inflates its own output.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if str(scope.get("path") or "") == _METRICS_PATH:
            # Do not record the scrape of /metrics itself.
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method") or "UNKNOWN")
        start = time.perf_counter()
        status: dict[str, int] = {"value": 0}

        async def send_with_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["value"] = int(message.get("status") or 0)
            await send(message)

        try:
            await self._app(scope, receive, send_with_status)
        finally:
            record_request(method, status["value"], time.perf_counter() - start)


__all__ = [
    "EVIDENCE_METRIC_EXPIRED",
    "EVIDENCE_METRIC_EXTRACTION_FAILURE",
    "EVIDENCE_METRIC_EXTRACTION_SUCCESS",
    "EVIDENCE_METRIC_FINALIZED",
    "EVIDENCE_METRIC_REQUESTED",
    "EVIDENCE_METRIC_RETRY",
    "EVIDENCE_METRIC_UPLOAD_FAILURE",
    "EVIDENCE_METRIC_UPLOAD_SUCCESS",
    "PIPELINE_METRIC_DETECTIONS",
    "PIPELINE_METRIC_FRAMES",
    "PIPELINE_METRIC_OCCUPANCY_EVENTS",
    "PIPELINE_METRIC_OUTBOX",
    "PIPELINE_METRIC_PERSISTENCE",
    "PIPELINE_METRIC_TRACKS",
    "PIPELINE_METRIC_WORKER",
    "MetricsMiddleware",
    "configure_metrics",
    "enabled",
    "record_evidence_metric",
    "record_pipeline_metric",
    "record_request",
    "render",
]
