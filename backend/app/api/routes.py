"""API router registry for Order Supervisor backend."""

from fastapi import APIRouter
from app.api.health import router as health_router

# Root router for development/utility endpoints
router = APIRouter()
router.include_router(health_router)

