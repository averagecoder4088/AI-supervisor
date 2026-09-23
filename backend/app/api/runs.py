"""Run endpoints: a thin boundary over PostgreSQL and the Temporal client.

- Creating a run persists it as ``starting``, starts ``order-<order_id>``, then
  records ``running`` or ``failed``. PostgreSQL and Temporal are NOT one atomic
  transaction; each step's outcome is reported honestly.
- Events, run instructions and pause/resume/interrupt are Temporal Signals. The
  API never writes events or run instructions itself; the workflow's Activities do.
- Terminate is Temporal's client-side hard stop, not a Signal.
- 202 means "accepted at the workflow boundary", not "processed".
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.api.dependencies import get_session, get_temporal_client
from app.api.errors import (
    RUN_ALREADY_EXISTS,
    RUN_NOT_ACTIVE,
    RUN_NOT_FOUND,
    RUN_STATE_UPDATE_FAILED,
    SUPERVISOR_NOT_FOUND,
    TEMPORAL_UNAVAILABLE,
    WORKFLOW_ALREADY_STARTED,
    WORKFLOW_START_FAILED,
    ApiError,
)
from app.api.schemas import Accepted, EventCreate, InstructionCreate, RunCreate, RunOut
from app.db.models import Run, Supervisor
from app.temporal.constants import TASK_QUEUE, order_workflow_id
from app.temporal.supervisor_config import build_workflow_input
from app.temporal.types import OrderEvent
from app.temporal.workflows import OrderWorkflow

router = APIRouter(prefix="/api/runs", tags=["Runs"])

# Run lifecycle (decision B). "active" is the legacy Step 2 default, tolerated.
ACTIVE_STATUSES = frozenset({"starting", "running", "active"})


def run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        order_id=run.order_id,
        supervisor_id=run.supervisor_id,
        supervisor_version=run.supervisor.version,
        status=run.status,
        order_status=run.order_status,
        run_instructions=run.run_instructions,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


async def _load_run(session: AsyncSession, run_id: uuid.UUID) -> Optional[Run]:
    query = select(Run).options(selectinload(Run.supervisor)).where(Run.id == run_id)
    return (await session.execute(query)).scalar_one_or_none()


async def _active_run(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = await _load_run(session, run_id)
    if run is None:
        raise ApiError(404, RUN_NOT_FOUND, "Run not found")
    if run.status not in ACTIVE_STATUSES:
        raise ApiError(409, RUN_NOT_ACTIVE, f"Run is {run.status}")
    return run


def _temporal_error(err: RPCError) -> ApiError:
    if err.status == RPCStatusCode.NOT_FOUND:
        # The workflow is closed (or gone) even though the run looked active.
        return ApiError(409, RUN_NOT_ACTIVE, "Workflow is not running")
    return ApiError(503, TEMPORAL_UNAVAILABLE, "Temporal is unavailable")


def _handle(client: Client, run: Run) -> WorkflowHandle:
    return client.get_workflow_handle(order_workflow_id(run.order_id))


# ------------------------------------------------------------------ create


async def _set_status(session: AsyncSession, run: Run, status: str, **fields) -> None:
    run.status = status
    for name, value in fields.items():
        setattr(run, name, value)
    await session.commit()


@router.post("", status_code=201, response_model=RunOut)
async def create_run(
    body: RunCreate,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_temporal_client),
) -> RunOut:
    supervisor = await session.get(Supervisor, body.supervisor_id)
    if supervisor is None:
        raise ApiError(404, SUPERVISOR_NOT_FOUND, "Supervisor not found")
    if (await session.execute(select(Run.id).where(Run.order_id == body.order_id))).first() is not None:
        raise ApiError(409, RUN_ALREADY_EXISTS, f"A run for order {body.order_id!r} already exists")

    now = datetime.now(timezone.utc)
    run = Run(
        order_id=body.order_id,
        supervisor_id=supervisor.id,
        status="starting",
        run_instructions=[{"text": text, "added_at": now.isoformat()} for text in body.run_instructions],
    )
    session.add(run)
    try:
        await session.commit()  # persisted as "starting" BEFORE Temporal is contacted
    except IntegrityError as err:
        await session.rollback()
        raise ApiError(409, RUN_ALREADY_EXISTS, f"A run for order {body.order_id!r} already exists") from err
    run = await _load_run(session, run.id)

    # Snapshot this supervisor version into the workflow input (existing Step 4 translation).
    workflow_input = build_workflow_input(supervisor, run)
    start_error: Optional[ApiError] = None
    try:
        await client.start_workflow(
            OrderWorkflow.run, workflow_input, id=order_workflow_id(run.order_id), task_queue=TASK_QUEUE
        )
    except WorkflowAlreadyStartedError:
        start_error = ApiError(409, WORKFLOW_ALREADY_STARTED, f"Workflow for order {run.order_id!r} already exists")
    except RPCError:
        start_error = ApiError(503, TEMPORAL_UNAVAILABLE, "Temporal is unavailable; the run was marked failed")
    except Exception:  # noqa: BLE001 - any other start failure must still mark the run failed
        start_error = ApiError(500, WORKFLOW_START_FAILED, "Workflow could not be started; the run was marked failed")

    if start_error is not None:
        try:
            await _set_status(session, run, "failed")
        except SQLAlchemyError as err:
            raise ApiError(500, RUN_STATE_UPDATE_FAILED, "Workflow start failed and the run could not be marked failed") from err
        raise start_error

    try:
        await _set_status(session, run, "running", started_at=datetime.now(timezone.utc))
    except SQLAlchemyError as err:
        # Decision G: the workflow IS running; do not terminate it, do not pretend.
        await session.rollback()
        raise ApiError(
            500, RUN_STATE_UPDATE_FAILED, "Workflow started but the run could not be marked running; it remains starting"
        ) from err
    return run_out(run)


# -------------------------------------------------------------------- read


@router.get("", response_model=List[RunOut])
async def list_runs(session: AsyncSession = Depends(get_session)) -> List[RunOut]:
    query = select(Run).options(selectinload(Run.supervisor)).order_by(Run.created_at, Run.order_id)
    return [run_out(run) for run in (await session.execute(query)).scalars().all()]


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> RunOut:
    run = await _load_run(session, run_id)
    if run is None:
        raise ApiError(404, RUN_NOT_FOUND, "Run not found")
    return run_out(run)


# ---------------------------------------------------------- events/controls


@router.post("/{run_id}/events", status_code=202, response_model=Accepted)
async def submit_event(
    run_id: uuid.UUID,
    body: EventCreate,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_temporal_client),
) -> Accepted:
    run = await _active_run(session, run_id)
    try:
        await _handle(client, run).signal(
            OrderWorkflow.submit_event, OrderEvent(event_type=body.event_type, payload=body.payload)
        )
    except RPCError as err:
        raise _temporal_error(err) from err
    return Accepted(run_id=run.id, request="event")


@router.post("/{run_id}/instructions", status_code=202, response_model=Accepted)
async def add_instruction(
    run_id: uuid.UUID,
    body: InstructionCreate,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(get_temporal_client),
) -> Accepted:
    # The workflow owns run-instruction state: it stores the instruction and
    # persists runs.run_instructions through its save_run_instructions Activity.
    run = await _active_run(session, run_id)
    try:
        await _handle(client, run).signal(OrderWorkflow.add_run_instruction, body.instruction)
    except RPCError as err:
        raise _temporal_error(err) from err
    return Accepted(run_id=run.id, request="instruction")


async def _control(run_id: uuid.UUID, session: AsyncSession, client: Client, signal, name: str) -> Accepted:
    run = await _active_run(session, run_id)
    try:
        await _handle(client, run).signal(signal)
    except RPCError as err:
        raise _temporal_error(err) from err
    return Accepted(run_id=run.id, request=name)


@router.post("/{run_id}/pause", status_code=202, response_model=Accepted)
async def pause_run(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_session), client: Client = Depends(get_temporal_client)
) -> Accepted:
    return await _control(run_id, session, client, OrderWorkflow.pause, "pause")


@router.post("/{run_id}/resume", status_code=202, response_model=Accepted)
async def resume_run(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_session), client: Client = Depends(get_temporal_client)
) -> Accepted:
    return await _control(run_id, session, client, OrderWorkflow.resume, "resume")


@router.post("/{run_id}/interrupt", status_code=202, response_model=Accepted)
async def interrupt_run(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_session), client: Client = Depends(get_temporal_client)
) -> Accepted:
    return await _control(run_id, session, client, OrderWorkflow.interrupt, "interrupt")


@router.post("/{run_id}/terminate", status_code=202, response_model=Accepted)
async def terminate_run(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_session), client: Client = Depends(get_temporal_client)
) -> Accepted:
    run = await _active_run(session, run_id)
    try:
        await _handle(client, run).terminate(reason="Terminated via API")  # hard stop, not a Signal
    except RPCError as err:
        raise _temporal_error(err) from err
    try:
        await _set_status(session, run, "terminated")
    except SQLAlchemyError as err:
        await session.rollback()
        raise ApiError(500, RUN_STATE_UPDATE_FAILED, "Workflow terminated but the run status could not be updated") from err
    return Accepted(run_id=run.id, request="terminate")
