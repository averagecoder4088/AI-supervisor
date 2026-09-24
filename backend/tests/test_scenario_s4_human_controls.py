"""S4 - Human controls, end to end (Step 8).

The operator's controls (pause, resume, interrupt) go through the real FastAPI endpoints
and Temporal Signals. The simulator (``app.simulation``) plays the external world (mock
tables only, events through the real event endpoint), and it keeps moving while the
supervisor is paused. Every supervisor-side row is produced by the real API, workflow and
Activities against real PostgreSQL and Temporal's time-skipping test server. Only the LLM
is scripted (three successful calls), and one tool is wrapped in a gate so an interrupt can
land while the tool is genuinely executing; the wrapper still calls the REAL database-backed
handler once the gate opens.

Proven here: pause records events and stops reasoning; one resume cycle sees the whole
backlog; an interrupt during a running tool does not cancel it and the cycle completes
normally; the order completes (final output) while paused.
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

from app.db.mock_factories import create_mock_order, create_mock_shipment
from app.db.mock_models import MockCustomerMessage, MockOrder, MockShipment
from app.db.models import Action, Event, FinalOutput, MemorySnapshot, Run, ToolExecution
from app.llm.fake import FakeLLMClient
from app.llm.schemas import FINAL_OUTPUT_SCHEMA_NAME, REASONING_SCHEMA_NAME
from app.main import create_app
from app.simulation.s2_delayed_shipment import S2_DELAY_REASON, S2_ESCALATION_REASON
from app.simulation.s4_human_controls import (
    CONTROL_INTERRUPT_DURING_TOOL,
    CONTROL_PAUSED,
    CONTROL_RESUMED,
    S4_MEMORY_SUMMARIES,
    S4_TOOL,
    GatedTool,
    s4_llm_script,
    s4_supervisor_body,
)
from app.simulation.world import EVENT_ORDER_STATUS_WITH_DELAY, ExternalWorld, shipment_id_for
from app.temporal.constants import order_workflow_id
from app.temporal.contracts import ToolRequest
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from app.tools.mock_operations import build_mock_tool_handlers
from tests.api_support import PREFIX, pooled_session_factory  # noqa: F401

pytestmark = pytest.mark.asyncio

ORDER_ID = f"{PREFIX}S4-001"
PAUSED_EVENTS = ["order_created", "payment_confirmed", "shipment_created", "shipment_delayed"]
ALL_EVENTS = [*PAUSED_EVENTS, "delivered"]
STATUS_AFTER = {
    "order_created": "created",
    "payment_confirmed": "payment_confirmed",
    "shipment_created": "shipped",
    "shipment_delayed": "delayed",
}


# ------------------------------------------------------------------ fixtures


async def _delete_mock_orders(factory) -> None:
    # Shipments and messages cascade from mock_orders. The generic API cleanup does not
    # know the mock tables, so S4 cleans them itself (before and after every test).
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


class S4:
    def __init__(
        self, env: WorkflowEnvironment, factory, api: httpx.AsyncClient, llm: FakeLLMClient, gate: GatedTool
    ) -> None:
        self.env = env
        self.factory = factory
        self.api = api
        self.llm = llm
        self.gate = gate
        self.world = ExternalWorld(factory, api)


@pytest_asyncio.fixture
async def s4(factory) -> AsyncIterator[S4]:
    llm = FakeLLMClient(s4_llm_script())
    real_handlers = build_mock_tool_handlers(factory)  # the REAL database-backed tools
    gate = GatedTool(real_handlers[S4_TOOL])
    registry = dict(real_handlers, **{S4_TOOL: gate})  # only escalate_shipment is wrapped
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=factory, connect_temporal=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            try:
                async with create_worker(env.client, session_factory=factory, llm_client=llm, tool_registry=registry):
                    yield S4(env, factory, api, llm, gate)
            finally:
                gate.release.set()  # never leave a blocked tool behind, even if the test fails


# ------------------------------------------------------------------- helpers


async def eventually(check: Callable, attempts: int = 500):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def live_status(s4: S4, run_id: str) -> Dict[str, Any]:
    response = await s4.api.get(f"/api/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()


async def status_where(s4: S4, run_id: str, predicate: Callable[[Dict[str, Any]], bool]) -> Dict[str, Any]:
    async def check():
        response = await s4.api.get(f"/api/runs/{run_id}/status")
        if response.status_code != 200:
            return None
        status = response.json()
        return status if predicate(status) else None

    return await eventually(check)


async def settled(s4: S4, run_id: str, cycles: int) -> Dict[str, Any]:
    """Wait until exactly ``cycles`` COMPLETED reasoning cycles finished and the workflow is sleeping."""
    return await status_where(s4, run_id, lambda s: s["reasoning_count"] == cycles and s["state"] == "sleeping")


async def paused(s4: S4, run_id: str) -> Dict[str, Any]:
    """Wait until Temporal's live state is definitely ``paused``."""
    return await status_where(s4, run_id, lambda s: s["state"] == "paused")


async def timeline(s4: S4, run_id: str) -> List[Dict[str, Any]]:
    return (await s4.api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]


async def recorded(s4: S4, run_id: str, event_type: str, *, wakes: bool) -> None:
    """Wait until the WORKFLOW has persisted the event and judged whether it wakes the supervisor.

    The label reflects the wake POLICY, so an important event still says it wakes the
    supervisor even while the supervisor is paused (existing wording, deliberately unchanged).
    """
    verdict = "important: wakes the supervisor" if wakes else "recorded; does not wake the supervisor"
    expected = f"Event received: {event_type} ({verdict})"

    async def check():
        return any(e["message"].startswith(expected) for e in await timeline(s4, run_id))

    await eventually(check)


async def control_entries(s4: S4, run_id: str, message: str, count: int) -> None:
    """Poll for ``count`` control entries with this exact message (the workflow persists them on its next loop)."""

    async def check():
        entries = [e for e in await timeline(s4, run_id) if e["entry_type"] == "control" and e["message"] == message]
        return len(entries) == count

    await eventually(check)


async def mock_state(factory) -> Dict[str, List[Dict[str, Any]]]:
    """Every column of every mock row for the S4 order (updated_at proves nothing was written)."""

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


def prompt(s4: S4, call_index: int) -> Dict[str, Any]:
    return json.loads(s4.llm.calls[call_index]["user_prompt"])


def shipment_except(state: Dict[str, List[Dict[str, Any]]], *skip: str) -> Dict[str, Any]:
    (shipment,) = state["shipments"]
    return {k: v for k, v in shipment.items() if k not in skip}


def spy_on_events(s4: S4) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Record the mock state seen at the moment each event is submitted (commit-before-emit)."""
    seen: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    original = s4.world._emit

    async def spy(run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        seen[event_type] = await mock_state(s4.factory)
        await original(run_id, event_type, payload)

    s4.world._emit = spy  # type: ignore[assignment]
    return seen


async def control(s4: S4, run_id: str, name: str) -> None:
    response = await s4.api.post(f"/api/runs/{run_id}/{name}")
    assert response.status_code == 202, response.text
    assert response.json() == {"run_id": run_id, "request": name, "accepted": True}


# ------------------------------------------------------------ small S4 tests


async def test_s4_supervisor_uses_the_s2_configuration_and_mapping():
    body = s4_supervisor_body("x")
    assert body["order_status_by_event"] == EVENT_ORDER_STATUS_WITH_DELAY
    assert {"shipment_delayed", "customer_message_received"} <= set(body["wake_policy"]["important_event_types"])
    assert (body["default_wake_interval_minutes"], body["min_wake_interval_minutes"]) == (60, 1)
    assert body["terminal_order_statuses"] == ["delivered", "cancelled"]
    assert S4_TOOL in body["enabled_tools"]


async def test_the_gated_tool_waits_for_the_gate_then_runs_the_real_handler(factory):
    async with factory() as session:
        await create_mock_order(session, order_id=ORDER_ID, status="shipped")
        await create_mock_shipment(session, order_id=ORDER_ID, shipment_id=shipment_id_for(ORDER_ID), status="delayed")
        await session.commit()
    gate = GatedTool(build_mock_tool_handlers(factory)[S4_TOOL])
    request = ToolRequest(
        tool_execution_id=str(uuid.UUID(int=1)),
        order_id=ORDER_ID,
        tool_name=S4_TOOL,
        tool_input={"reason": S2_ESCALATION_REASON, "priority": "high"},
    )

    running = asyncio.ensure_future(gate(request))
    await asyncio.wait_for(gate.started.wait(), 5)
    await asyncio.sleep(0.05)  # bounded settle: the real handler must NOT have run yet
    assert not running.done()
    assert (await mock_state(factory))["shipments"][0]["escalated"] is False

    gate.release.set()
    result = await asyncio.wait_for(running, 5)
    assert (result.success, result.error) == (True, None)  # the REAL handler produced this result
    assert result.output == {"order_id": ORDER_ID, "shipment_id": shipment_id_for(ORDER_ID), "escalated": True}
    assert (await mock_state(factory))["shipments"][0]["escalated"] is True
    assert gate.calls == 1


# ------------------------------------------------------------------- S4 flow


async def test_s4_human_controls(s4):
    api, world, llm, factory, env, gate = s4.api, s4.world, s4.llm, s4.factory, s4.env, s4.gate
    at_emit = spy_on_events(s4)

    created = await api.post("/api/supervisors", json=s4_supervisor_body(f"{PREFIX}S4 Human Controls"))
    assert created.status_code == 201, created.text
    supervisor_id = created.json()["id"]

    # 1-3. The world places the order; the run starts; cycle 1 (workflow_start, no tool) completes.
    await world.place_order(ORDER_ID)
    response = await api.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": supervisor_id})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    start = await settled(s4, run_id, 1)
    assert start["last_wake_reason"] == "workflow_start" and start["memory"]["situation_summary"] == S4_MEMORY_SUMMARIES[0]
    assert start["next_wake_at"] is not None and len(llm.calls) == 1
    placed = await mock_state(factory)
    assert [(o["order_id"], o["status"]) for o in placed["orders"]] == [(ORDER_ID, "created")]
    assert placed["shipments"] == [] and placed["messages"] == []

    # 4-5. The operator pauses through the API; Temporal's live state is definitely paused.
    await control(s4, run_id, "pause")
    frozen = await paused(s4, run_id)
    assert (frozen["reasoning_count"], frozen["next_wake_at"]) == (1, None)  # the schedule is suspended
    await control_entries(s4, run_id, CONTROL_PAUSED, 1)

    # 6-7. While paused the world keeps moving; every event is recorded and reflected in state.
    steps = [
        (lambda: world.order_created(run_id, ORDER_ID), "order_created", False),
        (lambda: world.confirm_payment(run_id, ORDER_ID), "payment_confirmed", False),
        (lambda: world.create_shipment(run_id, ORDER_ID), "shipment_created", False),
        (lambda: world.delay_shipment(run_id, ORDER_ID, S2_DELAY_REASON), "shipment_delayed", True),
    ]
    for index, (step, event_type, important) in enumerate(steps, start=1):
        await step()
        await recorded(s4, run_id, event_type, wakes=important)  # persisted by the workflow's Activity
        status = await live_status(s4, run_id)
        assert (status["state"], status["reasoning_count"], status["next_wake_at"]) == ("paused", 1, None)
        assert status["order_status"] == STATUS_AFTER[event_type]  # workflow view follows the mapping
        assert await mock_order_status(factory) == STATUS_AFTER[event_type]  # ... and agrees with the world
        assert [e["event_type"] for e in status["pending_events"]] == PAUSED_EVENTS[:index]
        assert (await api.get(f"/api/runs/{run_id}")).json()["order_status"] == STATUS_AFTER[event_type]
        assert len(llm.calls) == 1  # 8. no additional reasoning, even for the important event

    # 8. Nothing was decided or written by the supervisor during the pause.
    delayed = at_emit["shipment_delayed"]  # state at the moment the event was submitted
    assert (delayed["shipments"][0]["status"], delayed["shipments"][0]["escalated"]) == ("delayed", False)
    assert delayed["shipments"][0]["delay_reason"] == S2_DELAY_REASON
    assert await mock_state(factory) == delayed
    assert (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"] == []
    assert (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"] == []
    assert len((await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]) == 1
    async with factory() as session:
        recorded_events = (
            (await session.execute(select(Event).where(Event.run_id == uuid.UUID(run_id)).order_by(Event.received_at, Event.id)))
            .scalars()
            .all()
        )
    assert [e.event_type for e in recorded_events] == PAUSED_EVENTS  # persisted while still paused

    # 9-10. Three hours pass while paused: the scheduled wake stays suspended.
    await env.sleep(timedelta(hours=3))
    still = await live_status(s4, run_id)
    assert (still["state"], still["reasoning_count"], still["next_wake_at"], len(llm.calls)) == ("paused", 1, None, 1)

    # 11. The operator resumes: one reasoning cycle (wake_reason resume) starts and reaches the gated tool.
    await control(s4, run_id, "resume")
    await asyncio.wait_for(gate.started.wait(), 10)
    resume_prompt = prompt(s4, 1)  # 12. what the resume cycle was given
    assert resume_prompt["wake_reason"] == "resume" and resume_prompt["order_status"] == "delayed"
    assert [e["event_type"] for e in resume_prompt["new_events"]] == PAUSED_EVENTS
    assert resume_prompt["new_events"][-1]["payload"]["delay_reason"] == S2_DELAY_REASON
    assert resume_prompt["memory"]["situation_summary"] == S4_MEMORY_SUMMARIES[0]
    assert len(llm.calls) == 2

    # 13-16. The tool is IN FLIGHT: pending action/execution, workflow reasoning, shipment not yet escalated.
    in_flight = await status_where(s4, run_id, lambda s: s["state"] == "reasoning")
    assert (in_flight["reasoning_count"], in_flight["interrupt_count"]) == (1, 0)
    (pending_action,) = (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"]
    assert (pending_action["action_type"], pending_action["status"]) == (S4_TOOL, "pending")
    (pending_execution,) = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert (pending_execution["tool_name"], pending_execution["status"]) == (S4_TOOL, "pending")
    assert (await mock_state(factory))["shipments"][0]["escalated"] is False
    assert not gate.release.is_set() and gate.calls == 1

    # 17-19. The operator interrupts WHILE the tool runs; the workflow accepts it (still reasoning, gate still closed).
    await control(s4, run_id, "interrupt")
    interrupted = await status_where(s4, run_id, lambda s: s["interrupt_count"] == 1)
    assert interrupted["state"] == "reasoning"  # the cycle and its tool are still in flight
    assert not gate.release.is_set()
    assert (await mock_state(factory))["shipments"][0]["escalated"] is False
    handle = env.client.get_workflow_handle_for(OrderWorkflow.run, order_workflow_id(ORDER_ID))
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

    # 20-24. Open the gate: the REAL handler runs, the tool completes, and the cycle completes normally.
    gate.release.set()
    resumed = await settled(s4, run_id, 2)
    assert resumed["last_wake_reason"] == "resume" and resumed["last_cycle_outcome"] == "completed"
    assert resumed["interrupt_count"] == 1 and resumed["state"] == "sleeping"
    assert resumed["memory"]["situation_summary"] == S4_MEMORY_SUMMARIES[1]
    assert resumed["memory"]["last_action"] == f"{S4_TOOL}: succeeded"
    assert resumed["pending_events"] == [] and len(resumed["recent_events"]) == 4  # the backlog was seen
    assert resumed["last_decision"]["executed_tool"] == S4_TOOL
    assert len(llm.calls) == 2 and gate.calls == 1
    escalated = await mock_state(factory)
    assert escalated["shipments"][0]["escalated"] is True  # the tool's REAL side effect persisted
    assert shipment_except(escalated, "escalated", "updated_at") == shipment_except(delayed, "escalated", "updated_at")
    assert escalated["orders"] == delayed["orders"] and escalated["messages"] == []
    (action,) = (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"]
    assert (action["action_type"], action["status"]) == (S4_TOOL, "completed") and action["completed_at"] is not None
    (execution,) = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert (execution["status"], execution["error"]) == ("success", None)
    assert execution["result"] == {"order_id": ORDER_ID, "shipment_id": shipment_id_for(ORDER_ID), "escalated": True}
    assert len((await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]) == 2
    # The workflow persists control entries on its next loop, i.e. after the running cycle finished.
    await control_entries(s4, run_id, CONTROL_INTERRUPT_DURING_TOOL, 1)
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

    # 25-27. Pause again, then the world delivers while paused.
    await control(s4, run_id, "pause")
    again = await paused(s4, run_id)
    assert (again["reasoning_count"], again["next_wake_at"]) == (2, None)
    await control_entries(s4, run_id, CONTROL_PAUSED, 2)
    await world.deliver(run_id, ORDER_ID)
    at_delivery = at_emit["delivered"]["shipments"][0]
    assert (at_delivery["status"], at_delivery["escalated"], at_delivery["delay_reason"]) == (
        "delivered",
        True,
        S2_DELAY_REASON,
    )

    # 28-30. Terminal while paused: no third cycle; final output -> persist -> run completed -> workflow closes.
    await eventually(lambda: _completed(factory, run_id))
    result = await handle.result()  # workflow closure is the synchronization point
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert result.reasoning_count == 2 and result.interrupt_count == 1  # nothing reasoned after delivered
    assert (result.terminal_order_status_reached, result.final_output_persisted) == (True, True)
    assert result.state == "terminal" and result.order_status == "delivered" and result.events_received == 5
    assert [c["schema_name"] for c in llm.calls] == [REASONING_SCHEMA_NAME] * 2 + [FINAL_OUTPUT_SCHEMA_NAME]
    assert len(llm.calls) == 3

    await assert_final_mock_state(factory)
    await assert_supervisor_history(s4, run_id)
    await assert_observable_through_the_api(s4, run_id)


async def _completed(factory, run_id: str) -> bool:
    return await run_status(factory, run_id) == "completed"


async def assert_final_mock_state(factory) -> None:
    final = await mock_state(factory)
    (order,) = final["orders"]
    (shipment,) = final["shipments"]
    assert order["status"] == "delivered"
    assert (shipment["status"], shipment["delay_reason"], shipment["escalated"]) == ("delivered", S2_DELAY_REASON, True)
    assert final["messages"] == []


async def assert_supervisor_history(s4: S4, run_id: str) -> None:
    rid = uuid.UUID(run_id)
    async with s4.factory() as session:
        run = await session.get(Run, rid)
        assert run.status == "completed" and run.order_status == "delivered" and run.completed_at is not None

        events = (
            (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at, Event.id)))
            .scalars()
            .all()
        )
        assert [e.event_type for e in events] == ALL_EVENTS

        actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
        assert [(a.action_type, a.status) for a in actions] == [(S4_TOOL, "completed")]
        executions = (
            (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([a.id for a in actions]))))
            .scalars()
            .all()
        )
        assert [(e.tool_name, e.status) for e in executions] == [(S4_TOOL, "success")]
        snapshots = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
        assert len(snapshots) == 2
        outputs = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
        assert len(outputs) == 1 and outputs[0].output["source"] == "llm"


async def assert_observable_through_the_api(s4: S4, run_id: str) -> None:
    api = s4.api
    run = (await api.get(f"/api/runs/{run_id}")).json()
    assert (run["status"], run["order_status"]) == ("completed", "delivered") and run["completed_at"] is not None

    final = (await api.get(f"/api/runs/{run_id}/final-output")).json()
    output = final["final_output"]
    assert final["created_at"] is not None
    assert set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
    assert output["source"] == "llm" and len(output["key_actions"]) == 1

    # The final-output LLM call received the real history.
    final_prompt = prompt(s4, 2)
    assert s4.llm.calls[2]["schema_name"] == FINAL_OUTPUT_SCHEMA_NAME
    assert final_prompt["final_order_status"] == "delivered"
    assert (final_prompt["events_received"], final_prompt["reasoning_count"]) == (5, 2)
    assert [(a["tool"], a["success"]) for a in final_prompt["action_log"]] == [(S4_TOOL, True)]

    snapshots = (await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]
    assert [s["memory"]["situation_summary"] for s in snapshots] == S4_MEMORY_SUMMARIES
    (execution,) = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert execution["input"] == {"reason": S2_ESCALATION_REASON, "priority": "high"}

    entries = await timeline(s4, run_id)
    by_type: Dict[str, List[str]] = {}
    for entry in entries:
        by_type.setdefault(entry["entry_type"], []).append(entry["message"])
    assert {k: len(v) for k, v in by_type.items()} == {"event": 5, "control": 4, "decision": 2, "action": 1, "system": 1}
    assert sorted(by_type["control"]) == sorted(
        [CONTROL_PAUSED, CONTROL_PAUSED, CONTROL_RESUMED, CONTROL_INTERRUPT_DURING_TOOL]
    )
    important = [m for m in by_type["event"] if "important: wakes the supervisor" in m]
    assert [m.split(" (")[0] for m in important] == ["Event received: shipment_delayed"]  # existing wording, unchanged
    assert len([m for m in by_type["event"] if "recorded; does not wake the supervisor" in m]) == 4
    decisions = " ".join(by_type["decision"])
    assert "[tool: none]" in decisions and f"[tool: {S4_TOOL}]" in decisions
    assert "terminal status 'delivered'" in by_type["system"][0]

    closed = await api.get(f"/api/runs/{run_id}/status")  # the workflow is closed
    assert (closed.status_code, closed.json()["code"]) == (409, "RUN_NOT_ACTIVE")
