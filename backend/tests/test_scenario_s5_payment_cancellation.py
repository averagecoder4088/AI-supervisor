"""S5 - Payment failure and cancellation, end to end (Step 8).

The simulator (``app.simulation``) plays the external world: it fails the payment and later
cancels the order in the MOCK tables, and submits each event through the real FastAPI event
endpoint. The supervisor can only inspect the order (``get_order_status``); there is no
cancel tool, so the world does the cancelling. Every supervisor-side row is produced by the
real API, workflow and Activities against real PostgreSQL and Temporal's test server. Only
the LLM is scripted (three successful calls).

``order_cancelled`` is both an important event and a terminal status. Terminal precedence
means it is recorded, updates the order status, and the run goes straight to final output
with NO further reasoning cycle. No scheduled wake is involved, so no time is skipped.
"""

import asyncio
import json
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment

from app.db.mock_models import MockCustomerMessage, MockOrder, MockShipment
from app.db.models import Action, Event, FinalOutput, MemorySnapshot, Run, ToolExecution
from app.llm.fake import FakeLLMClient
from app.llm.schemas import FINAL_OUTPUT_SCHEMA_NAME, REASONING_SCHEMA_NAME
from app.main import create_app
from app.simulation.s2_delayed_shipment import S2_ORDER_STATUS_BY_EVENT
from app.simulation.s5_payment_cancellation import (
    S5_CANCELLATION_REASON,
    S5_MEMORY_SUMMARIES,
    S5_ORDER_STATUS_BY_EVENT,
    S5_PAYMENT_FAILURE_REASON,
    s5_llm_script,
    s5_supervisor_body,
)
from app.simulation.world import (
    EVENT_ORDER_STATUS,
    EVENT_ORDER_STATUS_WITH_CANCELLATION,
    EVENT_ORDER_STATUS_WITH_DELAY,
    EventNotAccepted,
    ExternalWorld,
    InvalidTransition,
)
from app.temporal.constants import order_workflow_id
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from app.tools.registry import TOOL_NAMES, TOOL_SPECS
from tests.api_support import PREFIX, pooled_session_factory  # noqa: F401

pytestmark = pytest.mark.asyncio

ORDER_ID = f"{PREFIX}S5-001"
SHIPPED_ORDER_ID = f"{PREFIX}S5-002"
ALL_EVENTS = ["order_created", "payment_failed", "order_cancelled"]
FOUR_TOOLS = {"get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"}


# ------------------------------------------------------------------ fixtures


async def _delete_mock_orders(factory) -> None:
    # Shipments and messages cascade from mock_orders. The generic API cleanup does not
    # know the mock tables, so S5 cleans them itself (before and after every test).
    async with factory() as session:
        await session.execute(delete(MockOrder).where(MockOrder.order_id.like(f"{PREFIX}%")))
        await session.commit()


@pytest_asyncio.fixture
async def factory(pooled_session_factory):  # noqa: F811
    await _delete_mock_orders(pooled_session_factory)
    try:
        yield pooled_session_factory
    finally:
        await _delete_mock_orders(pooled_session_factory)


class S5:
    def __init__(self, env: WorkflowEnvironment, factory, api: httpx.AsyncClient, llm: FakeLLMClient) -> None:
        self.env = env
        self.factory = factory
        self.api = api
        self.llm = llm
        self.world = ExternalWorld(factory, api)


@pytest_asyncio.fixture
async def s5(factory) -> AsyncIterator[S5]:
    llm = FakeLLMClient(s5_llm_script())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=factory, connect_temporal=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            async with create_worker(env.client, session_factory=factory, llm_client=llm):
                yield S5(env, factory, api, llm)


# ------------------------------------------------------------------- helpers


async def eventually(check: Callable, attempts: int = 500):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def live_status(s5: S5, run_id: str) -> Dict[str, Any]:
    response = await s5.api.get(f"/api/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()


async def settled(s5: S5, run_id: str, cycles: int) -> Dict[str, Any]:
    """Wait until exactly ``cycles`` COMPLETED reasoning cycles finished and the workflow is sleeping."""

    async def check():
        response = await s5.api.get(f"/api/runs/{run_id}/status")
        if response.status_code != 200:
            return None
        status = response.json()
        return status if status["reasoning_count"] == cycles and status["state"] == "sleeping" else None

    return await eventually(check)


async def recorded(s5: S5, run_id: str, event_type: str, *, wakes: bool) -> None:
    """Wait until the WORKFLOW has persisted the event and judged whether it wakes the supervisor.

    The label reflects the wake POLICY, so ``order_cancelled`` (important) says it wakes the
    supervisor even though, being terminal, it never does (existing wording, unchanged).
    """
    verdict = "important: wakes the supervisor" if wakes else "recorded; does not wake the supervisor"
    expected = f"Event received: {event_type} ({verdict})"

    async def check():
        entries = (await s5.api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
        return any(e["message"].startswith(expected) for e in entries)

    await eventually(check)


async def mock_state(factory, order_id: str = ORDER_ID) -> Dict[str, List[Dict[str, Any]]]:
    """Every column of every mock row for one order (updated_at proves nothing was written)."""

    def rows(items):
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in items]

    async with factory() as session:
        out = {}
        for name, model in (
            ("orders", MockOrder),
            ("shipments", MockShipment),
            ("messages", MockCustomerMessage),
        ):
            query = select(model).where(model.order_id == order_id).order_by(model.created_at, model.id)
            out[name] = rows((await session.execute(query)).scalars().all())
        return out


async def order_status_in_db(factory, order_id: str = ORDER_ID) -> str:
    async with factory() as session:
        return (await session.execute(select(MockOrder.status).where(MockOrder.order_id == order_id))).scalar_one()


async def run_status(factory, run_id: str) -> str:
    async with factory() as session:
        return (await session.get(Run, uuid.UUID(run_id))).status


def prompt(s5: S5, call_index: int) -> Dict[str, Any]:
    return json.loads(s5.llm.calls[call_index]["user_prompt"])


def spy_on_events(s5: S5) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Record the mock state seen at the moment each event is submitted (commit-before-emit)."""
    seen: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    original = s5.world._emit

    async def spy(run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        seen[event_type] = await mock_state(s5.factory)
        await original(run_id, event_type, payload)

    s5.world._emit = spy  # type: ignore[assignment]
    return seen


# ------------------------------------------------------------ small S5 tests


async def test_s5_mapping_matches_the_world_and_the_supervisor_configuration():
    assert S5_ORDER_STATUS_BY_EVENT == EVENT_ORDER_STATUS_WITH_CANCELLATION
    body = s5_supervisor_body("x")
    assert body["order_status_by_event"] == EVENT_ORDER_STATUS_WITH_CANCELLATION
    assert body["order_status_by_event"]["payment_failed"] == "payment_failed"
    assert body["order_status_by_event"]["order_cancelled"] == "cancelled"
    # order_cancelled is important AND terminal: terminal precedence keeps it from waking reasoning.
    assert {"payment_failed", "order_cancelled"} <= set(body["wake_policy"]["important_event_types"])
    assert "cancelled" in body["terminal_order_statuses"]
    # The earlier scenarios' tables are untouched.
    assert "payment_failed" not in EVENT_ORDER_STATUS_WITH_DELAY and "order_cancelled" not in EVENT_ORDER_STATUS
    assert "payment_failed" not in S2_ORDER_STATUS_BY_EVENT


async def test_fail_payment_and_cancel_order_guards(factory):
    # No Temporal client: every event is refused (503), but the world change is committed first.
    app = create_app(temporal_client=None, session_factory=factory, connect_temporal=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        world = ExternalWorld(factory, api)
        run_id = str(uuid.uuid4())

        async def applied(step) -> None:
            with pytest.raises(EventNotAccepted):
                await step

        with pytest.raises(InvalidTransition):
            await world.fail_payment(run_id, ORDER_ID, "x")  # no such order
        with pytest.raises(InvalidTransition):
            await world.cancel_order(run_id, ORDER_ID, "x")  # no such order

        # fail_payment works only from created, and only once.
        await world.place_order(ORDER_ID)
        await applied(world.fail_payment(run_id, ORDER_ID, S5_PAYMENT_FAILURE_REASON))
        assert await order_status_in_db(factory) == "payment_failed"
        with pytest.raises(InvalidTransition):
            await world.fail_payment(run_id, ORDER_ID, "again")

        # cancel_order works from payment_failed, changes only the order, and only once.
        before = await mock_state(factory)
        await applied(world.cancel_order(run_id, ORDER_ID, S5_CANCELLATION_REASON))
        after = await mock_state(factory)
        assert after["orders"][0]["status"] == "cancelled"
        assert after["shipments"] == [] and after["messages"] == []
        assert before["orders"][0]["status"] == "payment_failed"
        with pytest.raises(InvalidTransition):
            await world.cancel_order(run_id, ORDER_ID, "again")  # second cancellation is rejected
        with pytest.raises(InvalidTransition):
            await world.fail_payment(run_id, ORDER_ID, "late")  # a cancelled order cannot fail payment

        # cancel_order is rejected once a shipment exists, and nothing changes.
        await world.place_order(SHIPPED_ORDER_ID)
        await applied(world.confirm_payment(run_id, SHIPPED_ORDER_ID))
        await applied(world.create_shipment(run_id, SHIPPED_ORDER_ID))
        with pytest.raises(InvalidTransition):
            await world.cancel_order(run_id, SHIPPED_ORDER_ID, "too late")
        assert await order_status_in_db(factory, SHIPPED_ORDER_ID) == "shipped"
        assert len((await mock_state(factory, SHIPPED_ORDER_ID))["shipments"]) == 1


async def test_the_supervisor_has_exactly_the_four_existing_tools_and_no_cancel_tool():
    assert set(TOOL_NAMES) == set(TOOL_SPECS) == FOUR_TOOLS
    assert not any("cancel" in name for name in TOOL_NAMES)  # the supervisor cannot cancel an order
    assert set(s5_supervisor_body("x")["enabled_tools"]) == FOUR_TOOLS


# ------------------------------------------------------------------- S5 flow


async def test_s5_payment_failure_and_cancellation(s5):
    api, world, llm, factory, env = s5.api, s5.world, s5.llm, s5.factory, s5.env
    at_emit = spy_on_events(s5)

    created = await api.post("/api/supervisors", json=s5_supervisor_body(f"{PREFIX}S5 Payment Cancellation"))
    assert created.status_code == 201, created.text
    supervisor_id = created.json()["id"]

    # 1-2. The world places the order; the run starts through the API.
    await world.place_order(ORDER_ID)
    placed = await mock_state(factory)
    assert [(o["order_id"], o["status"], o["customer_id"]) for o in placed["orders"]] == [
        (ORDER_ID, "created", f"CUST-{ORDER_ID}")
    ]
    assert placed["shipments"] == [] and placed["messages"] == []
    response = await api.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": supervisor_id})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]

    # 3. Cycle 1 (workflow_start, no tool) settles completely before order_created.
    start = await settled(s5, run_id, 1)
    assert start["last_wake_reason"] == "workflow_start" and start["memory"]["situation_summary"] == S5_MEMORY_SUMMARIES[0]
    assert start["memory"]["last_action"] is None and len(llm.calls) == 1

    # 4. order_created: a routine notification (no world change, no wake).
    await world.order_created(run_id, ORDER_ID)
    await recorded(s5, run_id, "order_created", wakes=False)
    routine = await live_status(s5, run_id)
    assert (routine["reasoning_count"], routine["order_status"], len(llm.calls)) == (1, "created", 1)
    assert await mock_state(factory) == placed

    # 5-6. The payment fails in the world FIRST (committed), then the event is submitted.
    await world.fail_payment(run_id, ORDER_ID, S5_PAYMENT_FAILURE_REASON)
    failed_world = at_emit["payment_failed"]  # state at the moment the event was submitted
    assert [(o["order_id"], o["status"]) for o in failed_world["orders"]] == [(ORDER_ID, "payment_failed")]
    assert failed_world["shipments"] == [] and failed_world["messages"] == []
    await recorded(s5, run_id, "payment_failed", wakes=True)  # persisted by the workflow's Activity

    # 7-9. It wakes exactly ONE important-event reasoning cycle; get_order_status observes the world's state.
    inspected = await settled(s5, run_id, 2)
    assert inspected["last_wake_reason"] == "important_event" and inspected["last_cycle_outcome"] == "completed"
    assert inspected["order_status"] == "payment_failed"
    assert inspected["memory"]["situation_summary"] == S5_MEMORY_SUMMARIES[1]
    assert inspected["memory"]["last_action"] == "get_order_status: succeeded"
    assert inspected["last_decision"]["executed_tool"] == "get_order_status"
    assert len(llm.calls) == 2
    failure_prompt = prompt(s5, 1)
    assert failure_prompt["wake_reason"] == "important_event" and failure_prompt["order_status"] == "payment_failed"
    assert [e["event_type"] for e in failure_prompt["new_events"]] == ["order_created", "payment_failed"]
    assert failure_prompt["new_events"][-1]["payload"] == {"reason": S5_PAYMENT_FAILURE_REASON}
    assert failure_prompt["memory"]["situation_summary"] == S5_MEMORY_SUMMARIES[0]  # cycle 1's memory
    (execution,) = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert (execution["tool_name"], execution["status"], execution["error"]) == ("get_order_status", "success", None)
    assert execution["result"] == {"order_id": ORDER_ID, "status": "payment_failed"}
    assert execution["result"]["status"] == failure_prompt["order_status"]  # the two independent views agree
    # 10. get_order_status is read-only: the mock state is byte-identical to what the world left.
    assert await mock_state(factory) == failed_world

    # 11-12. The WORLD cancels (the supervisor cannot): committed FIRST, then order_cancelled is submitted.
    await world.cancel_order(run_id, ORDER_ID, S5_CANCELLATION_REASON)
    cancelled_world = at_emit["order_cancelled"]
    assert [(o["order_id"], o["status"]) for o in cancelled_world["orders"]] == [(ORDER_ID, "cancelled")]
    assert cancelled_world["shipments"] == [] and cancelled_world["messages"] == []

    # 13-17. Terminal precedence: no third cycle; final output -> persist -> run completed -> workflow closes.
    await eventually(lambda: _completed(factory, run_id))
    handle = env.client.get_workflow_handle_for(OrderWorkflow.run, order_workflow_id(ORDER_ID))
    result = await handle.result()  # workflow closure is the synchronization point
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert result.reasoning_count == 2  # order_cancelled never reached a reasoning cycle
    assert result.last_wake_reason == "important_event"  # still the payment_failed cycle's reason
    assert (result.terminal_order_status_reached, result.final_output_persisted) == (True, True)
    assert result.state == "terminal" and result.order_status == "cancelled" and result.events_received == 3
    assert [c["schema_name"] for c in llm.calls] == [REASONING_SCHEMA_NAME] * 2 + [FINAL_OUTPUT_SCHEMA_NAME]
    assert len(llm.calls) == 3  # exactly: reasoning, reasoning, final_output - no third reasoning call

    await assert_final_mock_state(factory)
    await assert_supervisor_history(s5, run_id)
    await assert_observable_through_the_api(s5, run_id)


async def _completed(factory, run_id: str) -> bool:
    return await run_status(factory, run_id) == "completed"


async def assert_final_mock_state(factory) -> None:
    final = await mock_state(factory)
    (order,) = final["orders"]
    assert (order["order_id"], order["status"]) == (ORDER_ID, "cancelled")
    assert final["shipments"] == [] and final["messages"] == []  # no unintended side effects


async def assert_supervisor_history(s5: S5, run_id: str) -> None:
    rid = uuid.UUID(run_id)
    async with s5.factory() as session:
        run = await session.get(Run, rid)
        assert run.status == "completed" and run.order_status == "cancelled" and run.completed_at is not None

        events = (
            (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at, Event.id)))
            .scalars()
            .all()
        )
        assert [e.event_type for e in events] == ALL_EVENTS
        assert events[1].payload == {"reason": S5_PAYMENT_FAILURE_REASON}
        assert events[2].payload == {"reason": S5_CANCELLATION_REASON}

        actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
        assert [(a.action_type, a.status) for a in actions] == [("get_order_status", "completed")]
        executions = (
            (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([a.id for a in actions]))))
            .scalars()
            .all()
        )
        assert [(e.tool_name, e.status) for e in executions] == [("get_order_status", "success")]
        snapshots = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
        assert len(snapshots) == 2
        outputs = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
        assert len(outputs) == 1 and outputs[0].output["source"] == "llm"


async def assert_observable_through_the_api(s5: S5, run_id: str) -> None:
    api = s5.api
    run = (await api.get(f"/api/runs/{run_id}")).json()
    assert (run["status"], run["order_status"]) == ("completed", "cancelled") and run["completed_at"] is not None

    final = (await api.get(f"/api/runs/{run_id}/final-output")).json()
    output = final["final_output"]
    assert final["created_at"] is not None
    assert set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
    assert output["source"] == "llm" and len(output["key_actions"]) == 1

    # The final-output LLM call received the real history; order_cancelled was only ever PENDING.
    final_prompt = prompt(s5, 2)
    assert s5.llm.calls[2]["schema_name"] == FINAL_OUTPUT_SCHEMA_NAME
    assert final_prompt["final_order_status"] == "cancelled"
    assert (final_prompt["events_received"], final_prompt["reasoning_count"]) == (3, 2)
    assert [(a["tool"], a["success"]) for a in final_prompt["action_log"]] == [("get_order_status", True)]
    assert final_prompt["action_log"][0]["output"] == {"order_id": ORDER_ID, "status": "payment_failed"}
    assert [e["event_type"] for e in final_prompt["recent_events"]] == ALL_EVENTS

    (action,) = (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"]
    assert (action["action_type"], action["status"]) == ("get_order_status", "completed")
    (execution,) = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert execution["input"] == {} and execution["result"] == {"order_id": ORDER_ID, "status": "payment_failed"}
    snapshots = (await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]
    assert [s["memory"]["situation_summary"] for s in snapshots] == S5_MEMORY_SUMMARIES

    entries = (await api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
    by_type: Dict[str, List[str]] = {}
    for entry in entries:
        by_type.setdefault(entry["entry_type"], []).append(entry["message"])
    assert {k: len(v) for k, v in by_type.items()} == {"event": 3, "decision": 2, "action": 1, "system": 1}
    assert len(entries) == 7
    events_by_name = {m.split(" (")[0]: m for m in by_type["event"]}
    assert "recorded; does not wake the supervisor" in events_by_name["Event received: order_created"]
    assert "important: wakes the supervisor" in events_by_name["Event received: payment_failed"]
    # The existing label says "important: wakes the supervisor", yet no cycle ran for it (terminal precedence).
    cancelled = events_by_name["Event received: order_cancelled"]
    assert cancelled.endswith("(important: wakes the supervisor); order status -> cancelled")
    decisions = " ".join(by_type["decision"])
    assert "[tool: none]" in decisions and "[tool: get_order_status]" in decisions and len(by_type["decision"]) == 2
    assert "terminal status 'cancelled'" in by_type["system"][0]

    closed = await api.get(f"/api/runs/{run_id}/status")  # the workflow is closed
    assert (closed.status_code, closed.json()["code"]) == (409, "RUN_NOT_ACTIVE")
