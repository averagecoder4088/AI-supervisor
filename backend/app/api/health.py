"""Development health check endpoint."""

from typing import Dict
from fastapi import APIRouter, Depends
from app.config import Settings, get_settings

router = APIRouter(tags=["Health"])


@router.get("/health")
def health_check(settings: Settings = Depends(get_settings)) -> Dict[str, str]:
    """Return backend process health status and environment."""
    return {
        "status": "ok",
        "app_env": settings.app_env,
    }

