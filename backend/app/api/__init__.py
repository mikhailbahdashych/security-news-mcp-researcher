from fastapi import APIRouter

from app.api import feeds, health, items, models, sessions, settings

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(settings.router)
api_router.include_router(models.router)
api_router.include_router(feeds.router)
api_router.include_router(items.router)
api_router.include_router(sessions.router)

__all__ = ["api_router"]
