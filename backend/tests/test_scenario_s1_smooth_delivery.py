"""S1 - Smooth delivery, end to end (Step 8).

The simulator (``app.simulation``) plays the external world: it changes the MOCK
operational tables and then submits each event through the real FastAPI event
endpoint. Everything supervisor-side (events, timeline, actions, tool
executions, memory, final output, run status) is produced by the real API,
workflow and Activities against real PostgreSQL and Temporal's time-skipping
test server. Only the LLM is scripted (FakeLLMClient); the 60-minute scheduled
wake is crossed with ``env.sleep`` on the test server, never in real time.
"""

import asyncio
import json
import uuid
from datetime import timedelta
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
from app.simulation.s1_smooth_delivery import S1_ORDER_STATUS_BY_EVENT, s1_llm_script, s1_supervisor_body
from app.simulation.world import EVENT_ORDER_STATUS, EventNotAccepted, ExternalWorld, InvalidTransition, shipment_id_for
from app.temporal.constants import order_workflow_id
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.api_support import PREFIX, pooled_session_factory  # noqa: F401

pytestmark = pytest.mark.asyncio

ORDER_ID = f"{PREFIX}S1-001"
ROUTINE_EVENTS = ("order_created", "payment_confirmed", "shipment_created")


# ------------------------------------------------------------------ fixtures


async def _delete_mock_orders(factory) -> None:
    # Shipments and messages cascade from mock_orders. The generic API cleanup does not
    # know the mock tables, so S1 cleans them itself (before and after every test).
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


class S1:
    def __init__(self, env: WorkflowEnvironment, factory, api: httpx.AsyncClient, llm: FakeLLMClient) -> None:
        self.env = env
        self.factory = factory
        self.api = api
        self.llm = llm
        self.world = ExternalWorld(factory, api)


@pytest_asyncio.fixture
async def s1(factory) -> AsyncIterator[S1]:
    llm = FakeLLMClient(s1_llm_script())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=factory, connect_temporal=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            async with create_worker(env.client, session_factory=factory, llm_client=llm):
                yield S1(env, factory, api, llm)


# ------------------------------------------------------------------- helpers


async def eventually(check: Callable, attempts: int = 500):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def live_status(s1: S1, run_id: str) -> Dict[str, Any]:
    response = await s1.api.get(f"/api/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()


async def settled(s1: S1, run_id: str, cycles: int) -> Dict[str, Any]:
    """Wait until exactly ``cycles`` reasoning cycles finished and the workflow is sleeping."""

    async def check():
        response = await s1.api.get(f"/api/runs/{run_id}/status")
        if response.status_code != 200:
            return None
        status = response.json()
        return status if status["reasoning_count"] == cycles and status["state"] == "sleeping" else None

    return await eventually(check)


async def recorded(s1: S1, run_id: str, event_type: str) -> None:
    """Wait until the WORKFLOW has persisted the event and said it does not wake the supervisor."""
    expected = f"Event received: {event_type} (recorded; does not wake the supervisor)"

    async def check():
        entries = (await s1.api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
        return any(e["message"].startswith(expected) for e in entries)

    await eventually(check)


async def assert_routine(s1: S1, run_id: str, event_type: str, scheduled_at: str, expected_status: str) -> None:
    """A routine event is recorded, does not wake reasoning, and leaves the timer alone."""
    await recorded(s1, run_id, event_type)
    status = await live_status(s1, run_id)
    assert status["reasoning_count"] == 1 and status["state"] == "sleeping"
    assert status["next_wake_at"] == scheduled_at
    assert status["order_status"] == expected_status  # the supervisor's view ...
    assert await mock_order_status(s1.factory) == expected_status  # ... agrees with the world
    assert len(s1.llm.calls) == 1


async def mock_state(factory) -> Dict[str, List[Dict[str, Any]]]:
    """Every column of every mock row for the S1 order (updated_at proves nothing was written)."""

    def rows(items):
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in items]

    async with factory() as session:
        out = {}
        for name, model in (
            ("orders", MockOrder),
            ("shipments", MockShipment),
            ("messages", MockCustomerMessage),
        ):
            query = select(model).where(model.order_id == ORDER_ID).order_by(model.created_at, model.id)
            out[name] = rows((await session.execute(query)).scalars().all())
        return out


async def mock_order_status(factory) -> str:
    async with factory() as session:
        return (await session.execute(select(MockOrder.status).where(MockOrder.order_id == ORDER_ID))).scalar_one()


async def run_status(factory, run_id: str) -> str:
    async with factory() as session:
        return (await session.get(Run, uuid.UUID(run_id))).status


# ------------------------------------------------------------ small S1 tests


async def test_simulator_transitions_agree_with_the_supervisors_order_status_mapping():
    assert EVENT_ORDER_STATUS == S1_ORDER_STATUS_BY_EVENT
    assert s1_supervisor_body("x")["order_status_by_event"] == EVENT_ORDER_STATUS


async def test_a_non_202_event_response_fails_loudly(factory):
    # No Temporal client at all: the event endpoint answers 503, never 202.
    app = create_app(temporal_client=None, session_factory=factory, connect_temporal=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        world = ExternalWorld(factory, api)
        await world.place_order(ORDER_ID)
        with pytest.raises(EventNotAccepted, match="503"):
            await world.order_created(str(uuid.uuid4()), ORDER_ID)
        with pytest.raises(EventNotAccepted):
            await world.confirm_payment(str(uuid.uuid4()), ORDER_ID)
    # The world changed (the payment was applied) even though the event was refused.
    assert await mock_order_status(factory) == "payment_confirmed"


async def test_the_world_rejects_impossible_transitions(factory):
    app = create_app(temporal_client=None, session_factory=factory, connect_temporal=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        world = ExternalWorld(factory, api)
        with pytest.raises(InvalidTransition):
            await world.confirm_payment("x", ORDER_ID)  # no such order
        await world.place_order(ORDER_ID)
        with pytest.raises(InvalidTransition):
            await world.create_shipment("x", ORDER_ID)  # payment not confirmed yet
        with pytest.raises(InvalidTransition):
            await world.deliver("x", ORDER_ID)  # nothing shipped
    assert (await mock_state(factory))["shipments"] == []


# ------------------------------------------------------------------- S1 flow


async def test_s1_smooth_delivery(s1):
    api, world, llm, factory, env = s1.api, s1.world, s1.llm, s1.factory, s1.env

    # Supervisor through the existing API.
    created = await api.post("/api/supervisors", json=s1_supervisor_body(f"{PREFIX}S1 Smooth Delivery"))
    assert created.status_code == 201, created.text
    supervisor_id = created.json()["id"]

    # 1. External customer action: the minimum mock operational state. No event yet.
    await world.place_order(ORDER_ID)
    placed = await mock_state(factory)
    assert [(o["order_id"], o["status"], o["customer_id"]) for o in placed["orders"]] == [
        (ORDER_ID, "created", f"CUST-{ORDER_ID}")
    ]
    assert placed["shipments"] == [] and placed["messages"] == []

    # 2. Create the run through the existing API. runs.order_status is NOT seeded.
    response = await api.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": supervisor_id})
    assert response.status_code == 201, response.text
    run = response.json()
    run_id = run["id"]
    assert (run["status"], run["order_status"]) == ("running", None)

    # 3. The workflow-start reasoning cycle settles completely before order_created.
    start = await settled(s1, run_id, 1)
    assert start["last_wake_reason"] == "workflow_start"
    assert start["order_status"] is None and start["events_received"] == 0
    assert start["memory"]["situation_summary"] == "Order placed; awaiting payment"  # scripted, not the simulator
    assert start["memory"]["open_concerns"] == ["Payment not yet confirmed"]
    assert (start["memory"]["last_action"], start["memory"]["cycle_count"]) == ("get_order_status: succeeded", 1)
    scheduled_at = start["next_wake_at"]
    assert scheduled_at is not None
    assert len(llm.calls) == 1
    assert await mock_state(factory) == placed  # get_order_status is read-only
    assert (await api.get(f"/api/runs/{run_id}")).json()["order_status"] is None

    # 4. order_created: the notification of step 1. It does not create the order again.
    await world.order_created(run_id, ORDER_ID)
    await assert_routine(s1, run_id, "order_created", scheduled_at, "created")
    assert await mock_state(factory) == placed

    # 5. payment_confirmed: mock order created -> payment_confirmed, then the event.
    await world.confirm_payment(run_id, ORDER_ID)
    await assert_routine(s1, run_id, "payment_confirmed", scheduled_at, "payment_confirmed")

    # 6. shipment_created: shipment row + order -> shipped in one transaction, then the event.
    await world.create_shipment(run_id, ORDER_ID)
    await assert_routine(s1, run_id, "shipment_created", scheduled_at, "shipped")
    shipped = await mock_state(factory)
    (order,) = shipped["orders"]
    (shipment,) = shipped["shipments"]
    assert order["status"] == "shipped"
    assert (shipment["shipment_id"], shipment["status"], shipment["tracking_number"]) == (
        shipment_id_for(ORDER_ID),
        "created",
        f"TRK-{shipment_id_for(ORDER_ID)}",
    )
    assert (shipment["delay_reason"], shipment["escalated"]) == (None, False)
    assert (await live_status(s1, run_id))["events_received"] == 3

    # 7. Time-skip toward the scheduled wake. Not yet due at 30 minutes: still one cycle.
    await env.sleep(timedelta(minutes=30))
    assert (await live_status(s1, run_id))["reasoning_count"] == 1
    assert len(llm.calls) == 1
    await env.sleep(timedelta(minutes=31))

    # 8. The scheduled wake causes exactly one more reasoning cycle: get_shipment_status.
    scheduled = await settled(s1, run_id, 2)
    assert scheduled["last_wake_reason"] == "scheduled_wakeup"
    assert scheduled["memory"]["situation_summary"] == "Payment confirmed and shipment created; no delay observed"
    assert scheduled["memory"]["open_concerns"] == []
    assert (scheduled["memory"]["last_action"], scheduled["memory"]["cycle_count"]) == (
        "get_shipment_status: succeeded",
        2,
    )
    assert len(llm.calls) == 2
    assert await mock_state(factory) == shipped  # get_shipment_status is read-only

    # The routine events reached reasoning only now, as new events of the scheduled cycle,
    # together with the memory the FIRST cycle wrote (memory -> context loop).
    start_prompt = json.loads(llm.calls[0]["user_prompt"])
    assert (start_prompt["wake_reason"], start_prompt["order_status"], start_prompt["new_events"]) == (
        "workflow_start",
        None,
        [],
    )
    scheduled_prompt = json.loads(llm.calls[1]["user_prompt"])
    assert scheduled_prompt["wake_reason"] == "scheduled_wakeup"
    assert [e["event_type"] for e in scheduled_prompt["new_events"]] == list(ROUTINE_EVENTS)
    assert scheduled_prompt["memory"]["situation_summary"] == "Order placed; awaiting payment"

    # 9. delivered: shipment + order -> delivered in one transaction, then the event.
    await world.deliver(run_id, ORDER_ID)

    # 10. Terminal: no reasoning pass; final output -> persist -> run completed -> workflow ends.
    await eventually(lambda: _completed(factory, run_id))
    # complete_run commits the DB state BEFORE the workflow finishes returning, so workflow
    # closure (result()) is the synchronization point, not the DB status.
    handle = env.client.get_workflow_handle_for(OrderWorkflow.run, order_workflow_id(ORDER_ID))
    result = await handle.result()
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert result.reasoning_count == 2  # nothing reasoned after delivered
    assert (result.terminal_order_status_reached, result.final_output_persisted) == (True, True)
    assert result.order_status == "delivered" and result.events_received == 4

    # Exactly three LLM calls: two reasoning + one final output.
    assert [c["schema_name"] for c in llm.calls] == [
        REASONING_SCHEMA_NAME,
        REASONING_SCHEMA_NAME,
        FINAL_OUTPUT_SCHEMA_NAME,
    ]

    await assert_final_mock_state(factory)
    await assert_supervisor_history(s1, run_id)
    await assert_observable_through_the_api(s1, run_id)


async def _completed(factory, run_id: str) -> bool:
    return await run_status(factory, run_id) == "completed"


async def assert_final_mock_state(factory) -> None:
    final = await mock_state(factory)
    (order,) = final["orders"]
    (shipment,) = final["shipments"]
    assert order["status"] == "delivered"
    assert (shipment["status"], shipment["delay_reason"], shipment["escalated"]) == ("delivered", None, False)
    assert final["messages"] == []


async def assert_supervisor_history(s1: S1, run_id: str) -> None:
    rid = uuid.UUID(run_id)
    async with s1.factory() as session:
        run = await session.get(Run, rid)
        assert run.status == "completed"
        assert run.order_status == "delivered"
        assert run.completed_at is not None

        events = (
            (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at, Event.id)))
            .scalars()
            .all()
        )
        assert [e.event_type for e in events] == [*ROUTINE_EVENTS, "delivered"]

        actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
        assert sorted((a.action_type, a.status) for a in actions) == [
            ("get_order_status", "completed"),
            ("get_shipment_status", "completed"),
        ]
        executions = (
            (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([a.id for a in actions]))))
            .scalars()
            .all()
        )
        assert sorted((e.tool_name, e.status) for e in executions) == [
            ("get_order_status", "success"),
            ("get_shipment_status", "success"),
        ]
        snapshots = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
        assert len(snapshots) == 2
        outputs = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
        assert len(outputs) == 1 and outputs[0].output["source"] == "llm"


async def assert_observable_through_the_api(s1: S1, run_id: str) -> None:
    api = s1.api
    run = (await api.get(f"/api/runs/{run_id}")).json()
    assert (run["status"], run["order_status"]) == ("completed", "delivered")
    assert run["completed_at"] is not None

    final = (await api.get(f"/api/runs/{run_id}/final-output")).json()
    output = final["final_output"]
    assert final["created_at"] is not None
    assert set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
    assert output["source"] == "llm"
    assert output["summary"] == "Order delivered without any intervention."

    actions = (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"]
    assert [(a["action_type"], a["status"]) for a in actions] == [
        ("get_order_status", "completed"),
        ("get_shipment_status", "completed"),
    ]
    executions = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert [(e["tool_name"], e["status"], e["error"]) for e in executions] == [
        ("get_order_status", "success", None),
        ("get_shipment_status", "success", None),
    ]
    assert executions[0]["result"] == {"order_id": ORDER_ID, "status": "created"}
    assert executions[1]["result"] == {
        "order_id": ORDER_ID,
        "shipment_id": shipment_id_for(ORDER_ID),
        "status": "created",
        "tracking_number": f"TRK-{shipment_id_for(ORDER_ID)}",
        "delay_reason": None,
        "escalated": False,
    }

    snapshots = (await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]
    assert [s["memory"]["situation_summary"] for s in snapshots] == [
        "Order placed; awaiting payment",
        "Payment confirmed and shipment created; no delay observed",
    ]

    entries = (await api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
    by_type: Dict[str, List[str]] = {}
    for entry in entries:
        by_type.setdefault(entry["entry_type"], []).append(entry["message"])
    assert {k: len(v) for k, v in by_type.items()} == {"event": 4, "decision": 2, "action": 2, "system": 1}
    assert all("recorded; does not wake the supervisor" in m for m in by_type["event"])  # incl. delivered
    assert "terminal status 'delivered'" in by_type["system"][0]

    # The workflow is closed: live status is no longer available.
    closed = await api.get(f"/api/runs/{run_id}/status")
    assert (closed.status_code, closed.json()["code"]) == (409, "RUN_NOT_ACTIVE")
