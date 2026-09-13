from fastapi import APIRouter

from app.api import health, models, settings

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(settings.router)
api_router.include_router(models.router)

__all__ = ["api_router"]
