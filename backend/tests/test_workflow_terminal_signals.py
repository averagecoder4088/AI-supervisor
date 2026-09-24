"""R1: Signals that arrive during terminal handling are persisted, never lost.

The run stays open while the final output is generated and while complete_run
writes it, so the API still accepts events, instructions and controls. Each
test gates one of those two steps, sends Signals while it is in flight, and
checks that everything they queued is persisted before the workflow ends.

Real reasoning Activity (FakeLLMClient), in-memory persistence, Temporal's
time-skipping test server; no PostgreSQL rows are written.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from app.llm.fake import FakeLLMClient, make_decision_json, make_final_output_json
from app.temporal.constants import TASK_QUEUE, order_workflow_id
from app.temporal.contracts import COMPLETE_RUN, CompleteRunRecord
from app.temporal.types import OrderEvent, OrderWorkflowInput, OrderWorkflowStatus, WorkflowState
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.fakes import InMemoryPersistence, fake_activities

pytestmark = pytest.mark.asyncio

INSTRUCTION = "Refund the customer if they ask."
REFUND_PAYLOAD = {"reason": "Changed mind"}


class Gate:
    """Blocks the caller until released; ``entered`` proves it is in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


class GatedFinalOutput(Gate):
    """A final-output LLM response that stays in flight until released."""

    async def __call__(self, **call) -> str:
        await self.wait()
        return make_final_output_json(summary="Delivered; refund requested afterwards.")


class RecordingPersistence(InMemoryPersistence):
    """Snapshots what was already persisted when complete_run is called (optionally gated)."""

    def __init__(self, complete_gate: Optional[Gate] = None) -> None:
        super().__init__()
        self.complete_gate = complete_gate
        self.at_complete_run: Dict[str, List[Any]] = {}

    @activity.defn(name=COMPLETE_RUN)
    async def complete_run(self, record: CompleteRunRecord) -> None:
        self.at_complete_run = {
            "event_types": [e.event_type for e in self.events],
            "timeline": self.timeline_messages(),
            "instructions": [[i["text"] for i in r.instructions] for r in self.run_instructions],
        }
        if self.complete_gate is not None:
            await self.complete_gate.wait()
        self.completed_runs.append(record)


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


async def start(env: WorkflowEnvironment) -> WorkflowHandle:
    order_input = OrderWorkflowInput(
        order_id="R1-001",
        run_id="run-r1",
        order_status_by_event={"delivered": "delivered"},
        important_event_types=["refund_requested"],
    )
    handle = await env.client.start_workflow(
        OrderWorkflow.run, order_input, id=order_workflow_id(order_input.order_id), task_queue=TASK_QUEUE
    )
    await wait_for(handle, lambda s: s.reasoning_count == 1 and s.state == WorkflowState.SLEEPING)
    return handle


async def send_late_signals(handle: WorkflowHandle) -> None:
    """An important event, a run instruction and an interrupt, then wait until the workflow saw them."""
    await handle.signal(
        OrderWorkflow.submit_event, OrderEvent(event_type="refund_requested", payload=REFUND_PAYLOAD)
    )
    await handle.signal(OrderWorkflow.add_run_instruction, INSTRUCTION)
    await handle.signal(OrderWorkflow.interrupt)
    await wait_for(
        handle,
        lambda s: s.events_received == 2 and len(s.run_instructions) == 1 and s.interrupt_count == 1,
    )


def assert_late_records_persisted(persistence: InMemoryPersistence) -> None:
    assert [e.event_type for e in persistence.events] == ["delivered", "refund_requested"]
    late = persistence.events[-1]
    assert late.payload == REFUND_PAYLOAD
    assert late.resulting_order_status is None  # no mapping for refund_requested
    assert late.timeline_message.startswith("Event received: refund_requested")
    messages = persistence.timeline_messages()
    assert f"Run instruction added: {INSTRUCTION}" in messages
    assert "Interrupt received" in messages
    assert [i["text"] for i in persistence.run_instructions[-1].instructions] == [INSTRUCTION]


def assert_terminal_semantics_unchanged(result: OrderWorkflowStatus, persistence, llm: FakeLLMClient) -> None:
    assert result.state == WorkflowState.TERMINAL
    assert result.order_status == "delivered"
    assert result.terminal_order_status_reached is True
    assert result.final_output_persisted is True
    # The late important event and instruction never wake reasoning once terminal.
    assert result.reasoning_count == 1
    assert [c["schema_name"] for c in llm.calls] == ["reasoning_decision", "final_output"]
    (completed,) = persistence.completed_runs
    assert completed.output["source"] == "llm"
    assert completed.output["summary"] == "Delivered; refund requested afterwards."


# ------------------------------------------------------------------ tests


async def test_signals_during_final_output_generation_are_persisted_before_complete_run():
    final_gate = GatedFinalOutput()
    llm = FakeLLMClient([make_decision_json(), final_gate])
    persistence = RecordingPersistence()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with create_worker(env.client, activities=fake_activities(persistence, llm)):
            handle = await start(env)
            await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="delivered"))
            await asyncio.wait_for(final_gate.entered.wait(), timeout=10)

            await send_late_signals(handle)
            # Still in flight: the late records are queued, not yet persisted.
            assert [e.event_type for e in persistence.events] == ["delivered"]
            assert persistence.completed_runs == []

            final_gate.release.set()
            result = await handle.result()

    assert_late_records_persisted(persistence)
    # Persisted BEFORE the run was marked completed.
    snapshot = persistence.at_complete_run
    assert snapshot["event_types"] == ["delivered", "refund_requested"]
    assert f"Run instruction added: {INSTRUCTION}" in snapshot["timeline"]
    assert "Interrupt received" in snapshot["timeline"]
    assert snapshot["instructions"][-1] == [INSTRUCTION]
    # The final-output context was built before the late signals arrived.
    final_prompt = json.loads(llm.calls[1]["user_prompt"])
    assert final_prompt["events_received"] == 1
    assert_terminal_semantics_unchanged(result, persistence, llm)


async def test_signals_during_complete_run_are_persisted_before_the_workflow_ends():
    complete_gate = Gate()
    llm = FakeLLMClient(
        [make_decision_json(), make_final_output_json(summary="Delivered; refund requested afterwards.")]
    )
    persistence = RecordingPersistence(complete_gate=complete_gate)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with create_worker(env.client, activities=fake_activities(persistence, llm)):
            handle = await start(env)
            await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="delivered"))
            await asyncio.wait_for(complete_gate.entered.wait(), timeout=10)

            await send_late_signals(handle)
            assert [e.event_type for e in persistence.events] == ["delivered"]

            complete_gate.release.set()
            result = await handle.result()

    # Not yet persisted when complete_run started, but drained before the workflow ended.
    assert persistence.at_complete_run["event_types"] == ["delivered"]
    assert_late_records_persisted(persistence)
    assert_terminal_semantics_unchanged(result, persistence, llm)
