"""Step 7: read-only observation endpoints for a run.

Nothing here writes to PostgreSQL, sends a Signal, runs an Activity, a tool or
the LLM, or reconciles PostgreSQL with Temporal.

- History (timeline, memory, actions, tool executions, final output) is read
  from PostgreSQL, the source of truth for persisted records. Any existing run
  is observable, whatever its status.
- Live status comes only from Temporal's existing ``OrderWorkflow.get_status``
  query, with ``QueryRejectCondition.NOT_OPEN`` so a closed workflow is
  rejected (409) instead of replayed into stale in-memory state.

Ordering is chronological (oldest first) with the row id as a deterministic
tie-breaker; UUID ids do not represent true insertion order.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowQueryFailedError, WorkflowQueryRejectedError
from temporalio.common import QueryRejectCondition
from temporalio.service import RPCError

from app.api.dependencies import get_session, get_temporal_client
from app.api.errors import RUN_NOT_ACTIVE, RUN_NOT_FOUND, TEMPORAL_UNAVAILABLE, ApiError
from app.api.runs import _handle, _load_run, _temporal_error
from app.api.schemas import (
    ActionOut,
    ActionsOut,
    FinalOutputOut,
    MemoryOut,
    MemorySnapshotOut,
    TimelineEntryOut,
    TimelineOut,
    ToolExecutionOut,
    ToolExecutionsOut,
)
from app.db.models import Action, FinalOutput, MemorySnapshot, Run, TimelineEntry, ToolExecution
from app.temporal.types import OrderWorkflowStatus
from app.temporal.workflows import OrderWorkflow

router = APIRouter(prefix="/api/runs", tags=["Observation"])

# RPC timeout for the status query (decision H1). Overridable per app through
# ``app.state.status_query_timeout`` (tests use a short value).
STATUS_QUERY_TIMEOUT = timedelta(seconds=5)


async def _existing_run(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = await _load_run(session, run_id)
    if run is None:
        raise ApiError(404, RUN_NOT_FOUND, "Run not found")
    return run


# ------------------------------------------------------------ PostgreSQL


@router.get("/{run_id}/timeline", response_model=TimelineOut)
async def get_timeline(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> TimelineOut:
    run = await _existing_run(session, run_id)
    rows = await session.execute(
        select(TimelineEntry)
        .where(TimelineEntry.run_id == run.id)
        .order_by(TimelineEntry.created_at, TimelineEntry.id)
    )
    return TimelineOut(run_id=run.id, entries=[TimelineEntryOut.model_validate(r) for r in rows.scalars()])


@router.get("/{run_id}/memory", response_model=MemoryOut)
async def get_memory(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> MemoryOut:
    run = await _existing_run(session, run_id)
    rows = await session.execute(
        select(MemorySnapshot)
        .where(MemorySnapshot.run_id == run.id)
        .order_by(MemorySnapshot.created_at, MemorySnapshot.id)
    )
    return MemoryOut(run_id=run.id, snapshots=[MemorySnapshotOut.model_validate(r) for r in rows.scalars()])


@router.get("/{run_id}/actions", response_model=ActionsOut)
async def get_actions(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ActionsOut:
    run = await _existing_run(session, run_id)
    rows = await session.execute(
        select(Action).where(Action.run_id == run.id).order_by(Action.created_at, Action.id)
    )
    return ActionsOut(run_id=run.id, actions=[ActionOut.model_validate(r) for r in rows.scalars()])


@router.get("/{run_id}/tool-executions", response_model=ToolExecutionsOut)
async def get_tool_executions(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ToolExecutionsOut:
    run = await _existing_run(session, run_id)
    # tool_executions has no run_id: it belongs to the run through its action.
    rows = await session.execute(
        select(ToolExecution)
        .join(Action, ToolExecution.action_id == Action.id)
        .where(Action.run_id == run.id)
        .order_by(ToolExecution.started_at, ToolExecution.id)
    )
    return ToolExecutionsOut(
        run_id=run.id, tool_executions=[ToolExecutionOut.model_validate(r) for r in rows.scalars()]
    )


@router.get("/{run_id}/final-output", response_model=FinalOutputOut)
async def get_final_output(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> FinalOutputOut:
    run = await _existing_run(session, run_id)
    row = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == run.id))).scalar_one_or_none()
    if row is None:
        return FinalOutputOut(run_id=run.id, final_output=None, created_at=None)
    return FinalOutputOut(run_id=run.id, final_output=row.output, created_at=row.created_at)


# -------------------------------------------------------------- Temporal


@router.get("/{run_id}/status", response_model=OrderWorkflowStatus)
async def get_status(
    run_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OrderWorkflowStatus:
    run = await _existing_run(session, run_id)
    if run.status == "failed":
        # Decision G2: a failed run may never have owned order-<order_id> (e.g. the id
        # was already taken by another workflow), so Temporal is not queried at all.
        raise ApiError(409, RUN_NOT_ACTIVE, "Run is failed")

    client: Client = get_temporal_client(request)
    timeout = getattr(request.app.state, "status_query_timeout", STATUS_QUERY_TIMEOUT)
    try:
        return await _handle(client, run).query(
            OrderWorkflow.get_status,
            reject_condition=QueryRejectCondition.NOT_OPEN,
            rpc_timeout=timeout,
        )
    except WorkflowQueryRejectedError as err:
        # Decision G1: closed (completed/terminated/...) workflows are not replayed.
        raise ApiError(409, RUN_NOT_ACTIVE, "Workflow is not running") from err
    except WorkflowQueryFailedError as err:
        raise ApiError(503, TEMPORAL_UNAVAILABLE, "Workflow status query failed") from err  # decision H2
    except RPCError as err:
        # NOT_FOUND -> 409; CANCELLED / DEADLINE_EXCEEDED / UNAVAILABLE / others -> 503.
        raise _temporal_error(err) from err
