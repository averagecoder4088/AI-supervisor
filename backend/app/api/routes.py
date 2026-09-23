"""API router registry for Order Supervisor backend."""

from fastapi import APIRouter
from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.api.supervisors import router as supervisors_router

# Root router: development/utility endpoints plus the Step 6 /api resources.
router = APIRouter()
router.include_router(health_router)
router.include_router(supervisors_router)
router.include_router(runs_router)

