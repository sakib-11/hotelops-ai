"""Metrics endpoint — Prometheus exposition format (Task 8.x)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from backend.app.infrastructure.observability import metrics

router: APIRouter = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Expose Prometheus metrics when enabled (404 otherwise).

    Metrics are OPT-IN (``OBSERVABILITY_METRICS_ENABLED=true``): when
    disabled the endpoint returns 404 rather than serving an empty
    registry, making the opt-in state explicit.
    """
    if not metrics.enabled():
        raise HTTPException(status_code=404, detail="Metrics are disabled")
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
