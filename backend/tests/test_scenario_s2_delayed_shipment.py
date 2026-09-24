"""S2 - Delayed shipment, end to end (Step 8).

The simulator (``app.simulation``) plays the external world: it changes the MOCK
operational tables and then submits each event through the real FastAPI event
endpoint. The human operator's instruction goes through the real instructions
endpoint (not through the simulator). Everything supervisor-side (events,
timeline, actions, tool executions, memory, final output, run status) is
produced by the real API, workflow and Activities against real PostgreSQL and
Temporal's time-skipping test server. Only the LLM is scripted (FakeLLMClient);
the scheduled wake is crossed with ``env.sleep``, never in real time.

Causal chain under test: important event -> wake -> reasoning -> tool -> mock
DB mutation -> a LATER reasoning cycle observes that mutation.
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
from app.simulation.s2_delayed_shipment import (
    S2_CUSTOMER_QUESTION,
    S2_CUSTOMER_UPDATE,
    S2_DELAY_REASON,
    S2_ESCALATION_REASON,
    S2_INSTRUCTION,
    S2_MEMORY_SUMMARIES,
    S2_ORDER_STATUS_BY_EVENT,
    s2_llm_script,
    s2_supervisor_body,
)
from app.simulation.world import (
    EVENT_ORDER_STATUS,
    EVENT_ORDER_STATUS_WITH_DELAY,
    EventNotAccepted,
    ExternalWorld,
    InvalidTransition,
    shipment_id_for,
)
from app.temporal.constants import order_workflow_id
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.api_support import PREFIX, pooled_session_factory  # noqa: F401

pytestmark = pytest.mark.asyncio

ORDER_ID = f"{PREFIX}S2-001"
ROUTINE_EVENTS = ("order_created", "payment_confirmed", "shipment_created")
ALL_EVENTS = [*ROUTINE_EVENTS, "shipment_delayed", "customer_message_received", "delivered"]
TOOL_ORDER = ["escalate_shipment", "send_customer_update", "get_shipment_status"]


# ------------------------------------------------------------------ fixtures


async def _delete_mock_orders(factory) -> None:
    # Shipments and messages cascade from mock_orders. The generic API cleanup does not
    # know the mock tables, so S2 cleans them itself (before and after every test).
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


class S2:
    def __init__(self, env: WorkflowEnvironment, factory, api: httpx.AsyncClient, llm: FakeLLMClient) -> None:
        self.env = env
        self.factory = factory
        self.api = api
        self.llm = llm
        self.world = ExternalWorld(factory, api)


@pytest_asyncio.fixture
async def s2(factory) -> AsyncIterator[S2]:
    llm = FakeLLMClient(s2_llm_script())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=factory, connect_temporal=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            async with create_worker(env.client, session_factory=factory, llm_client=llm):
                yield S2(env, factory, api, llm)


# ------------------------------------------------------------------- helpers


async def eventually(check: Callable, attempts: int = 500):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def live_status(s2: S2, run_id: str) -> Dict[str, Any]:
    response = await s2.api.get(f"/api/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()


async def settled(s2: S2, run_id: str, cycles: int) -> Dict[str, Any]:
    """Wait until exactly ``cycles`` reasoning cycles finished and the workflow is sleeping."""

    async def check():
        response = await s2.api.get(f"/api/runs/{run_id}/status")
        if response.status_code != 200:
            return None
        status = response.json()
        return status if status["reasoning_count"] == cycles and status["state"] == "sleeping" else None

    return await eventually(check)


async def recorded(s2: S2, run_id: str, event_type: str, *, wakes: bool) -> None:
    """Wait until the WORKFLOW has persisted the event and judged whether it wakes the supervisor."""
    verdict = "important: wakes the supervisor" if wakes else "recorded; does not wake the supervisor"
    expected = f"Event received: {event_type} ({verdict})"

    async def check():
        entries = (await s2.api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
        return any(e["message"].startswith(expected) for e in entries)

    await eventually(check)


async def assert_routine(s2: S2, run_id: str, event_type: str, scheduled_at: str, expected_status: str) -> None:
    """A routine event is recorded, does not wake reasoning, and leaves the timer alone."""
    await recorded(s2, run_id, event_type, wakes=False)
    status = await live_status(s2, run_id)
    assert status["reasoning_count"] == 1 and status["state"] == "sleeping"
    assert status["next_wake_at"] == scheduled_at
    assert status["order_status"] == expected_status  # the supervisor's view ...
    assert await mock_order_status(s2.factory) == expected_status  # ... agrees with the world
    assert len(s2.llm.calls) == 1


async def mock_state(factory) -> Dict[str, List[Dict[str, Any]]]:
    """Every column of every mock row for the S2 order (updated_at proves nothing was written)."""

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


def prompt(s2: S2, call_index: int) -> Dict[str, Any]:
    return json.loads(s2.llm.calls[call_index]["user_prompt"])


def shipment_except(state: Dict[str, List[Dict[str, Any]]], *skip: str) -> Dict[str, Any]:
    (shipment,) = state["shipments"]
    return {k: v for k, v in shipment.items() if k not in skip}


def spy_on_events(s2: S2) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Record the mock state seen at the moment each event is submitted (commit-before-emit)."""
    seen: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    original = s2.world._emit

    async def spy(run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        seen[event_type] = await mock_state(s2.factory)
        await original(run_id, event_type, payload)

    s2.world._emit = spy  # type: ignore[assignment]
    return seen


# ------------------------------------------------------------ small S2 tests


async def test_s2_simulator_transitions_agree_with_the_supervisors_order_status_mapping():
    assert set(S2_ORDER_STATUS_BY_EVENT) == {*EVENT_ORDER_STATUS, "shipment_delayed"}
    assert EVENT_ORDER_STATUS_WITH_DELAY == S2_ORDER_STATUS_BY_EVENT
    assert s2_supervisor_body("x")["order_status_by_event"] == EVENT_ORDER_STATUS_WITH_DELAY
    assert "shipment_delayed" not in EVENT_ORDER_STATUS  # sealed S1's table is untouched


async def test_s2_supervisor_wakes_on_the_delay_and_the_customer_message_only():
    body = s2_supervisor_body("x")
    important = set(body["wake_policy"]["important_event_types"])
    assert {"shipment_delayed", "customer_message_received"} <= important
    assert important.isdisjoint({"order_created", "payment_confirmed", "shipment_created", "delivered"})
    assert body["terminal_order_statuses"] == ["delivered", "cancelled"]


async def test_the_new_world_steps_and_the_widened_delivery_guard(factory):
    # No Temporal client: every event is refused (503), but the world change is committed first.
    app = create_app(temporal_client=None, session_factory=factory, connect_temporal=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        world = ExternalWorld(factory, api)
        run_id = str(uuid.uuid4())

        async def applied(step) -> None:
            with pytest.raises(EventNotAccepted):
                await step

        # Impossible transitions are rejected and change nothing.
        with pytest.raises(InvalidTransition):
            await world.customer_message(run_id, ORDER_ID, "hi")  # no such order
        await world.place_order(ORDER_ID)
        with pytest.raises(InvalidTransition):
            await world.delay_shipment(run_id, ORDER_ID, "x")  # nothing shipped yet
        with pytest.raises(InvalidTransition):
            await world.deliver(run_id, ORDER_ID)  # arbitrary state: order only created
        await applied(world.confirm_payment(run_id, ORDER_ID))
        with pytest.raises(InvalidTransition):
            await world.deliver(run_id, ORDER_ID)  # payment confirmed, still nothing shipped
        await applied(world.create_shipment(run_id, ORDER_ID))

        # The delay changes shipment + order and never touches `escalated`.
        await applied(world.delay_shipment(run_id, ORDER_ID, S2_DELAY_REASON))
        state = await mock_state(factory)
        assert state["orders"][0]["status"] == "delayed"
        assert (state["shipments"][0]["status"], state["shipments"][0]["delay_reason"]) == ("delayed", S2_DELAY_REASON)
        assert state["shipments"][0]["escalated"] is False
        with pytest.raises(InvalidTransition):
            await world.delay_shipment(run_id, ORDER_ID, "again")  # already delayed

        # An inbound message adds exactly one inbound row and changes nothing else.
        await applied(world.customer_message(run_id, ORDER_ID, S2_CUSTOMER_QUESTION))
        after = await mock_state(factory)
        assert [(m["direction"], m["message"]) for m in after["messages"]] == [("inbound", S2_CUSTOMER_QUESTION)]
        assert after["orders"] == state["orders"] and after["shipments"] == state["shipments"]

        # Delivery is valid from a delayed shipment/order, keeps delay_reason and escalated, and only once.
        async with factory() as session:  # pretend a supervisor tool escalated it (tools own this write)
            shipment = (await session.execute(select(MockShipment).where(MockShipment.order_id == ORDER_ID))).scalar_one()
            shipment.escalated = True
            await session.commit()
        await applied(world.deliver(run_id, ORDER_ID))
        final = await mock_state(factory)
        assert final["orders"][0]["status"] == "delivered"
        assert (final["shipments"][0]["status"], final["shipments"][0]["delay_reason"]) == ("delivered", S2_DELAY_REASON)
        assert final["shipments"][0]["escalated"] is True
        with pytest.raises(InvalidTransition):
            await world.deliver(run_id, ORDER_ID)  # already delivered


# ------------------------------------------------------------------- S2 flow


async def test_s2_delayed_shipment(s2):
    api, world, llm, factory, env = s2.api, s2.world, s2.llm, s2.factory, s2.env
    at_emit = spy_on_events(s2)

    created = await api.post("/api/supervisors", json=s2_supervisor_body(f"{PREFIX}S2 Delayed Shipment"))
    assert created.status_code == 201, created.text
    supervisor_id = created.json()["id"]

    # 1. External customer action: the minimum mock operational state, no event yet.
    await world.place_order(ORDER_ID)
    placed = await mock_state(factory)
    assert [(o["order_id"], o["status"], o["customer_id"]) for o in placed["orders"]] == [
        (ORDER_ID, "created", f"CUST-{ORDER_ID}")
    ]
    assert placed["shipments"] == [] and placed["messages"] == []

    # 2. Run through the existing API (runs.order_status is not seeded).
    response = await api.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": supervisor_id})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    assert (response.json()["status"], response.json()["order_status"]) == ("running", None)

    # 3. Cycle 1 (workflow_start, no tool) settles completely before order_created.
    start = await settled(s2, run_id, 1)
    assert start["last_wake_reason"] == "workflow_start"
    assert start["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[0]  # scripted, not the simulator
    assert start["memory"]["last_action"] is None and start["memory"]["cycle_count"] == 1
    assert start["last_decision"]["executed_tool"] is None
    scheduled_at = start["next_wake_at"]
    assert scheduled_at is not None and len(llm.calls) == 1
    assert (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"] == []  # a no-tool cycle records no action
    assert await mock_state(factory) == placed

    # 4-6. Routine events: recorded, no wake, timer unchanged; order_created creates nothing.
    await world.order_created(run_id, ORDER_ID)
    await assert_routine(s2, run_id, "order_created", scheduled_at, "created")
    assert await mock_state(factory) == placed

    await world.confirm_payment(run_id, ORDER_ID)
    await assert_routine(s2, run_id, "payment_confirmed", scheduled_at, "payment_confirmed")

    await world.create_shipment(run_id, ORDER_ID)
    await assert_routine(s2, run_id, "shipment_created", scheduled_at, "shipped")
    shipped = await mock_state(factory)
    assert shipped["orders"][0]["status"] == "shipped"
    (shipment,) = shipped["shipments"]
    assert (shipment["shipment_id"], shipment["status"], shipment["tracking_number"]) == (
        shipment_id_for(ORDER_ID),
        "created",
        f"TRK-{shipment_id_for(ORDER_ID)}",
    )
    assert (shipment["delay_reason"], shipment["escalated"]) == (None, False)
    assert (await live_status(s2, run_id))["events_received"] == 3

    # 7. The human operator adds a run instruction through the existing API (not the simulator).
    instruction = await api.post(f"/api/runs/{run_id}/instructions", json={"instruction": S2_INSTRUCTION})
    assert instruction.status_code == 202, instruction.text

    # 8. Cycle 2 (instruction_added, no tool) consumes the three routine events.
    added = await settled(s2, run_id, 2)
    assert added["last_wake_reason"] == "instruction_added"
    assert added["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[1]
    assert added["memory"]["last_action"] is None and added["last_decision"]["executed_tool"] is None
    assert [i["text"] for i in added["run_instructions"]] == [S2_INSTRUCTION]
    assert len(llm.calls) == 2
    assert await mock_state(factory) == shipped  # no tool, no world change
    persisted = (await api.get(f"/api/runs/{run_id}")).json()["run_instructions"]
    assert [i["text"] for i in persisted] == [S2_INSTRUCTION] and persisted[0]["added_at"] is not None
    instruction_prompt = prompt(s2, 1)
    assert instruction_prompt["wake_reason"] == "instruction_added"
    assert instruction_prompt["run_instructions"] == [S2_INSTRUCTION]
    assert [e["event_type"] for e in instruction_prompt["new_events"]] == list(ROUTINE_EVENTS)
    assert (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"] == []

    # 9. The carrier reports a delay: the world changes first (escalated stays false), then the event.
    await world.delay_shipment(run_id, ORDER_ID, S2_DELAY_REASON)
    # The state captured by the spy at the moment shipment_delayed was submitted: committed BEFORE
    # the event, and race-free (the workflow cannot have reacted to it yet).
    delayed = at_emit["shipment_delayed"]
    assert delayed["orders"][0]["status"] == "delayed"
    assert (delayed["shipments"][0]["status"], delayed["shipments"][0]["delay_reason"]) == ("delayed", S2_DELAY_REASON)
    assert delayed["shipments"][0]["escalated"] is False
    assert delayed["messages"] == []

    # 10-12. The important event wakes exactly one reasoning cycle; the TOOL escalates the shipment.
    await recorded(s2, run_id, "shipment_delayed", wakes=True)
    escalated = await settled(s2, run_id, 3)
    assert escalated["last_wake_reason"] == "important_event"
    assert escalated["order_status"] == "delayed"
    assert escalated["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[2]
    assert escalated["memory"]["last_action"] == "escalate_shipment: succeeded"
    assert escalated["last_decision"]["executed_tool"] == "escalate_shipment"
    assert len(llm.calls) == 3
    after_escalation = await mock_state(factory)
    assert after_escalation["shipments"][0]["escalated"] is True  # written by the tool
    assert shipment_except(after_escalation, "escalated", "updated_at") == shipment_except(
        delayed, "escalated", "updated_at"
    )
    assert after_escalation["orders"] == delayed["orders"] and after_escalation["messages"] == []
    delay_prompt = prompt(s2, 2)
    assert delay_prompt["wake_reason"] == "important_event" and delay_prompt["order_status"] == "delayed"
    assert delay_prompt["run_instructions"] == [S2_INSTRUCTION]
    assert [(e["event_type"], e["payload"]) for e in delay_prompt["new_events"]] == [
        ("shipment_delayed", {"shipment_id": shipment_id_for(ORDER_ID), "delay_reason": S2_DELAY_REASON})
    ]
    assert delay_prompt["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[1]

    # 13. The customer writes in: one inbound row (committed before the event), then the event.
    await world.customer_message(run_id, ORDER_ID, S2_CUSTOMER_QUESTION)
    assert [(m["direction"], m["message"]) for m in at_emit["customer_message_received"]["messages"]] == [
        ("inbound", S2_CUSTOMER_QUESTION)
    ]

    # 14-16. The important event wakes exactly one cycle; the TOOL adds one outbound message.
    await recorded(s2, run_id, "customer_message_received", wakes=True)
    informed = await settled(s2, run_id, 4)
    assert informed["last_wake_reason"] == "important_event"
    assert informed["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[3]
    assert informed["memory"]["last_action"] == "send_customer_update: succeeded"
    assert len(llm.calls) == 4
    assert informed["last_decision"]["next_wake_in_minutes"] == 45  # the workflow's validated interval
    after_update = await mock_state(factory)
    assert [(m["direction"], m["message"]) for m in after_update["messages"]] == [
        ("inbound", S2_CUSTOMER_QUESTION),
        ("outbound", S2_CUSTOMER_UPDATE),
    ]
    assert after_update["orders"] == after_escalation["orders"]
    assert after_update["shipments"] == after_escalation["shipments"]
    message_prompt = prompt(s2, 3)
    assert message_prompt["wake_reason"] == "important_event"
    assert [(e["event_type"], e["payload"]) for e in message_prompt["new_events"]] == [
        ("customer_message_received", {"message": S2_CUSTOMER_QUESTION})
    ]
    previous = message_prompt["last_action_result"]  # the supervisor sees what its own tool did
    assert (previous["tool"], previous["success"], previous["error"]) == ("escalate_shipment", True, None)
    assert previous["output"] == {
        "order_id": ORDER_ID,
        "shipment_id": shipment_id_for(ORDER_ID),
        "escalated": True,
    }

    # 17-18. The 45-minute request (not the 60-minute default) is honored: nothing at 30 minutes,
    # the scheduled wake at 46 (which the default 60 would not reach).
    await env.sleep(timedelta(minutes=30))
    assert (await live_status(s2, run_id))["reasoning_count"] == 4 and len(llm.calls) == 4
    await env.sleep(timedelta(minutes=46 - 30))

    # 19-21. Scheduled cycle 5: get_shipment_status observes the earlier tool mutation, read-only.
    scheduled = await settled(s2, run_id, 5)
    assert scheduled["last_wake_reason"] == "scheduled_wakeup"
    assert scheduled["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[4]
    assert scheduled["memory"]["last_action"] == "get_shipment_status: succeeded"
    assert len(llm.calls) == 5
    assert await mock_state(factory) == after_update  # get_shipment_status is read-only
    scheduled_prompt = prompt(s2, 4)
    assert scheduled_prompt["wake_reason"] == "scheduled_wakeup" and scheduled_prompt["new_events"] == []
    assert scheduled_prompt["last_action_result"]["tool"] == "send_customer_update"
    assert scheduled_prompt["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[3]
    assert scheduled_prompt["run_instructions"] == [S2_INSTRUCTION]

    # 22-23. The world delivers: shipment + order in one transaction, keeping delay_reason and escalated.
    await world.deliver(run_id, ORDER_ID)
    (at_delivery_shipment,) = at_emit["delivered"]["shipments"]
    assert (at_delivery_shipment["status"], at_delivery_shipment["escalated"]) == ("delivered", True)
    assert at_delivery_shipment["delay_reason"] == S2_DELAY_REASON
    assert at_emit["delivered"]["orders"][0]["status"] == "delivered"

    # 24-25. Terminal: no reasoning pass; final output -> persist -> run completed -> workflow closes.
    await eventually(lambda: _completed(factory, run_id))
    handle = env.client.get_workflow_handle_for(OrderWorkflow.run, order_workflow_id(ORDER_ID))
    result = await handle.result()  # workflow closure is the synchronization point
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert result.reasoning_count == 5  # nothing reasoned after delivered
    assert (result.terminal_order_status_reached, result.final_output_persisted) == (True, True)
    assert result.order_status == "delivered" and result.events_received == 6
    assert [c["schema_name"] for c in llm.calls] == [REASONING_SCHEMA_NAME] * 5 + [FINAL_OUTPUT_SCHEMA_NAME]

    await assert_final_mock_state(factory)
    await assert_supervisor_history(s2, run_id)
    await assert_final_output_prompt(s2)
    await assert_observable_through_the_api(s2, run_id)


async def _completed(factory, run_id: str) -> bool:
    return await run_status(factory, run_id) == "completed"


async def assert_final_mock_state(factory) -> None:
    final = await mock_state(factory)
    (order,) = final["orders"]
    (shipment,) = final["shipments"]
    assert order["status"] == "delivered"
    assert (shipment["status"], shipment["delay_reason"], shipment["escalated"]) == (
        "delivered",
        S2_DELAY_REASON,
        True,
    )
    assert [(m["direction"], m["message"]) for m in final["messages"]] == [
        ("inbound", S2_CUSTOMER_QUESTION),
        ("outbound", S2_CUSTOMER_UPDATE),
    ]


async def assert_supervisor_history(s2: S2, run_id: str) -> None:
    rid = uuid.UUID(run_id)
    async with s2.factory() as session:
        run = await session.get(Run, rid)
        assert run.status == "completed"
        assert run.order_status == "delivered"
        assert run.completed_at is not None
        assert [i["text"] for i in run.run_instructions] == [S2_INSTRUCTION]

        events = (
            (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at, Event.id)))
            .scalars()
            .all()
        )
        assert [e.event_type for e in events] == ALL_EVENTS
        assert events[3].payload["delay_reason"] == S2_DELAY_REASON
        assert events[4].payload == {"message": S2_CUSTOMER_QUESTION}

        actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
        assert sorted((a.action_type, a.status) for a in actions) == sorted((t, "completed") for t in TOOL_ORDER)
        executions = (
            (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([a.id for a in actions]))))
            .scalars()
            .all()
        )
        assert sorted((e.tool_name, e.status) for e in executions) == sorted((t, "success") for t in TOOL_ORDER)
        snapshots = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
        assert len(snapshots) == 5
        outputs = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
        assert len(outputs) == 1 and outputs[0].output["source"] == "llm"


async def assert_final_output_prompt(s2: S2) -> None:
    """The final-output LLM call received the run's real history."""
    final_prompt = prompt(s2, 5)
    assert s2.llm.calls[5]["schema_name"] == FINAL_OUTPUT_SCHEMA_NAME
    assert final_prompt["final_order_status"] == "delivered"
    assert final_prompt["run_instructions"] == [S2_INSTRUCTION]
    assert (final_prompt["events_received"], final_prompt["reasoning_count"]) == (6, 5)
    assert final_prompt["memory"]["situation_summary"] == S2_MEMORY_SUMMARIES[4]
    log = final_prompt["action_log"]
    assert [(a["tool"], a["success"]) for a in log] == [(t, True) for t in TOOL_ORDER]
    assert log[0]["output"]["escalated"] is True
    assert log[2]["output"]["escalated"] is True and log[2]["output"]["status"] == "delayed"
    assert log[2]["output"]["delay_reason"] == S2_DELAY_REASON


async def assert_observable_through_the_api(s2: S2, run_id: str) -> None:
    api = s2.api
    run = (await api.get(f"/api/runs/{run_id}")).json()
    assert (run["status"], run["order_status"]) == ("completed", "delivered")
    assert run["completed_at"] is not None

    final = (await api.get(f"/api/runs/{run_id}/final-output")).json()
    output = final["final_output"]
    assert final["created_at"] is not None
    assert set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
    assert output["source"] == "llm" and len(output["key_actions"]) == 3

    actions = (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"]
    assert [(a["action_type"], a["status"]) for a in actions] == [(t, "completed") for t in TOOL_ORDER]
    executions = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert [(e["tool_name"], e["status"], e["error"]) for e in executions] == [(t, "success", None) for t in TOOL_ORDER]
    escalate, update, status_check = executions
    assert escalate["input"] == {"reason": S2_ESCALATION_REASON, "priority": "high"}
    assert escalate["result"] == {
        "order_id": ORDER_ID,
        "shipment_id": shipment_id_for(ORDER_ID),
        "escalated": True,
    }
    assert update["input"] == {"message": S2_CUSTOMER_UPDATE}
    assert set(update["result"]) == {"order_id", "message_id"} and update["result"]["order_id"] == ORDER_ID
    assert status_check["input"] == {}
    assert status_check["result"] == {  # the later observation of the tool's earlier mutation
        "order_id": ORDER_ID,
        "shipment_id": shipment_id_for(ORDER_ID),
        "status": "delayed",
        "tracking_number": f"TRK-{shipment_id_for(ORDER_ID)}",
        "delay_reason": S2_DELAY_REASON,
        "escalated": True,
    }
    async with s2.factory() as session:  # the tool's message_id is the outbound row's id
        outbound = (
            await session.execute(
                select(MockCustomerMessage.id).where(
                    MockCustomerMessage.order_id == ORDER_ID, MockCustomerMessage.direction == "outbound"
                )
            )
        ).scalar_one()
    assert update["result"]["message_id"] == str(outbound)

    snapshots = (await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]
    assert [s["memory"]["situation_summary"] for s in snapshots] == S2_MEMORY_SUMMARIES

    entries = (await api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
    by_type: Dict[str, List[str]] = {}
    for entry in entries:
        by_type.setdefault(entry["entry_type"], []).append(entry["message"])
    assert {k: len(v) for k, v in by_type.items()} == {
        "event": 6,
        "instruction": 1,
        "decision": 5,
        "action": 3,
        "system": 1,
    }
    important = [m for m in by_type["event"] if "important: wakes the supervisor" in m]
    assert sorted(m.split(" (")[0] for m in important) == [
        "Event received: customer_message_received",
        "Event received: shipment_delayed",
    ]
    assert len([m for m in by_type["event"] if "recorded; does not wake the supervisor" in m]) == 4
    assert by_type["instruction"] == [f"Run instruction added: {S2_INSTRUCTION}"]
    assert sum("[tool: none]" in m for m in by_type["decision"]) == 2
    assert all(f"[tool: {t}]" in " ".join(by_type["decision"]) for t in TOOL_ORDER)
    assert "terminal status 'delivered'" in by_type["system"][0]

    closed = await api.get(f"/api/runs/{run_id}/status")  # the workflow is closed
    assert (closed.status_code, closed.json()["code"]) == (409, "RUN_NOT_ACTIVE")
