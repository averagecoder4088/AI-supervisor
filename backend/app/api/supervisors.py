"""Supervisor endpoints. PostgreSQL only: creating a supervisor never starts a workflow.

Versions are immutable rows. A new name gets version 1; an existing name gets
max(version) + 1. Earlier versions, and the runs that point at them, are never
modified.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.api.errors import SUPERVISOR_NOT_FOUND, SUPERVISOR_VERSION_CONFLICT, VALIDATION_ERROR, ApiError
from app.api.schemas import SupervisorCreate, SupervisorOut
from app.db.models import Supervisor
from app.temporal.supervisor_config import SupervisorConfigError, validate_supervisor_config

router = APIRouter(prefix="/api/supervisors", tags=["Supervisors"])


def supervisor_out(row: Supervisor) -> SupervisorOut:
    return SupervisorOut(
        id=row.id,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        wake_policy=row.wake_policy,
        enabled_tools=row.enabled_tools,
        default_wake_interval_minutes=row.default_wake_interval,
        min_wake_interval_minutes=row.min_wake_interval,
        max_wake_interval_minutes=row.max_wake_interval,
        terminal_order_statuses=row.terminal_order_statuses,
        order_status_by_event=row.order_status_by_event,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", status_code=201, response_model=SupervisorOut)
async def create_supervisor(body: SupervisorCreate, session: AsyncSession = Depends(get_session)) -> SupervisorOut:
    try:
        config = validate_supervisor_config(
            wake_policy=body.wake_policy,
            enabled_tools=body.enabled_tools,
            terminal_order_statuses=body.terminal_order_statuses,
            order_status_by_event=body.order_status_by_event,
            default_wake_interval=body.default_wake_interval_minutes,
            min_wake_interval=body.min_wake_interval_minutes,
            max_wake_interval=body.max_wake_interval_minutes,
        )
    except SupervisorConfigError as err:
        raise ApiError(400, VALIDATION_ERROR, str(err)) from err

    latest = (
        await session.execute(select(func.max(Supervisor.version)).where(Supervisor.name == body.name))
    ).scalar()
    row = Supervisor(
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        wake_policy={"important_event_types": config.important_event_types},
        enabled_tools=config.enabled_tools,
        default_wake_interval=config.default_wake_interval,
        min_wake_interval=config.min_wake_interval,
        max_wake_interval=config.max_wake_interval,
        terminal_order_statuses=config.terminal_order_statuses,
        order_status_by_event=config.order_status_by_event,
        version=(latest or 0) + 1,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as err:
        # Concurrent creation of the same (name, version): report it; never overwrite.
        await session.rollback()
        raise ApiError(409, SUPERVISOR_VERSION_CONFLICT, "Supervisor version was created concurrently; retry") from err
    await session.refresh(row)
    return supervisor_out(row)


@router.get("/{supervisor_id}", response_model=SupervisorOut)
async def get_supervisor(supervisor_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> SupervisorOut:
    row = await session.get(Supervisor, supervisor_id)
    if row is None:
        raise ApiError(404, SUPERVISOR_NOT_FOUND, "Supervisor not found")
    return supervisor_out(row)
