"""OrderWorkflow + Activities on Temporal's time-skipping test server.

Real reasoning Activity (driven by FakeLLMClient), real tool Activity (stubs or
injected handlers), in-memory persistence (the PostgreSQL Activities are
tested separately). No network, no API key.
"""

import asyncio
import json
from datetime import timedelta
from typing import Any, List, Optional

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from app.llm.client import LLMAuthenticationError
from app.llm.fake import FakeLLMClient, make_decision_json, make_final_output_json
from app.temporal.constants import TASK_QUEUE, order_workflow_id
from app.temporal.contracts import ToolRequest, ToolResult
from app.temporal.types import OrderEvent, OrderWorkflowInput, OrderWorkflowStatus, WakeReason, WorkflowState
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.fakes import InMemoryPersistence, fake_activities

pytestmark = pytest.mark.asyncio

ALL_TOOLS = ["get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"]


class Harness:
    def __init__(self, env: WorkflowEnvironment, llm: FakeLLMClient, persistence: InMemoryPersistence) -> None:
        self.env = env
        self.llm = llm
        self.persistence = persistence

    async def start(self, **overrides) -> WorkflowHandle:
        values = dict(order_id="12345", run_id="run-1", enabled_tools=list(ALL_TOOLS))
        values.update(overrides)
        order_input = OrderWorkflowInput(**values)
        return await self.env.client.start_workflow(
            OrderWorkflow.run, order_input, id=order_workflow_id(order_input.order_id), task_queue=TASK_QUEUE
        )


async def run_with(test, *, responses: Optional[List[Any]] = None, tools=None):
    llm = FakeLLMClient(responses)
    persistence = InMemoryPersistence()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with create_worker(env.client, activities=fake_activities(persistence, llm, tools)):
            await test(Harness(env, llm, persistence))


async def status_of(handle: WorkflowHandle) -> OrderWorkflowStatus:
    return await handle.query(OrderWorkflow.get_status)


async def wait_for(handle: WorkflowHandle, predicate, attempts: int = 400) -> OrderWorkflowStatus:
    status = None
    for _ in range(attempts):
        status = await status_of(handle)
        if predicate(status):
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last status: {status}")


def settled(count: int):
    return lambda s: s.reasoning_count == count and s.state == WorkflowState.SLEEPING


class Gate:
    """An LLM response that blocks until released (a slow in-flight LLM call)."""

    def __init__(self, response: str) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.response = response

    async def __call__(self, **call) -> str:
        self.entered.set()
        await self.release.wait()
        return self.response


# ------------------------------------------------------------------ tests


async def test_reasoning_cycle_executes_one_tool_and_saves_memory():
    decision = make_decision_json(
        assessment="Need tracking info.",
        tool="get_shipment_status",
        next_wake_in_minutes=30,
        situation_summary="Waiting on carrier.",
        open_concerns=["no tracking yet"],
    )

    async def test(h: Harness):
        handle = await h.start()
        status = await wait_for(handle, settled(1))

        p = h.persistence
        assert [a.tool_name for a in p.actions_started] == ["get_shipment_status"]
        assert len(p.actions_finished) == 1 and p.actions_finished[0].success is True
        assert p.actions_finished[0].result["source"] == "step4_stub"
        assert len(p.memory_snapshots) == 1
        assert status.memory["situation_summary"] == "Waiting on carrier."
        assert status.memory["open_concerns"] == ["no tracking yet"]
        assert status.memory["last_action"] == "get_shipment_status: succeeded"
        assert status.memory["last_wake_reason"] == "workflow_start"
        assert status.last_decision["executed_tool"] == "get_shipment_status"
        assert status.last_cycle_outcome == "completed"
        # Requested 30 minutes, within [5, 1440].
        assert status.next_wake_at - (await h.env.get_current_time()) <= timedelta(minutes=30)

        # The next cycle's context carries the last tool result.
        await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="shipment_delayed"))
        await wait_for(handle, settled(2))
        second_prompt = json.loads(h.llm.calls[1]["user_prompt"])
        assert second_prompt["last_action_result"]["tool"] == "get_shipment_status"
        assert [e["event_type"] for e in second_prompt["new_events"]] == ["shipment_delayed"]

    await run_with(test, responses=[decision])


async def test_every_event_is_persisted_even_when_it_does_not_wake_reasoning():
    async def test(h: Harness):
        handle = await h.start(order_status_by_event={"payment_confirmed": "paid"})
        await wait_for(handle, settled(1))
        await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="payment_confirmed", payload={"amount": 10}))
        await wait_for(handle, lambda s: len(h.persistence.events) == 1)

        record = h.persistence.events[0]
        assert (record.event_type, record.payload, record.run_id) == ("payment_confirmed", {"amount": 10}, "run-1")
        assert record.resulting_order_status == "paid"
        assert "does not wake" in record.timeline_message
        status = await status_of(handle)
        assert status.reasoning_count == 1  # recorded != reasoning wake
        assert len(h.llm.calls) == 1

    await run_with(test)


async def test_decision_asking_for_two_tools_executes_none():
    two_tools = json.loads(make_decision_json())
    two_tools["tool"] = ["get_order_status", "escalate_shipment"]

    async def test(h: Harness):
        handle = await h.start()
        status = await wait_for(handle, lambda s: s.last_cycle_outcome == "llm_failed")
        assert h.persistence.actions_started == []
        assert status.state == WorkflowState.SLEEPING
        assert len(h.llm.calls) == 3  # invalid output is retried, then the cycle fails safely

    await run_with(test, responses=[json.dumps(two_tools)] * 3)


async def test_tool_not_enabled_is_rejected_by_the_workflow():
    decision = make_decision_json(tool="escalate_shipment", reason="late", priority="high")

    async def test(h: Harness):
        handle = await h.start(enabled_tools=["get_order_status"])
        status = await wait_for(handle, settled(1))
        assert h.persistence.actions_started == []
        assert status.last_decision["requested_tool"] == "escalate_shipment"
        assert status.last_decision["executed_tool"] is None
        assert "not enabled" in status.last_decision["rejection_reason"]
        await wait_for(handle, lambda s: any("Tool request rejected" in m for m in h.persistence.timeline_messages()))

    await run_with(test, responses=[decision])


async def test_business_tool_failure_is_recorded_not_hidden():
    async def not_found(request: ToolRequest) -> ToolResult:
        return ToolResult(success=False, output={}, error="shipment not found")

    async def test(h: Harness):
        handle = await h.start()
        status = await wait_for(handle, settled(1))
        finished = h.persistence.actions_finished[0]
        assert (finished.success, finished.error) == (False, "shipment not found")
        assert status.memory["last_action"] == "get_shipment_status: failed"

    await run_with(
        test,
        responses=[make_decision_json(tool="get_shipment_status")],
        tools={"get_shipment_status": not_found},
    )


async def test_side_effecting_tool_crash_is_attempted_once_and_recorded():
    calls = []

    async def crash(request: ToolRequest) -> ToolResult:
        calls.append(request.tool_execution_id)
        raise RuntimeError("SMS gateway down")

    async def test(h: Harness):
        handle = await h.start()
        await wait_for(handle, settled(1))
        assert len(calls) == 1  # never retried: a retry could message the customer twice
        finished = h.persistence.actions_finished[0]
        assert finished.success is False
        assert "SMS gateway down" in finished.error
        assert finished.tool_execution_id == h.persistence.actions_started[0].tool_execution_id

    await run_with(
        test,
        responses=[make_decision_json(tool="send_customer_update", message="Your order is delayed.")],
        tools={"send_customer_update": crash},
    )


async def test_read_only_tool_crash_is_retried():
    calls = []

    async def flaky(request: ToolRequest) -> ToolResult:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return ToolResult(success=True, output={"order_status": "shipped"})

    async def test(h: Harness):
        handle = await h.start()
        await wait_for(handle, settled(1))
        assert len(calls) == 3
        assert h.persistence.actions_finished[0].success is True

    await run_with(test, responses=[make_decision_json(tool="get_order_status")], tools={"get_order_status": flaky})


async def test_llm_failure_keeps_workflow_alive_and_schedules_default_wake():
    async def test(h: Harness):
        handle = await h.start(default_wake_interval_minutes=45)
        status = await wait_for(handle, lambda s: s.last_cycle_outcome == "llm_failed")
        assert status.state == WorkflowState.SLEEPING
        assert status.reasoning_count == 0
        assert status.next_wake_at is not None
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        await wait_for(handle, lambda s: any("Reasoning failed" in m for m in h.persistence.timeline_messages()))

        # The next scheduled wake-up reasons normally again.
        await h.env.sleep(timedelta(minutes=46))
        status = await wait_for(handle, settled(1))
        assert status.last_wake_reason == WakeReason.SCHEDULED_WAKEUP

    await run_with(test, responses=[LLMAuthenticationError("bad key")])


async def test_pause_is_honored_at_the_checkpoint_before_any_tool():
    gate = Gate(make_decision_json(tool="send_customer_update", message="hello"))

    async def test(h: Harness):
        handle = await h.start()
        await asyncio.wait_for(gate.entered.wait(), 10)
        await handle.signal(OrderWorkflow.pause)
        await wait_for(handle, lambda s: s.state == WorkflowState.PAUSED)
        gate.release.set()

        status = await wait_for(handle, lambda s: s.last_cycle_outcome == "discarded_paused")
        assert status.state == WorkflowState.PAUSED
        assert status.reasoning_count == 0
        assert status.next_wake_at is None
        assert h.persistence.actions_started == []  # the decided tool never ran

        await handle.signal(OrderWorkflow.resume)
        status = await wait_for(handle, settled(1))
        assert status.last_wake_reason == WakeReason.RESUME

    await run_with(test, responses=[gate])


async def test_interrupt_abandons_in_flight_reasoning_and_workflow_stays_alive():
    gate = Gate(make_decision_json(tool="send_customer_update", message="hello"))

    async def test(h: Harness):
        handle = await h.start()
        await asyncio.wait_for(gate.entered.wait(), 10)
        await handle.signal(OrderWorkflow.interrupt)

        status = await wait_for(handle, lambda s: s.last_cycle_outcome == "interrupted")
        assert status.state == WorkflowState.SLEEPING
        assert status.reasoning_count == 0
        assert status.interrupt_count == 1
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        gate.release.set()  # the abandoned call finishes; its result is discarded
        await asyncio.sleep(0.2)
        assert h.persistence.actions_started == []

        # Future events still trigger reasoning.
        await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="refund_requested"))
        status = await wait_for(handle, settled(1))
        assert status.last_wake_reason == WakeReason.IMPORTANT_EVENT

    await run_with(test, responses=[gate])


async def test_run_instruction_signal_stores_wakes_and_reaches_the_llm():
    async def test(h: Harness):
        handle = await h.start()
        await wait_for(handle, settled(1))
        await handle.signal(OrderWorkflow.add_run_instruction, "If shipment is delayed, escalate immediately.")

        status = await wait_for(handle, settled(2))
        assert status.last_wake_reason == WakeReason.INSTRUCTION_ADDED
        assert [i.text for i in status.run_instructions] == ["If shipment is delayed, escalate immediately."]
        saved = h.persistence.run_instructions[-1]
        assert saved.run_id == "run-1"
        assert saved.instructions[0]["text"] == "If shipment is delayed, escalate immediately."
        assert "If shipment is delayed, escalate immediately." in h.llm.calls[1]["user_prompt"]

    await run_with(test)


async def test_run_instruction_while_paused_is_stored_without_waking():
    async def test(h: Harness):
        handle = await h.start()
        await wait_for(handle, settled(1))
        await handle.signal(OrderWorkflow.pause)
        await handle.signal(OrderWorkflow.add_run_instruction, "Do not contact the customer without human review.")
        await wait_for(handle, lambda s: len(h.persistence.run_instructions) == 1)
        status = await status_of(handle)
        assert status.state == WorkflowState.PAUSED
        assert status.reasoning_count == 1
        messages = h.persistence.timeline_messages()
        assert "Supervisor paused" in messages
        assert any(m.startswith("Run instruction added") for m in messages)

    await run_with(test)


async def test_terminal_status_generates_and_persists_final_output():
    responses = [
        make_decision_json(),
        make_final_output_json(summary="Delivered on time.", key_learnings=["Carrier was reliable."]),
    ]

    async def test(h: Harness):
        handle = await h.start(order_status_by_event={"delivered": "delivered"})
        await wait_for(handle, settled(1))
        await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="delivered"))
        result = await handle.result()

        assert result.state == WorkflowState.TERMINAL
        assert result.final_output_persisted is True
        assert result.reasoning_count == 1
        (completed,) = h.persistence.completed_runs
        assert completed.run_id == "run-1"
        assert completed.output["summary"] == "Delivered on time."
        assert completed.output["key_learnings"] == ["Carrier was reliable."]
        assert completed.output["source"] == "llm"
        assert set(completed.output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
        assert h.persistence.events[-1].resulting_order_status == "delivered"  # flushed before completion

    await run_with(test, responses=responses)


async def test_final_output_falls_back_deterministically_when_llm_fails():
    async def test(h: Harness):
        handle = await h.start(order_status_by_event={"delivered": "delivered"})
        await wait_for(handle, settled(1))
        await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="delivered"))
        result = await handle.result()

        assert result.final_output_persisted is True
        (completed,) = h.persistence.completed_runs
        assert completed.output["source"] == "fallback"
        assert "12345" in completed.output["summary"]
        assert "fallback" in completed.output["key_learnings"][0]

    await run_with(test, responses=[make_decision_json(), LLMAuthenticationError("bad key")])
