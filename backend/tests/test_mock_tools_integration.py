"""Step 5 tools through the existing Step 4 path.

- registry: the four existing tools, nothing more;
- execute_tool Activity with the database-backed handlers;
- a full OrderWorkflow run on Temporal's test server with the REAL worker
  wiring (``create_worker`` -> PostgreSQL persistence + mock tools), against
  the real local PostgreSQL inside a rolled-back transaction.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from app.db.mock_factories import create_mock_order, create_mock_shipment
from app.db.mock_models import MockShipment
from app.db.models import Action, Run, Supervisor, ToolExecution
from app.llm.fake import FakeLLMClient, make_decision_json
from app.temporal.activities.tools import ToolActivities
from app.temporal.constants import TASK_QUEUE, order_workflow_id
from app.temporal.contracts import ToolRequest
from app.temporal.types import OrderWorkflowInput, WorkflowState
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from app.tools.mock_operations import build_mock_tool_handlers
from app.tools.registry import TOOL_SPECS

ALL_TOOLS = list(TOOL_SPECS)


@pytest_asyncio.fixture
async def run_id(db_session_factory) -> str:
    """Supervisor + run for ORD-001, plus its mock order and delayed shipment."""
    async with db_session_factory() as session:
        supervisor = Supervisor(
            name=f"step5-{uuid.uuid4().hex[:8]}",
            instructions="Supervise.",
            wake_policy={"important_event_types": ["shipment_delayed"]},
            enabled_tools=ALL_TOOLS,
            default_wake_interval=60,
            min_wake_interval=5,
            max_wake_interval=1440,
            terminal_order_statuses=["delivered"],
            version=1,
        )
        session.add(supervisor)
        await session.flush()
        run = Run(order_id="ORD-001", supervisor_id=supervisor.id)
        session.add(run)
        await create_mock_order(session, order_id="ORD-001", status="shipped")
        await create_mock_shipment(
            session, order_id="ORD-001", shipment_id="SHIP-001", status="delayed", delay_reason="Carrier delay"
        )
        await session.commit()
        return str(run.id)


def test_registry_resolves_exactly_the_four_existing_tools(db_session_factory):
    handlers = build_mock_tool_handlers(db_session_factory)
    assert set(handlers) == set(TOOL_SPECS)
    ToolActivities(handlers)  # accepted by the existing registry validation


@pytest.mark.asyncio
async def test_execute_tool_activity_runs_database_backed_tools(db_session_factory, run_id):
    activities = ToolActivities(build_mock_tool_handlers(db_session_factory))
    request = ToolRequest(tool_execution_id=str(uuid.uuid4()), order_id="ORD-001", tool_name="get_shipment_status", tool_input={})
    result = await ActivityEnvironment().run(activities.execute_tool, request)
    assert result.success is True
    assert result.output["shipment_id"] == "SHIP-001"
    assert result.output["status"] == "delayed"

    # Business failure is a normal Activity result, not an exception.
    missing = ToolRequest(tool_execution_id=str(uuid.uuid4()), order_id="ORD-404", tool_name="get_order_status", tool_input={})
    failure = await ActivityEnvironment().run(activities.execute_tool, missing)
    assert (failure.success, failure.error) == (False, "Order not found")


async def _wait_settled(handle, attempts: int = 500):
    status = None
    for _ in range(attempts):
        status = await handle.query(OrderWorkflow.get_status)
        if status.reasoning_count == 1 and status.state == WorkflowState.SLEEPING:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow did not settle: {status}")


async def _run_one_cycle(db_session_factory, run_id: str, decision: str, tool_registry=None):
    llm = FakeLLMClient([decision])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with create_worker(
            env.client, session_factory=db_session_factory, llm_client=llm, tool_registry=tool_registry
        ):
            handle = await env.client.start_workflow(
                OrderWorkflow.run,
                OrderWorkflowInput(order_id="ORD-001", run_id=run_id, enabled_tools=ALL_TOOLS),
                id=order_workflow_id("ORD-001"),
                task_queue=TASK_QUEUE,
            )
            status = await _wait_settled(handle)
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
            await handle.terminate(reason="test cleanup")
            return status


async def _tool_rows(factory, run_id: str):
    async with factory() as session:
        rows = (
            await session.execute(
                select(Action, ToolExecution)
                .join(ToolExecution, ToolExecution.action_id == Action.id)
                .where(Action.run_id == uuid.UUID(run_id))
            )
        ).all()
    return rows


@pytest.mark.asyncio
async def test_workflow_tool_call_mutates_mock_state_and_records_supervisor_history(db_session_factory, run_id):
    decision = make_decision_json(
        assessment="Delayed shipment; escalate.",
        tool="escalate_shipment",
        reason="Shipment has been delayed for 24 hours",
        priority="high",
    )
    status = await _run_one_cycle(db_session_factory, run_id, decision)
    assert status.last_decision["executed_tool"] == "escalate_shipment"

    # Operational state: the operation's result.
    async with db_session_factory() as session:
        shipment = (await session.execute(select(MockShipment).where(MockShipment.order_id == "ORD-001"))).scalar_one()
    assert shipment.escalated is True

    # Supervisor history: "the supervisor invoked this operation" (existing Step 4 persistence).
    ((action, execution),) = await _tool_rows(db_session_factory, run_id)
    assert (action.action_type, action.status) == ("escalate_shipment", "completed")
    assert execution.status == "success"
    assert execution.input == {"reason": "Shipment has been delayed for 24 hours", "priority": "high"}
    assert execution.result == {"order_id": "ORD-001", "shipment_id": "SHIP-001", "escalated": True}
    assert execution.error is None


@pytest.mark.asyncio
async def test_tool_business_failure_is_recorded_not_raised(db_session_factory, run_id):
    handlers = build_mock_tool_handlers(db_session_factory)
    calls = []

    # The order exists but has no shipment -> get_shipment_status is a business failure.
    async with db_session_factory() as session:
        await session.execute(MockShipment.__table__.delete().where(MockShipment.order_id == "ORD-001"))
        await session.commit()

    async def counted_shipment(request):
        calls.append(request.tool_execution_id)
        return await handlers["get_shipment_status"](request)

    registry = dict(handlers, get_shipment_status=counted_shipment)
    status = await _run_one_cycle(db_session_factory, run_id, make_decision_json(tool="get_shipment_status"), registry)

    assert len(calls) == 1  # a business failure is not an Activity exception, so it is not retried
    assert status.memory["last_action"] == "get_shipment_status: failed"
    ((action, execution),) = await _tool_rows(db_session_factory, run_id)
    assert (action.status, execution.status, execution.error) == ("failed", "failed", "Shipment not found")
    assert execution.result is None
