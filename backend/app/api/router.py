"""Main API router aggregating all route modules."""

from fastapi import APIRouter

from backend.app.api.routes.configuration import router as configuration_router
from backend.app.api.routes.health import router as health_router
from backend.app.api.routes.live import router as live_router
from backend.app.api.routes.media import router as media_router
from backend.app.api.routes.metrics import router as metrics_router
from backend.app.api.routes.operational import router as operational_router

api_router: APIRouter = APIRouter()
api_router.include_router(health_router)
api_router.include_router(media_router)
api_router.include_router(metrics_router)
api_router.include_router(configuration_router)
api_router.include_router(operational_router)
api_router.include_router(live_router)
