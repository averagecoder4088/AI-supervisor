"""S3 - LLM unavailable during an incident, end to end (Step 8).

The LLM is an unreliable dependency. The scripted client succeeds at workflow start,
FAILS when the shipment delay wakes the supervisor, succeeds at the next scheduled wake
(recovery) and FAILS at the final-output call. The simulator (``app.simulation``) plays
the external world (mock tables only, events through the real FastAPI endpoint); every
supervisor-side row is produced by the real API, workflow and Activities against real
PostgreSQL and Temporal's time-skipping test server. Time is skipped with ``env.sleep``,
never waited in real time.

Chain under test: delay event -> wake -> LLM Activity fails -> workflow survives with no
partial state -> schedule falls back to the configured default -> recovery wake sees the
event that woke the failed attempt -> tool mutates the mock world -> terminal -> the
final-output LLM fails -> deterministic fallback output, run still completed.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta
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
from app.simulation.s2_delayed_shipment import S2_DELAY_REASON, S2_ESCALATION_REASON
from app.simulation.s3_llm_unavailable import (
    S3_MEMORY_SUMMARIES,
    S3_OUTAGE_MESSAGE,
    s3_llm_script,
    s3_supervisor_body,
)
from app.simulation.world import EVENT_ORDER_STATUS_WITH_DELAY, ExternalWorld, shipment_id_for
from app.temporal.constants import order_workflow_id
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.api_support import PREFIX, pooled_session_factory  # noqa: F401

pytestmark = pytest.mark.asyncio

ORDER_ID = f"{PREFIX}S3-001"
ROUTINE_EVENTS = ("order_created", "payment_confirmed", "shipment_created")
PENDING_AT_FAILURE = [*ROUTINE_EVENTS, "shipment_delayed"]
ALL_EVENTS = [*PENDING_AT_FAILURE, "delivered"]


# ------------------------------------------------------------------ fixtures


async def _delete_mock_orders(factory) -> None:
    # Shipments and messages cascade from mock_orders. The generic API cleanup does not
    # know the mock tables, so S3 cleans them itself (before and after every test).
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


class S3:
    def __init__(self, env: WorkflowEnvironment, factory, api: httpx.AsyncClient, llm: FakeLLMClient) -> None:
        self.env = env
        self.factory = factory
        self.api = api
        self.llm = llm
        self.world = ExternalWorld(factory, api)


@pytest_asyncio.fixture
async def s3(factory) -> AsyncIterator[S3]:
    llm = FakeLLMClient(s3_llm_script())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        app = create_app(temporal_client=env.client, session_factory=factory, connect_temporal=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            async with create_worker(env.client, session_factory=factory, llm_client=llm):
                yield S3(env, factory, api, llm)


# ------------------------------------------------------------------- helpers


async def eventually(check: Callable, attempts: int = 500):
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last value: {last!r}")


async def live_status(s3: S3, run_id: str) -> Dict[str, Any]:
    response = await s3.api.get(f"/api/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()


async def settled(s3: S3, run_id: str, cycles: int) -> Dict[str, Any]:
    """Wait until exactly ``cycles`` COMPLETED reasoning cycles finished and the workflow is sleeping."""

    async def check():
        response = await s3.api.get(f"/api/runs/{run_id}/status")
        if response.status_code != 200:
            return None
        status = response.json()
        return status if status["reasoning_count"] == cycles and status["state"] == "sleeping" else None

    return await eventually(check)


async def failed_attempt(s3: S3, run_id: str) -> Dict[str, Any]:
    """Wait until a reasoning attempt ended in ``llm_failed`` (failed attempts are NOT counted as cycles)."""

    async def check():
        response = await s3.api.get(f"/api/runs/{run_id}/status")
        if response.status_code != 200:
            return None
        status = response.json()
        return status if status["last_cycle_outcome"] == "llm_failed" and status["state"] == "sleeping" else None

    return await eventually(check)


async def recorded(s3: S3, run_id: str, event_type: str, *, wakes: bool) -> None:
    """Wait until the WORKFLOW has persisted the event and judged whether it wakes the supervisor."""
    verdict = "important: wakes the supervisor" if wakes else "recorded; does not wake the supervisor"
    expected = f"Event received: {event_type} ({verdict})"

    async def check():
        entries = (await s3.api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
        return any(e["message"].startswith(expected) for e in entries)

    await eventually(check)


async def assert_routine(s3: S3, run_id: str, event_type: str, scheduled_at: str, expected_status: str) -> None:
    """A routine event is recorded, does not wake reasoning, and leaves the timer alone."""
    await recorded(s3, run_id, event_type, wakes=False)
    status = await live_status(s3, run_id)
    assert status["reasoning_count"] == 1 and status["state"] == "sleeping"
    assert status["next_wake_at"] == scheduled_at
    assert status["order_status"] == expected_status  # the supervisor's view ...
    assert await mock_order_status(s3.factory) == expected_status  # ... agrees with the world
    assert len(s3.llm.calls) == 1


async def mock_state(factory) -> Dict[str, List[Dict[str, Any]]]:
    """Every column of every mock row for the S3 order (updated_at proves nothing was written)."""

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


def prompt(s3: S3, call_index: int) -> Dict[str, Any]:
    return json.loads(s3.llm.calls[call_index]["user_prompt"])


def shipment_except(state: Dict[str, List[Dict[str, Any]]], *skip: str) -> Dict[str, Any]:
    (shipment,) = state["shipments"]
    return {k: v for k, v in shipment.items() if k not in skip}


def when(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def spy_on_events(s3: S3) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Record the mock state seen at the moment each event is submitted (commit-before-emit)."""
    seen: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    original = s3.world._emit

    async def spy(run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        seen[event_type] = await mock_state(s3.factory)
        await original(run_id, event_type, payload)

    s3.world._emit = spy  # type: ignore[assignment]
    return seen


# ------------------------------------------------------------ small S3 test


async def test_s3_supervisor_uses_the_s2_configuration_and_mapping():
    body = s3_supervisor_body("x")
    assert body["order_status_by_event"] == EVENT_ORDER_STATUS_WITH_DELAY
    assert {"shipment_delayed", "customer_message_received"} <= set(body["wake_policy"]["important_event_types"])
    assert (body["default_wake_interval_minutes"], body["min_wake_interval_minutes"]) == (60, 1)
    assert body["terminal_order_statuses"] == ["delivered", "cancelled"]


# ------------------------------------------------------------------- S3 flow


async def test_s3_llm_failure_recovery_and_fallback_final_output(s3):
    api, world, llm, factory, env = s3.api, s3.world, s3.llm, s3.factory, s3.env
    at_emit = spy_on_events(s3)

    created = await api.post("/api/supervisors", json=s3_supervisor_body(f"{PREFIX}S3 LLM Unavailable"))
    assert created.status_code == 201, created.text
    supervisor_id = created.json()["id"]

    # 1. External world: the minimal order. 2. Run through the existing API.
    await world.place_order(ORDER_ID)
    placed = await mock_state(factory)
    assert [(o["order_id"], o["status"]) for o in placed["orders"]] == [(ORDER_ID, "created")]
    assert placed["shipments"] == [] and placed["messages"] == []
    response = await api.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": supervisor_id})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]

    # 3. Cycle 1 (workflow_start) SUCCEEDS; it requests a 45-minute wake.
    start = await settled(s3, run_id, 1)
    assert start["last_wake_reason"] == "workflow_start" and start["last_cycle_outcome"] == "completed"
    assert start["memory"]["situation_summary"] == S3_MEMORY_SUMMARIES[0]
    requested_wake_at = start["next_wake_at"]
    assert requested_wake_at is not None and len(llm.calls) == 1

    # 4-6. Routine events: recorded, no wake, timer unchanged.
    await world.order_created(run_id, ORDER_ID)
    await assert_routine(s3, run_id, "order_created", requested_wake_at, "created")
    await world.confirm_payment(run_id, ORDER_ID)
    await assert_routine(s3, run_id, "payment_confirmed", requested_wake_at, "payment_confirmed")
    await world.create_shipment(run_id, ORDER_ID)
    await assert_routine(s3, run_id, "shipment_created", requested_wake_at, "shipped")
    shipped = await mock_state(factory)
    assert shipment_except(shipped)["escalated"] is False

    # 7. The carrier reports a delay: the world changes first, then the (important) event.
    await world.delay_shipment(run_id, ORDER_ID, S2_DELAY_REASON)
    delayed = at_emit["shipment_delayed"]  # state at the moment the event was submitted
    assert delayed["orders"][0]["status"] == "delayed"
    assert (delayed["shipments"][0]["status"], delayed["shipments"][0]["escalated"]) == ("delayed", False)
    await recorded(s3, run_id, "shipment_delayed", wakes=True)

    # 8. The important event wakes reasoning, but the LLM call FAILS: one failed attempt, no cycle counted.
    failed = await failed_attempt(s3, run_id)
    assert failed["reasoning_count"] == 1  # a failed attempt is not a completed reasoning cycle
    assert failed["last_wake_reason"] == "workflow_start"  # only completed cycles update it
    assert len(llm.calls) == 2
    failed_prompt = prompt(s3, 1)  # the attempt that failed
    assert failed_prompt["wake_reason"] == "important_event"
    assert [e["event_type"] for e in failed_prompt["new_events"]] == PENDING_AT_FAILURE
    assert failed_prompt["memory"]["situation_summary"] == S3_MEMORY_SUMMARIES[0]

    # No partial state: no tool, no action, no memory change, no world change; the workflow is alive.
    assert await mock_state(factory) == delayed
    assert (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"] == []
    assert (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"] == []
    snapshots = (await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]
    assert [s["memory"]["situation_summary"] for s in snapshots] == [S3_MEMORY_SUMMARIES[0]]
    assert failed["memory"] == start["memory"]
    handle = env.client.get_workflow_handle_for(OrderWorkflow.run, order_workflow_id(ORDER_ID))
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    assert failed["state"] == "sleeping"

    # The failure is visible in the history, not hidden. The workflow persists this system entry
    # on its next loop iteration, i.e. possibly just after status already shows llm_failed, so poll
    # for it (as recorded() does) instead of asserting immediately.
    async def failure_timeline_entries():
        entries = (await api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
        found = [e for e in entries if e["message"].startswith("Reasoning failed after retries; no tool executed:")]
        return found or None

    failure_entries = await eventually(failure_timeline_entries)
    assert len(failure_entries) == 1 and failure_entries[0]["entry_type"] == "system"
    assert S3_OUTAGE_MESSAGE in failure_entries[0]["message"]

    # Every event is still pending: nothing was lost, and none reached a completed cycle.
    assert [e["event_type"] for e in failed["pending_events"]] == PENDING_AT_FAILURE
    assert failed["recent_events"] == [] and failed["events_received"] == 4

    # The schedule fell back to the configured DEFAULT (60), not the earlier 45-minute request.
    gap = when(failed["next_wake_at"]) - when(requested_wake_at)
    assert timedelta(minutes=14) < gap < timedelta(minutes=16), gap
    await env.sleep(timedelta(minutes=46))  # a 45-minute wake would already have fired
    still = await live_status(s3, run_id)
    assert (still["reasoning_count"], still["last_cycle_outcome"], len(llm.calls)) == (1, "llm_failed", 2)

    # 9. Recovery at the scheduled wake: the delay reaches reasoning now, and the tool escalates.
    await env.sleep(timedelta(minutes=15))
    recovered = await settled(s3, run_id, 2)
    assert recovered["last_wake_reason"] == "scheduled_wakeup" and recovered["last_cycle_outcome"] == "completed"
    assert recovered["memory"]["situation_summary"] == S3_MEMORY_SUMMARIES[1]
    assert recovered["memory"]["last_action"] == "escalate_shipment: succeeded"
    assert recovered["pending_events"] == [] and len(recovered["recent_events"]) == 4
    assert len(llm.calls) == 3
    recovery_prompt = prompt(s3, 2)
    assert recovery_prompt["wake_reason"] == "scheduled_wakeup" and recovery_prompt["order_status"] == "delayed"
    assert [e["event_type"] for e in recovery_prompt["new_events"]] == PENDING_AT_FAILURE
    assert recovery_prompt["new_events"][-1]["payload"]["delay_reason"] == S2_DELAY_REASON
    # The failed attempt wrote no memory, so the recovering cycle starts from cycle 1's memory.
    assert recovery_prompt["memory"]["situation_summary"] == S3_MEMORY_SUMMARIES[0]
    assert recovery_prompt["last_action_result"] is None
    escalated = await mock_state(factory)
    assert escalated["shipments"][0]["escalated"] is True  # written by the TOOL
    assert shipment_except(escalated, "escalated", "updated_at") == shipment_except(delayed, "escalated", "updated_at")
    assert escalated["orders"] == delayed["orders"] and escalated["messages"] == []

    # 10. The world delivers (escalated and delay_reason are kept); delivered is terminal.
    await world.deliver(run_id, ORDER_ID)
    at_delivery = at_emit["delivered"]["shipments"][0]
    assert (at_delivery["status"], at_delivery["escalated"], at_delivery["delay_reason"]) == (
        "delivered",
        True,
        S2_DELAY_REASON,
    )

    # 11-12. No reasoning after delivered; the final-output LLM call fails; the run still completes.
    await eventually(lambda: _completed(factory, run_id))
    result = await handle.result()  # workflow closure is the synchronization point
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert result.reasoning_count == 2  # completed cycles only; nothing reasoned after delivered
    assert (result.terminal_order_status_reached, result.final_output_persisted) == (True, True)
    assert result.order_status == "delivered" and result.events_received == 5
    assert [c["schema_name"] for c in llm.calls] == [REASONING_SCHEMA_NAME] * 3 + [FINAL_OUTPUT_SCHEMA_NAME]
    assert len(llm.calls) == 4

    await assert_final_mock_state(factory)
    await assert_supervisor_history(s3, run_id)
    await assert_observable_through_the_api(s3, run_id)


async def _completed(factory, run_id: str) -> bool:
    return await run_status(factory, run_id) == "completed"


async def assert_final_mock_state(factory) -> None:
    final = await mock_state(factory)
    (order,) = final["orders"]
    (shipment,) = final["shipments"]
    assert order["status"] == "delivered"
    assert (shipment["status"], shipment["delay_reason"], shipment["escalated"]) == ("delivered", S2_DELAY_REASON, True)
    assert final["messages"] == []


async def assert_supervisor_history(s3: S3, run_id: str) -> None:
    rid = uuid.UUID(run_id)
    async with s3.factory() as session:
        run = await session.get(Run, rid)
        assert run.status == "completed" and run.order_status == "delivered" and run.completed_at is not None

        events = (
            (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at, Event.id)))
            .scalars()
            .all()
        )
        assert [e.event_type for e in events] == ALL_EVENTS

        actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
        assert [(a.action_type, a.status) for a in actions] == [("escalate_shipment", "completed")]
        executions = (
            (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([a.id for a in actions]))))
            .scalars()
            .all()
        )
        assert [(e.tool_name, e.status) for e in executions] == [("escalate_shipment", "success")]
        snapshots = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
        assert len(snapshots) == 2  # the failed attempt wrote none
        outputs = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
        assert len(outputs) == 1 and outputs[0].output["source"] == "fallback"


async def assert_observable_through_the_api(s3: S3, run_id: str) -> None:
    api = s3.api
    run = (await api.get(f"/api/runs/{run_id}")).json()
    assert (run["status"], run["order_status"]) == ("completed", "delivered") and run["completed_at"] is not None

    # The final output is the deterministic fallback, honestly marked, built only from recorded state.
    final = (await api.get(f"/api/runs/{run_id}/final-output")).json()
    output = final["final_output"]
    assert final["created_at"] is not None
    assert set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
    assert output["source"] == "fallback"
    assert output["summary"] == (
        f"Order {ORDER_ID} reached terminal status 'delivered' after 2 reasoning cycle(s) and 5 event(s); "
        f"1 tool execution(s), 0 failed. Last recorded situation: {S3_MEMORY_SUMMARIES[1]}"
    )
    assert output["key_actions"] == ["escalate_shipment: succeeded"]
    (learning,) = output["key_learnings"]
    assert learning.startswith("Generated by the deterministic fallback because LLM final-output generation failed: ")
    assert S3_OUTAGE_MESSAGE in learning
    assert output["recommendations"] == ["Review this run's timeline and tool executions manually."]

    # The final-output LLM WAS asked, with the real run history, and failed.
    final_prompt = prompt(s3, 3)
    assert final_prompt["final_order_status"] == "delivered"
    assert (final_prompt["events_received"], final_prompt["reasoning_count"]) == (5, 2)
    assert [(a["tool"], a["success"]) for a in final_prompt["action_log"]] == [("escalate_shipment", True)]

    actions = (await api.get(f"/api/runs/{run_id}/actions")).json()["actions"]
    assert [(a["action_type"], a["status"]) for a in actions] == [("escalate_shipment", "completed")]
    (execution,) = (await api.get(f"/api/runs/{run_id}/tool-executions")).json()["tool_executions"]
    assert (execution["tool_name"], execution["status"], execution["error"]) == ("escalate_shipment", "success", None)
    assert execution["input"] == {"reason": S2_ESCALATION_REASON, "priority": "high"}
    assert execution["result"] == {
        "order_id": ORDER_ID,
        "shipment_id": shipment_id_for(ORDER_ID),
        "escalated": True,
    }

    snapshots = (await api.get(f"/api/runs/{run_id}/memory")).json()["snapshots"]
    assert [s["memory"]["situation_summary"] for s in snapshots] == S3_MEMORY_SUMMARIES

    entries = (await api.get(f"/api/runs/{run_id}/timeline")).json()["entries"]
    by_type: Dict[str, List[str]] = {}
    for entry in entries:
        by_type.setdefault(entry["entry_type"], []).append(entry["message"])
    assert {k: len(v) for k, v in by_type.items()} == {"event": 5, "decision": 2, "action": 1, "system": 2}
    assert sum(m.startswith("Reasoning failed after retries; no tool executed:") for m in by_type["system"]) == 1
    assert sum("terminal status 'delivered'" in m for m in by_type["system"]) == 1
    decisions = " ".join(by_type["decision"])
    assert "[tool: none]" in decisions and "[tool: escalate_shipment]" in decisions  # no decision for the failed attempt

    closed = await api.get(f"/api/runs/{run_id}/status")  # the workflow is closed
    assert (closed.status_code, closed.json()["code"]) == (409, "RUN_NOT_ACTIVE")
