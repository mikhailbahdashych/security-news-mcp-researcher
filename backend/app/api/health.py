from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Liveness probe used by the SPA footer and by `docker compose` smoke checks."""
    return HealthResponse(status="ok", version=__version__)
