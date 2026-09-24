"""Step 7 read-only observation endpoints against Temporal's test server + real PostgreSQL.

History endpoints read PostgreSQL only; ``/status`` is Temporal's existing
``get_status`` query. Populated history comes from the REAL path (API supervisor
and run, worker with real Activities and a scripted FakeLLMClient); mock
operational tables are never seeded, so tools end in business failures
("... not found"), which are still recorded as actions and tool executions.
Ordering/tie-break and join cases insert history rows directly with fixed
timestamps and ids, because workflow timestamps can coincide.
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

from app.db.models import Action, FinalOutput, MemorySnapshot, Run, TimelineEntry, ToolExecution
from app.llm.fake import FakeLLMClient, make_decision_json
from app.main import create_app
from app.temporal.constants import order_workflow_id
from app.temporal.types import WorkflowState
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.api_support import PREFIX, pooled_session_factory, supervisor_body  # noqa: F401

pytestmark = pytest.mark.asyncio

HISTORY_PATHS = ("timeline", "memory", "actions", "tool-executions", "final-output")
ALL_PATHS = HISTORY_PATHS + ("status",)

ALL_TABLES = (
    "supervisors",
    "runs",
    "events",
    "timeline_entries",
    "actions",
    "tool_executions",
    "memory_snapshots",
    "final_outputs",
    "mock_orders",
    "mock_shipments",
    "mock_customer_messages",
)

# Business-failing (no mock rows are seeded) tool calls, one per reasoning cycle.
SCRIPT = ("get_order_status", "get_shipment_status", "get_order_status")


# ------------------------------------------------------------------ stubs


class _RecordingTemporal:
    """A Temporal client stand-in that records every use and fails the way it is told to."""

    def __init__(self, error: Optional[BaseException] = None) -> None:
        self.error = error
        self.handles: List[str] = []
        self.queries: List[Any] = []

    def get_workflow_handle(self, workflow_id: str) -> "_RecordingTemporal":
        self.handles.append(workflow_id)
        return self

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        self.queries.append((args, kwargs))
        if self.error is not None:
            raise self.error
        raise AssertionError("unexpected query")

    @property
    def used(self) -> bool:
        return bool(self.handles or self.queries)


def _unavailable() -> RPCError:
    return RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")


# ------------------------------------------------------------------ stack


class Stack:
    def __init__(self, env: WorkflowEnvironment, factory, app, api: httpx.AsyncClient) -> None:
        self.env = env
        self.factory = factory
        self.app = app
        self.api = api

    def worker(self, llm: Optional[FakeLLMClient] = None):
        return create_worker(self.env.client, session_factory=self.factory, llm_client=llm or FakeLLMClient())

    async def supervisor(self, **changes: Any) -> Dict[str, Any]:
        response = await self.api.post("/api/supervisors", json=supervisor_body(**changes))
        assert response.status_code == 201, response.text
        return response.json()

    async def run(self, order: str, supervisor_id: str) -> Dict[str, Any]:
        response = await self.api.post("/api/runs", json={"order_id": f"{PREFIX}{order}", "supervisor_id": supervisor_id})
        assert response.status_code == 201, response.text
        return response.json()

    async def get(self, run_id: Any, path: str) -> httpx.Response:
        return await self.api.get(f"/api/runs/{run_id}/{path}")

    def handle(self, order: str):
        return self.env.client.get_workflow_handle(order_workflow_id(f"{PREFIX}{order}"))

    async def status(self, order: str):
        return await self.handle(order).query(OrderWorkflow.get_status)


@pytest_asyncio.fixture
async def stack(pooled_session_factory) -> AsyncIterator[Stack]:  # noqa: F811
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=pooled_session_factory, connect_temporal=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            yield Stack(env, pooled_session_factory, app, api)


@asynccontextmanager
async def api_for(factory, client) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(temporal_client=client, session_factory=factory, connect_temporal=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        yield api


async def eventually(check: Callable, attempts: int = 500):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def _reasoned(stack: Stack, order: str, count: int):
    status = await stack.status(order)
    return status if status.reasoning_count == count and status.state == WorkflowState.SLEEPING else None


async def _db_status(stack: Stack, run_id: str, expected: str) -> bool:
    async with stack.factory() as session:
        return (await session.get(Run, uuid.UUID(run_id))).status == expected


@asynccontextmanager
async def populated(stack: Stack, order: str) -> AsyncIterator[Dict[str, Any]]:
    """A run with three reasoning cycles (start + two important events), each choosing a tool.

    The worker stays alive (workflow open and sleeping) for the body of the ``with``.
    """
    supervisor = await stack.supervisor()
    run = await stack.run(order, supervisor["id"])
    llm = FakeLLMClient([make_decision_json(tool=tool, assessment=f"Check via {tool}.") for tool in SCRIPT])
    async with stack.worker(llm):
        await eventually(lambda: _reasoned(stack, order, 1))
        for cycle in (2, 3):
            response = await stack.api.post(f"/api/runs/{run['id']}/events", json={"event_type": "shipment_delayed"})
            assert response.status_code == 202
            await eventually(lambda: _reasoned(stack, order, cycle))
        yield run


async def complete(stack: Stack, run_id: str) -> None:
    response = await stack.api.post(f"/api/runs/{run_id}/events", json={"event_type": "delivered"})
    assert response.status_code == 202
    await eventually(lambda: _db_status(stack, run_id, "completed"))


async def insert_run(factory, supervisor_id: str, order: str, status: str = "running") -> uuid.UUID:
    async with factory() as session:
        run = Run(order_id=f"{PREFIX}{order}", supervisor_id=uuid.UUID(supervisor_id), status=status)
        session.add(run)
        await session.commit()
        return run.id


async def table_counts(factory) -> Dict[str, int]:
    async with factory() as session:
        return {
            table: (await session.execute(text(f"select count(*) from {table}"))).scalar_one() for table in ALL_TABLES
        }


def T(seconds: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def U(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def ids(items: List[Dict[str, Any]]) -> List[str]:
    return [item["id"] for item in items]


# ----------------------------------------------- 1, 2: unknown / malformed run


async def test_unknown_run_is_404_on_all_six_endpoints(stack):
    missing = uuid.uuid4()
    for path in ALL_PATHS:
        response = await stack.get(missing, path)
        assert (response.status_code, response.json()["code"]) == (404, "RUN_NOT_FOUND"), path
        assert set(response.json()) == {"error", "code"}


async def test_unknown_run_status_is_404_before_any_temporal_query(pooled_session_factory):  # noqa: F811
    stub = _RecordingTemporal()
    async with api_for(pooled_session_factory, stub) as api:
        response = await api.get(f"/api/runs/{uuid.uuid4()}/status")
    assert (response.status_code, response.json()["code"]) == (404, "RUN_NOT_FOUND")
    assert not stub.used


async def test_malformed_run_id_is_400_validation_error(stack):
    for path in ALL_PATHS:
        for bad in ("not-a-uuid", "123"):
            response = await stack.get(bad, path)
            assert (response.status_code, response.json()["code"]) == (400, "VALIDATION_ERROR"), (path, bad)


# ------------------------------------------------ 3-7: empty history (no workflow)


async def test_empty_history_and_missing_final_output(stack):
    supervisor = await stack.supervisor()
    run_id = await insert_run(stack.factory, supervisor["id"], "E1")  # a row only: no workflow, no history

    timeline = await stack.get(run_id, "timeline")
    assert (timeline.status_code, timeline.json()) == (200, {"run_id": str(run_id), "entries": []})
    memory = await stack.get(run_id, "memory")
    assert (memory.status_code, memory.json()) == (200, {"run_id": str(run_id), "snapshots": []})
    actions = await stack.get(run_id, "actions")
    assert (actions.status_code, actions.json()) == (200, {"run_id": str(run_id), "actions": []})
    executions = await stack.get(run_id, "tool-executions")
    assert (executions.status_code, executions.json()) == (200, {"run_id": str(run_id), "tool_executions": []})

    final = await stack.get(run_id, "final-output")
    assert final.status_code == 200  # never 404 for an existing run
    body = final.json()
    assert body == {"run_id": str(run_id), "final_output": None, "created_at": None}
    assert body["final_output"] is None and body["created_at"] is None


# --------------------------------------------------- 8: populated via the real path


async def test_populated_history_through_the_real_path(stack):
    async with populated(stack, "P1") as run:
        run_id = run["id"]

        timeline = (await stack.get(run_id, "timeline")).json()
        assert timeline["run_id"] == run_id
        entries = timeline["entries"]
        assert entries
        for entry in entries:
            assert set(entry) == {"id", "run_id", "entry_type", "message", "created_at"}
            assert entry["run_id"] == run_id
        types = [e["entry_type"] for e in entries]
        assert "decision" in types
        assert sum(1 for e in entries if e["entry_type"] == "decision") == 3

        snapshots = (await stack.get(run_id, "memory")).json()["snapshots"]
        assert len(snapshots) == 3
        for snapshot in snapshots:
            assert set(snapshot) == {"id", "run_id", "memory", "created_at"}
            assert isinstance(snapshot["memory"], dict) and snapshot["memory"]

        actions = (await stack.get(run_id, "actions")).json()["actions"]
        assert len(actions) == 3
        assert sorted(a["action_type"] for a in actions) == sorted(SCRIPT)
        for action in actions:
            assert set(action) == {"id", "run_id", "action_type", "status", "reasoning", "created_at", "completed_at"}
            assert action["run_id"] == run_id
            assert action["status"] == "failed"  # business failure ("... not found")
            assert action["completed_at"] is not None

        executions = (await stack.get(run_id, "tool-executions")).json()["tool_executions"]
        assert len(executions) == 3
        assert sorted(e["tool_name"] for e in executions) == sorted(SCRIPT)
        for execution in executions:
            assert set(execution) == {
                "id",
                "action_id",
                "tool_name",
                "status",
                "input",
                "result",
                "error",
                "started_at",
                "completed_at",
            }
            assert (execution["status"], execution["result"]) == ("failed", None)
            assert execution["error"]
        # Each execution belongs to one of this run's actions, one execution per action.
        assert sorted(e["action_id"] for e in executions) == sorted(ids(actions))

        # Still open: no final output yet.
        final = await stack.get(run_id, "final-output")
        assert (final.status_code, final.json()["final_output"], final.json()["created_at"]) == (200, None, None)

        # The workflow's own state agrees with what was persisted.
        status = await stack.status("P1")
        assert status.reasoning_count == len(snapshots) == 3


# --------------------------------------------------------------- 9-12: ordering


async def test_history_is_ordered_oldest_first_on_the_real_path(stack):
    """Whatever the (possibly equal) workflow timestamps, the API order is (time, id) ASC."""
    async with populated(stack, "O1") as run:
        run_id = run["id"]
        timeline = (await stack.get(run_id, "timeline")).json()["entries"]
        memory = (await stack.get(run_id, "memory")).json()["snapshots"]
        actions = (await stack.get(run_id, "actions")).json()["actions"]
        executions = (await stack.get(run_id, "tool-executions")).json()["tool_executions"]

    async with stack.factory() as session:
        rid = uuid.UUID(run_id)
        action_rows = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
        action_ids = [a.id for a in action_rows]
        expected = {
            "timeline": sorted(
                (await session.execute(select(TimelineEntry).where(TimelineEntry.run_id == rid))).scalars(),
                key=lambda r: (r.created_at, r.id),
            ),
            "memory": sorted(
                (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars(),
                key=lambda r: (r.created_at, r.id),
            ),
            "actions": sorted(action_rows, key=lambda r: (r.created_at, r.id)),
            "executions": sorted(
                (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_(action_ids)))).scalars(),
                key=lambda r: (r.started_at, r.id),
            ),
        }
    assert ids(timeline) == [str(r.id) for r in expected["timeline"]]
    assert ids(memory) == [str(r.id) for r in expected["memory"]]
    assert ids(actions) == [str(r.id) for r in expected["actions"]]
    assert ids(executions) == [str(r.id) for r in expected["executions"]]
    assert len(timeline) > 3 and len(memory) == len(actions) == len(executions) == 3


async def test_timeline_and_memory_order_by_time_then_id(stack):
    supervisor = await stack.supervisor()
    run_id = await insert_run(stack.factory, supervisor["id"], "O2")
    # Inserted scrambled. Two rows share a timestamp, so the id decides between them.
    timeline = [(U(5), T(20)), (U(3), T(10)), (U(2), T(20)), (U(4), T(30)), (U(1), T(10))]
    memory = [(U(14), T(30)), (U(11), T(10)), (U(13), T(10)), (U(12), T(20))]
    async with stack.factory() as session:
        session.add_all(
            [TimelineEntry(id=i, run_id=run_id, entry_type="system", message=str(i), created_at=t) for i, t in timeline]
        )
        session.add_all([MemorySnapshot(id=i, run_id=run_id, memory={"n": i.int}, created_at=t) for i, t in memory])
        await session.commit()

    entries = (await stack.get(run_id, "timeline")).json()["entries"]
    assert ids(entries) == [str(U(n)) for n in (1, 3, 2, 5, 4)]  # T10:1,3  T20:2,5  T30:4
    snapshots = (await stack.get(run_id, "memory")).json()["snapshots"]
    assert ids(snapshots) == [str(U(n)) for n in (11, 13, 12, 14)]  # T10:11,13  T20:12  T30:14


async def test_action_order_by_time_then_id(stack):
    supervisor = await stack.supervisor()
    run_id = await insert_run(stack.factory, supervisor["id"], "O3")
    actions = [(U(23), T(20)), (U(22), T(20)), (U(21), T(30)), (U(24), T(10))]
    async with stack.factory() as session:
        session.add_all(
            [
                Action(id=i, run_id=run_id, action_type="get_order_status", status="completed", created_at=t)
                for i, t in actions
            ]
        )
        await session.commit()

    body = (await stack.get(run_id, "actions")).json()["actions"]
    assert ids(body) == [str(U(n)) for n in (24, 22, 23, 21)]


async def test_tool_executions_order_by_started_at_and_come_only_through_this_runs_actions(stack):
    supervisor = await stack.supervisor()
    run_a = await insert_run(stack.factory, supervisor["id"], "O4a")
    run_b = await insert_run(stack.factory, supervisor["id"], "O4b")
    action_a1, action_a2, action_b = U(31), U(32), U(33)
    async with stack.factory() as session:
        session.add_all(
            [
                Action(id=action_a1, run_id=run_a, action_type="get_order_status", status="completed", created_at=T(1)),
                Action(id=action_a2, run_id=run_a, action_type="get_order_status", status="completed", created_at=T(2)),
                Action(id=action_b, run_id=run_b, action_type="get_order_status", status="completed", created_at=T(1)),
            ]
        )
        await session.flush()
        # The later action's execution starts EARLIER: ordering follows started_at, not the action.
        session.add_all(
            [
                ToolExecution(id=U(45), action_id=action_a1, tool_name="get_order_status", status="success", started_at=T(40)),
                ToolExecution(id=U(43), action_id=action_a2, tool_name="get_order_status", status="success", started_at=T(20)),
                ToolExecution(id=U(42), action_id=action_a1, tool_name="get_order_status", status="success", started_at=T(20)),
                ToolExecution(id=U(44), action_id=action_b, tool_name="get_order_status", status="success", started_at=T(5)),
            ]
        )
        await session.commit()

    a = (await stack.get(run_a, "tool-executions")).json()
    assert a["run_id"] == str(run_a)
    assert ids(a["tool_executions"]) == [str(U(42)), str(U(43)), str(U(45))]  # T20:42,43  T40:45
    assert {e["action_id"] for e in a["tool_executions"]} <= {str(action_a1), str(action_a2)}

    b = (await stack.get(run_b, "tool-executions")).json()
    assert ids(b["tool_executions"]) == [str(U(44))]  # the other run's execution never leaks


# ----------------------------------------------------------------- 13: final output


async def test_final_output_after_terminal_completion(stack):
    supervisor = await stack.supervisor()  # delivered -> delivered, terminal
    run = await stack.run("F1", supervisor["id"])
    async with stack.worker():
        await eventually(lambda: _reasoned(stack, "F1", 1))
        await complete(stack, run["id"])
    assert (await stack.handle("F1").describe()).status == WorkflowExecutionStatus.COMPLETED

    response = await stack.get(run["id"], "final-output")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run["id"]
    assert body["created_at"] is not None
    output = body["final_output"]
    assert set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
    assert output["source"] == "llm"
    assert isinstance(output["summary"], str)
    for key in ("key_actions", "key_learnings", "recommendations"):
        assert isinstance(output[key], list)

    async with stack.factory() as session:
        stored = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == uuid.UUID(run["id"])))).scalar_one()
    assert output == stored.output

    # The terminal history is still observable on a completed run.
    timeline = (await stack.get(run["id"], "timeline")).json()["entries"]
    assert any("terminal" in e["message"] for e in timeline)


# ------------------------------------------------------------------- 14: live status


async def test_live_status_on_an_open_workflow(stack):
    async with populated(stack, "S1") as run:
        response = await stack.get(run["id"], "status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "order_id",
            "state",
            "order_status",
            "next_wake_at",
            "last_wake_reason",
            "reasoning_count",
            "interrupt_count",
            "events_received",
            "pending_events",
            "recent_events",
            "terminal_order_status_reached",
            "memory",
            "last_decision",
            "last_cycle_outcome",
            "run_instructions",
            "final_output_persisted",
        }
        direct = await stack.status("S1")
        assert body["order_id"] == f"{PREFIX}S1" == direct.order_id
        assert body["state"] == "sleeping" == direct.state
        assert body["reasoning_count"] == 3 == direct.reasoning_count
        assert body["events_received"] == 2 == direct.events_received
        assert body["last_wake_reason"] == "important_event"
        assert body["last_cycle_outcome"] == "completed"
        assert body["terminal_order_status_reached"] is False
        assert body["final_output_persisted"] is False
        assert body["next_wake_at"] is not None
        assert body["memory"] == direct.memory


# -------------------------------------------------------- 15: Temporal unavailable


async def test_status_returns_503_when_temporal_is_unavailable(pooled_session_factory):  # noqa: F811
    stub = _RecordingTemporal(error=_unavailable())
    async with api_for(pooled_session_factory, stub) as api:
        supervisor = (await api.post("/api/supervisors", json=supervisor_body())).json()
        run_id = await insert_run(pooled_session_factory, supervisor["id"], "U1")
        response = await api.get(f"/api/runs/{run_id}/status")
    assert (response.status_code, response.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE")
    assert stub.handles == [f"order-{PREFIX}U1"] and len(stub.queries) == 1


async def test_status_returns_503_when_no_temporal_client_is_connected(pooled_session_factory):  # noqa: F811
    async with api_for(pooled_session_factory, None) as api:
        supervisor = (await api.post("/api/supervisors", json=supervisor_body())).json()
        run_id = await insert_run(pooled_session_factory, supervisor["id"], "U2")
        response = await api.get(f"/api/runs/{run_id}/status")
        assert (response.status_code, response.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE")
        # Unknown run is still a 404: the lookup comes before the Temporal client is needed.
        missing = await api.get(f"/api/runs/{uuid.uuid4()}/status")
        assert (missing.status_code, missing.json()["code"]) == (404, "RUN_NOT_FOUND")


async def test_status_query_error_mapping(pooled_session_factory):  # noqa: F811
    cases = [
        (RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""), 409, "RUN_NOT_ACTIVE"),
        (RPCError("cancelled", RPCStatusCode.CANCELLED, b""), 503, "TEMPORAL_UNAVAILABLE"),
        (RPCError("deadline", RPCStatusCode.DEADLINE_EXCEEDED, b""), 503, "TEMPORAL_UNAVAILABLE"),
        (WorkflowQueryFailedError("query handler failed"), 503, "TEMPORAL_UNAVAILABLE"),  # H2
    ]
    for index, (error, status_code, code) in enumerate(cases):
        async with api_for(pooled_session_factory, _RecordingTemporal(error=error)) as api:
            supervisor = (await api.post("/api/supervisors", json=supervisor_body(name=f"{PREFIX}Map {index}"))).json()
            run_id = await insert_run(pooled_session_factory, supervisor["id"], f"M{index}")
            response = await api.get(f"/api/runs/{run_id}/status")
        assert (response.status_code, response.json()["code"]) == (status_code, code), error


# -------------------------------------------------- 16, 17: closed workflows (NOT_OPEN)


async def test_completed_workflow_status_is_409_not_a_stale_replay(stack):
    supervisor = await stack.supervisor()
    run = await stack.run("C1", supervisor["id"])
    async with stack.worker():
        await eventually(lambda: _reasoned(stack, "C1", 1))
        await complete(stack, run["id"])
    assert (await stack.handle("C1").describe()).status == WorkflowExecutionStatus.COMPLETED

    response = await stack.get(run["id"], "status")
    assert (response.status_code, response.json()["code"]) == (409, "RUN_NOT_ACTIVE")
    assert set(response.json()) == {"error", "code"}


async def test_terminated_workflow_status_is_409_not_stale_sleeping(stack):
    async with populated(stack, "T1") as run:
        assert (await stack.get(run["id"], "status")).status_code == 200  # open: live
        assert (await stack.api.post(f"/api/runs/{run['id']}/terminate")).status_code == 202
        assert (await stack.handle("T1").describe()).status == WorkflowExecutionStatus.TERMINATED

        response = await stack.get(run["id"], "status")
        assert (response.status_code, response.json()["code"]) == (409, "RUN_NOT_ACTIVE")

        # Its persisted history stays observable after termination.
        assert len((await stack.get(run["id"], "actions")).json()["actions"]) == 3


async def test_terminated_workflow_without_a_worker_is_409(stack):
    supervisor = await stack.supervisor()
    run = await stack.run("T2", supervisor["id"])  # started; never processed
    assert (await stack.api.post(f"/api/runs/{run['id']}/terminate")).status_code == 202
    response = await stack.get(run["id"], "status")
    assert (response.status_code, response.json()["code"]) == (409, "RUN_NOT_ACTIVE")


# ---------------------------------------------------------------- 18: failed run


async def test_failed_run_status_is_409_and_temporal_is_not_queried(pooled_session_factory):  # noqa: F811
    stub = _RecordingTemporal(error=AssertionError("Temporal must not be queried for a failed run"))
    async with api_for(pooled_session_factory, stub) as api:
        supervisor = (await api.post("/api/supervisors", json=supervisor_body())).json()
        run_id = await insert_run(pooled_session_factory, supervisor["id"], "X1", status="failed")
        response = await api.get(f"/api/runs/{run_id}/status")
        assert (response.status_code, response.json()["code"]) == (409, "RUN_NOT_ACTIVE")
        assert not stub.used  # no workflow handle, no query

        # A failed run is still observable in PostgreSQL, and the run row is untouched.
        assert (await api.get(f"/api/runs/{run_id}/timeline")).status_code == 200
        assert (await api.get(f"/api/runs/{run_id}/final-output")).status_code == 200
    async with pooled_session_factory() as session:
        assert (await session.get(Run, run_id)).status == "failed"


# ------------------------------------------------------------ 19: query timeout


async def test_no_worker_status_times_out_quickly_with_503(stack):
    supervisor = await stack.supervisor()
    run = await stack.run("W1", supervisor["id"])  # open workflow, no worker polling
    stack.app.state.status_query_timeout = timedelta(seconds=0.5)

    started = time.monotonic()
    response = await stack.get(run["id"], "status")
    elapsed = time.monotonic() - started

    assert (response.status_code, response.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE")
    assert elapsed < 4.0, f"status waited {elapsed:.1f}s; the injected 0.5s timeout was not used"
    assert (await stack.handle("W1").describe()).status == WorkflowExecutionStatus.RUNNING  # nothing changed


# ------------------------------------------------------------------ 20: no mutation


async def test_observation_never_mutates_anything(stack):
    async with populated(stack, "N1") as run:
        run_id = run["id"]
        async with stack.factory() as session:
            before_run = {c.name: getattr(await session.get(Run, uuid.UUID(run_id)), c.name) for c in Run.__table__.columns}
        before_counts = await table_counts(stack.factory)
        assert before_counts["runs"] == 1 and before_counts["timeline_entries"] > 0
        before_status = await stack.status("N1")

        first = {}
        for _ in range(3):
            for path in ALL_PATHS:
                response = await stack.get(run_id, path)
                assert response.status_code == 200, (path, response.text)
                first.setdefault(path, response.json())
                assert response.json() == first[path], path  # repeated reads are identical

        async with stack.factory() as session:
            after_run = {c.name: getattr(await session.get(Run, uuid.UUID(run_id)), c.name) for c in Run.__table__.columns}
        assert await table_counts(stack.factory) == before_counts
        assert after_run == before_run
        after_status = await stack.status("N1")
        assert after_status == before_status  # no Signal, no wake, no reasoning
        assert (await stack.handle("N1").describe()).status == WorkflowExecutionStatus.RUNNING


# ------------------------------------------- 21: PostgreSQL endpoints without Temporal


async def test_history_endpoints_work_without_a_temporal_client(stack):
    async with populated(stack, "D1") as run:
        expected = {path: (await stack.get(run["id"], path)).json() for path in HISTORY_PATHS}

    async with api_for(stack.factory, None) as api:
        for path in HISTORY_PATHS:
            response = await api.get(f"/api/runs/{run['id']}/{path}")
            assert response.status_code == 200, path
            assert response.json() == expected[path], path
        assert len(expected["actions"]["actions"]) == 3

        # Only /status needs Temporal.
        status = await api.get(f"/api/runs/{run['id']}/status")
        assert (status.status_code, status.json()["code"]) == (503, "TEMPORAL_UNAVAILABLE")
