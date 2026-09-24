"""API router registry for Order Supervisor backend."""

from fastapi import APIRouter
from app.api.health import router as health_router
from app.api.observation import router as observation_router
from app.api.runs import router as runs_router
from app.api.supervisors import router as supervisors_router

# Root router: development/utility endpoints, the Step 6 /api resources and the
# Step 7 read-only observation endpoints.
router = APIRouter()
router.include_router(health_router)
router.include_router(supervisors_router)
router.include_router(runs_router)
router.include_router(observation_router)

