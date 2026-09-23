"""Run, event and control endpoints against Temporal's test server + real PostgreSQL.

The worker uses the REAL Activities (PostgreSQL persistence, database-backed
mock tools) with FakeLLMClient; the API uses a pooled engine (decision J).
"""

import asyncio
import json
import uuid
from typing import Any, AsyncIterator, Callable, Dict

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

from app.db.models import Event, Run
from app.llm.fake import FakeLLMClient
from app.main import create_app
from app.temporal.constants import TASK_QUEUE, order_workflow_id
from app.temporal.types import OrderWorkflowInput, WorkflowState
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.api_support import PREFIX, pooled_session_factory, supervisor_body  # noqa: F401

pytestmark = pytest.mark.asyncio


class Stack:
    def __init__(self, env: WorkflowEnvironment, factory, api: httpx.AsyncClient) -> None:
        self.env = env
        self.factory = factory
        self.api = api

    def worker(self):
        return create_worker(self.env.client, session_factory=self.factory, llm_client=FakeLLMClient())

    async def supervisor(self, **changes: Any) -> Dict[str, Any]:
        response = await self.api.post("/api/supervisors", json=supervisor_body(**changes))
        assert response.status_code == 201, response.text
        return response.json()

    async def run(self, order: str, supervisor_id: str, **extra: Any) -> httpx.Response:
        return await self.api.post(
            "/api/runs", json={"order_id": f"{PREFIX}{order}", "supervisor_id": supervisor_id, **extra}
        )

    async def db_run(self, order: str) -> Run:
        async with self.factory() as session:
            return (await session.execute(select(Run).where(Run.order_id == f"{PREFIX}{order}"))).scalar_one()

    async def events(self, run_id: str):
        async with self.factory() as session:
            return (await session.execute(select(Event).where(Event.run_id == uuid.UUID(run_id)))).scalars().all()

    def handle(self, order: str):
        return self.env.client.get_workflow_handle(order_workflow_id(f"{PREFIX}{order}"))


@pytest_asyncio.fixture
async def stack(pooled_session_factory) -> AsyncIterator[Stack]:  # noqa: F811
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=pooled_session_factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            yield Stack(env, pooled_session_factory, api)


async def eventually(check: Callable, attempts: int = 300):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def workflow_status(stack: Stack, order: str):
    return await stack.handle(order).query(OrderWorkflow.get_status)


async def started_input(stack: Stack, order: str) -> Dict[str, Any]:
    history = await stack.handle(order).fetch_history()
    payload = history.events[0].workflow_execution_started_event_attributes.input.payloads[0]
    return json.loads(payload.data)


# ------------------------------------------------------------ run creation


async def test_create_run_starts_workflow_and_becomes_running(stack):
    supervisor = await stack.supervisor()
    response = await stack.run("1", supervisor["id"], run_instructions=["Prioritize shipment delays."])
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "running"
    assert run["started_at"] is not None
    assert run["supervisor_id"] == supervisor["id"]
    assert run["supervisor_version"] == 1
    assert [i["text"] for i in run["run_instructions"]] == ["Prioritize shipment delays."]

    description = await stack.handle("1").describe()
    assert description.id == f"order-{PREFIX}1"
    assert description.status == WorkflowExecutionStatus.RUNNING
    assert (await stack.db_run("1")).status == "running"


async def test_workflow_input_snapshots_supervisor_version_and_run_instructions(stack):
    supervisor = await stack.supervisor()
    run = (await stack.run("2", supervisor["id"], run_instructions=["Keep customer communication concise."])).json()

    started = await started_input(stack, "2")
    assert started["run_id"] == run["id"]
    assert started["supervisor_version"] == 1
    assert started["supervisor_instructions"] == "Monitor shipment health and respond to delays."
    assert started["enabled_tools"] == supervisor["enabled_tools"]
    assert started["order_status_by_event"] == {"delivered": "delivered", "order_cancelled": "cancelled"}
    assert [i["text"] for i in started["run_instructions"]] == ["Keep customer communication concise."]

    # A newer supervisor version does not change the existing run.
    await stack.supervisor(instructions="Totally different.")
    again = (await stack.api.get(f"/api/runs/{run['id']}")).json()
    assert (again["supervisor_id"], again["supervisor_version"]) == (supervisor["id"], 1)


async def test_supervisor_version_comes_from_linked_supervisor_not_a_runs_column(stack):
    assert "supervisor_version" not in Run.__table__.columns
    await stack.supervisor()
    v2 = await stack.supervisor(instructions="Version two.")
    run = (await stack.run("3", v2["id"])).json()
    assert run["supervisor_version"] == 2
    assert (await started_input(stack, "3"))["supervisor_version"] == 2


async def test_duplicate_order_is_409_and_starts_no_second_workflow(stack):
    supervisor = await stack.supervisor()
    first = (await stack.run("4", supervisor["id"])).json()
    response = await stack.run("4", supervisor["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "RUN_ALREADY_EXISTS"

    async with stack.factory() as session:
        runs = (await session.execute(select(Run).where(Run.order_id == f"{PREFIX}4"))).scalars().all()
    assert [str(r.id) for r in runs] == [first["id"]]
    assert (await started_input(stack, "4"))["run_id"] == first["id"]  # still the original workflow


async def test_unknown_supervisor_is_404(stack):
    response = await stack.run("5", str(uuid.uuid4()))
    assert (response.status_code, response.json()["code"]) == (404, "SUPERVISOR_NOT_FOUND")


async def test_workflow_start_failure_marks_run_failed(stack):
    supervisor = await stack.supervisor()
    # A workflow with this id already exists in Temporal -> the API's start fails for real.
    await stack.env.client.start_workflow(
        OrderWorkflow.run, OrderWorkflowInput(order_id=f"{PREFIX}6"), id=order_workflow_id(f"{PREFIX}6"), task_queue=TASK_QUEUE
    )
    response = await stack.run("6", supervisor["id"])
    assert (response.status_code, response.json()["code"]) == (409, "WORKFLOW_ALREADY_STARTED")
    run = await stack.db_run("6")
    assert run.status == "failed"
    assert run.started_at is None

    # A failed run rejects events.
    event = await stack.api.post(f"/api/runs/{run.id}/events", json={"event_type": "shipment_delayed"})
    assert (event.status_code, event.json()["code"]) == (409, "RUN_NOT_ACTIVE")


async def test_get_run_and_list_runs(stack):
    supervisor = await stack.supervisor()
    a = (await stack.run("7a", supervisor["id"])).json()
    b = (await stack.run("7b", supervisor["id"])).json()
    got = await stack.api.get(f"/api/runs/{a['id']}")
    assert (got.status_code, got.json()) == (200, a)

    listed = await stack.api.get("/api/runs")
    assert listed.status_code == 200
    ids = [r["id"] for r in listed.json() if r["order_id"].startswith(PREFIX)]
    assert ids == [a["id"], b["id"]]


async def test_unknown_run_is_404(stack):
    missing = uuid.uuid4()
    assert (await stack.api.get(f"/api/runs/{missing}")).json()["code"] == "RUN_NOT_FOUND"
    for path in ("events", "pause", "resume", "interrupt", "terminate"):
        response = await stack.api.post(f"/api/runs/{missing}/{path}", json={"event_type": "delivered"})
        assert (response.status_code, response.json()["code"]) == (404, "RUN_NOT_FOUND")


async def test_run_request_validation_is_400(stack):
    supervisor = await stack.supervisor()
    for body in (
        {"order_id": f"{PREFIX}8"},  # no supervisor_id
        {"order_id": f"{PREFIX}8", "supervisor_id": "nope"},
        {"order_id": "   ", "supervisor_id": supervisor["id"]},
        {"order_id": f"{PREFIX}8", "supervisor_id": supervisor["id"], "run_instructions": ["ok", " "]},
    ):
        response = await stack.api.post("/api/runs", json=body)
        assert (response.status_code, response.json()["code"]) == (400, "VALIDATION_ERROR"), body


# ------------------------------------------------------------------ events


async def test_http_event_goes_through_signal_and_record_event_not_the_api(stack):
    """202 = accepted at the boundary. The row appears only when the WORKFLOW records it."""
    supervisor = await stack.supervisor()
    run = (await stack.run("9", supervisor["id"])).json()  # started, but no worker is polling yet

    response = await stack.api.post(
        f"/api/runs/{run['id']}/events", json={"event_type": "payment_confirmed", "payload": {"amount": 42}}
    )
    assert response.status_code == 202
    assert response.json() == {"run_id": run["id"], "request": "event", "accepted": True}
    assert await stack.events(run["id"]) == []  # FastAPI inserted nothing

    async with stack.worker():  # now the workflow runs and processes the Signal
        rows = await eventually(lambda: stack.events(run["id"]))
    (row,) = rows
    assert (row.event_type, row.payload) == ("payment_confirmed", {"amount": 42})


async def test_invalid_event_type_is_400(stack):
    supervisor = await stack.supervisor()
    run = (await stack.run("10", supervisor["id"])).json()
    for body in ({"event_type": "package_lost"}, {"event_type": "delivered", "payload": ["not", "an", "object"]}, {}):
        response = await stack.api.post(f"/api/runs/{run['id']}/events", json=body)
        assert (response.status_code, response.json()["code"]) == (400, "VALIDATION_ERROR")


async def test_pause_resume_interrupt_and_events_while_paused(stack):
    supervisor = await stack.supervisor()
    run = (await stack.run("11", supervisor["id"])).json()
    async with stack.worker():
        await eventually(lambda: _settled(stack, "11"))

        paused = await stack.api.post(f"/api/runs/{run['id']}/pause")
        assert (paused.status_code, paused.json()["request"]) == (202, "pause")
        await eventually(lambda: _state(stack, "11", WorkflowState.PAUSED))

        # Paused runs still accept events; the workflow records them without reasoning.
        event = await stack.api.post(f"/api/runs/{run['id']}/events", json={"event_type": "shipment_delayed"})
        assert event.status_code == 202
        await eventually(lambda: stack.events(run["id"]))
        assert (await workflow_status(stack, "11")).reasoning_count == 1

        resumed = await stack.api.post(f"/api/runs/{run['id']}/resume")
        assert resumed.status_code == 202
        status = await eventually(lambda: _reasoned(stack, "11", 2))
        assert status.last_wake_reason == "resume"

        interrupted = await stack.api.post(f"/api/runs/{run['id']}/interrupt")
        assert interrupted.status_code == 202
        status = await eventually(lambda: _interrupts(stack, "11", 1))
        assert (await stack.handle("11").describe()).status == WorkflowExecutionStatus.RUNNING


async def test_terminate_is_client_side_and_run_then_rejects_requests(stack):
    supervisor = await stack.supervisor()
    run = (await stack.run("12", supervisor["id"])).json()

    response = await stack.api.post(f"/api/runs/{run['id']}/terminate")
    assert (response.status_code, response.json()["request"]) == (202, "terminate")
    assert (await stack.handle("12").describe()).status == WorkflowExecutionStatus.TERMINATED
    assert (await stack.db_run("12")).status == "terminated"

    for path, body in (("events", {"event_type": "delivered"}), ("pause", None), ("resume", None), ("interrupt", None), ("terminate", None)):
        rejected = await stack.api.post(f"/api/runs/{run['id']}/{path}", json=body)
        assert (rejected.status_code, rejected.json()["code"]) == (409, "RUN_NOT_ACTIVE"), path


async def test_completed_run_rejects_events(stack):
    supervisor = await stack.supervisor()  # maps delivered -> delivered, terminal on delivered
    run = (await stack.run("13", supervisor["id"])).json()
    async with stack.worker():
        await eventually(lambda: _settled(stack, "13"))
        assert (await stack.api.post(f"/api/runs/{run['id']}/events", json={"event_type": "delivered"})).status_code == 202
        await eventually(lambda: _db_status(stack, "13", "completed"))

    rejected = await stack.api.post(f"/api/runs/{run['id']}/events", json={"event_type": "shipment_delayed"})
    assert (rejected.status_code, rejected.json()["code"]) == (409, "RUN_NOT_ACTIVE")


# ---------------------------------------------------------- instructions


async def test_instruction_is_a_signal_and_the_workflow_persists_it(stack):
    supervisor = await stack.supervisor()
    run = (await stack.run("16", supervisor["id"], run_instructions=["Prioritize shipment delays."])).json()

    response = await stack.api.post(
        f"/api/runs/{run['id']}/instructions", json={"instruction": "  Prioritize customer communication.  "}
    )
    assert response.status_code == 202
    assert response.json() == {"run_id": run["id"], "request": "instruction", "accepted": True}
    # No worker yet: the API did not touch runs.run_instructions itself.
    assert [i["text"] for i in (await stack.db_run("16")).run_instructions] == ["Prioritize shipment delays."]

    async with stack.worker():
        # The Signal reached the workflow's own instruction state...
        await eventually(lambda: _instruction_count(stack, "16", 2))
        # ...and the workflow persisted it through its save_run_instructions Activity.
        await eventually(lambda: _db_instruction_count(stack, "16", 2))
    texts = [i["text"] for i in (await stack.db_run("16")).run_instructions]
    assert texts == ["Prioritize shipment delays.", "Prioritize customer communication."]


async def test_instruction_validation_unknown_and_inactive_runs(stack):
    supervisor = await stack.supervisor()
    run = (await stack.run("17", supervisor["id"])).json()
    for body in ({}, {"instruction": ""}, {"instruction": "   "}, {"instruction": "ok", "extra": 1}):
        response = await stack.api.post(f"/api/runs/{run['id']}/instructions", json=body)
        assert (response.status_code, response.json()["code"]) == (400, "VALIDATION_ERROR"), body

    missing = await stack.api.post(f"/api/runs/{uuid.uuid4()}/instructions", json={"instruction": "x"})
    assert (missing.status_code, missing.json()["code"]) == (404, "RUN_NOT_FOUND")

    assert (await stack.api.post(f"/api/runs/{run['id']}/terminate")).status_code == 202
    inactive = await stack.api.post(f"/api/runs/{run['id']}/instructions", json={"instruction": "x"})
    assert (inactive.status_code, inactive.json()["code"]) == (409, "RUN_NOT_ACTIVE")


# ------------------------------------------------------ Temporal unavailable


class _UnavailableTemporal:
    """Stands in for a Temporal client whose server cannot be reached."""

    def get_workflow_handle(self, workflow_id: str) -> "_UnavailableTemporal":
        return self

    async def _fail(self, *args: Any, **kwargs: Any) -> None:
        raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")

    signal = terminate = start_workflow = _fail


async def _unavailable_api(factory, client) -> httpx.AsyncClient:
    app = create_app(temporal_client=client, session_factory=factory, connect_temporal=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_temporal_unavailable_returns_503(pooled_session_factory):  # noqa: F811
    async with await _unavailable_api(pooled_session_factory, _UnavailableTemporal()) as api:
        supervisor = (await api.post("/api/supervisors", json=supervisor_body())).json()

        # Start attempted and failed -> run persisted as failed, 503.
        response = await api.post("/api/runs", json={"order_id": f"{PREFIX}14", "supervisor_id": supervisor["id"]})
        assert (response.status_code, response.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE")

        # Event/control on an active run while Temporal is unreachable -> 503.
        async with pooled_session_factory() as session:
            run = Run(order_id=f"{PREFIX}14b", supervisor_id=uuid.UUID(supervisor["id"]), status="running")
            session.add(run)
            await session.commit()
        for path, body in (
            ("events", {"event_type": "shipment_delayed"}),
            ("instructions", {"instruction": "Be brief."}),
            ("pause", None),
            ("terminate", None),
        ):
            failed = await api.post(f"/api/runs/{run.id}/{path}", json=body)
            assert (failed.status_code, failed.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE"), path

    async with pooled_session_factory() as session:
        statuses = dict((await session.execute(select(Run.order_id, Run.status).where(Run.order_id.like(f"{PREFIX}14%")))).all())
    assert statuses == {f"{PREFIX}14": "failed", f"{PREFIX}14b": "running"}  # terminate failed -> not marked


async def test_app_without_temporal_still_serves_database_endpoints(pooled_session_factory):  # noqa: F811
    async with await _unavailable_api(pooled_session_factory, None) as api:
        supervisor = await api.post("/api/supervisors", json=supervisor_body())
        assert supervisor.status_code == 201
        assert (await api.get("/api/runs")).status_code == 200
        response = await api.post("/api/runs", json={"order_id": f"{PREFIX}15", "supervisor_id": supervisor.json()["id"]})
        assert (response.status_code, response.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE")
    async with pooled_session_factory() as session:
        # No start could be attempted, so no run was persisted.
        assert (await session.execute(select(Run).where(Run.order_id == f"{PREFIX}15"))).first() is None


# ------------------------------------------------------------------ helpers


async def _settled(stack: Stack, order: str):
    status = await workflow_status(stack, order)
    return status if status.reasoning_count >= 1 and status.state == WorkflowState.SLEEPING else None


async def _state(stack: Stack, order: str, state: str):
    status = await workflow_status(stack, order)
    return status if status.state == state else None


async def _reasoned(stack: Stack, order: str, count: int):
    status = await workflow_status(stack, order)
    return status if status.reasoning_count == count and status.state == WorkflowState.SLEEPING else None


async def _interrupts(stack: Stack, order: str, count: int):
    status = await workflow_status(stack, order)
    return status if status.interrupt_count == count else None


async def _instruction_count(stack: Stack, order: str, count: int):
    status = await workflow_status(stack, order)
    return status if len(status.run_instructions) == count else None


async def _db_instruction_count(stack: Stack, order: str, count: int):
    return len((await stack.db_run(order)).run_instructions) == count


async def _db_status(stack: Stack, order: str, expected: str):
    return (await stack.db_run(order)).status == expected
