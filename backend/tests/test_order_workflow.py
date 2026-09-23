"""Temporal workflow lifecycle tests for OrderWorkflow (written in Step 3; still valid in Step 4).

These run the real workflow on Temporal's time-skipping test server, so
scheduled wake-ups are exercised with real Temporal timers but without
real wall-clock waiting: ``env.sleep`` advances test-server time.
"""

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import TerminatedError
from temporalio.testing import WorkflowEnvironment

from app.temporal.constants import TASK_QUEUE, order_workflow_id
from app.temporal.types import (
    OrderEvent,
    OrderWorkflowInput,
    OrderWorkflowStatus,
    WakeReason,
    WorkflowState,
)
from app.temporal.worker import create_worker
from app.temporal.workflows import OrderWorkflow
from tests.fakes import InMemoryPersistence, fake_activities

pytestmark = pytest.mark.asyncio

ORDER_ID = "12345"


@pytest_asyncio.fixture
async def env():
    # Step 4: the workflow now calls Activities, so the worker registers them.
    # Persistence is in-memory and the LLM is FakeLLMClient (a no-tool decision
    # by default), so these Step 3 lifecycle tests are unchanged in substance.
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with create_worker(env.client, activities=fake_activities(InMemoryPersistence())):
            yield env


async def start_order(env: WorkflowEnvironment, **overrides) -> WorkflowHandle:
    order_input = OrderWorkflowInput(order_id=ORDER_ID, **overrides)
    return await env.client.start_workflow(
        OrderWorkflow.run,
        order_input,
        id=order_workflow_id(order_input.order_id),
        task_queue=TASK_QUEUE,
    )


async def get_status(handle: WorkflowHandle) -> OrderWorkflowStatus:
    return await handle.query(OrderWorkflow.get_status)


async def wait_for(handle: WorkflowHandle, predicate) -> OrderWorkflowStatus:
    """Poll the workflow's status query until predicate(status) holds."""
    for _ in range(200):
        status = await get_status(handle)
        if predicate(status):
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached; last status: {status}")


async def start_and_settle(env: WorkflowEnvironment, **overrides) -> WorkflowHandle:
    """Start a workflow and wait until its initial reasoning pass has put it to sleep."""
    handle = await start_order(env, **overrides)
    await wait_for(handle, lambda s: s.reasoning_count == 1 and s.state == WorkflowState.SLEEPING)
    return handle


async def test_workflow_starts_with_order_workflow_id(env):
    handle = await start_order(env)

    assert order_workflow_id(ORDER_ID) == "order-12345"
    assert handle.id == "order-12345"
    description = await handle.describe()
    assert description.id == "order-12345"
    assert description.status == WorkflowExecutionStatus.RUNNING


async def test_initial_state_established_on_start(env):
    handle = await start_and_settle(env, order_status="created")

    status = await get_status(handle)
    assert status.order_id == ORDER_ID
    assert status.order_status == "created"
    assert status.state == WorkflowState.SLEEPING
    assert status.last_wake_reason == WakeReason.WORKFLOW_START
    assert status.reasoning_count == 1
    assert status.next_wake_at is not None
    assert status.events_received == 0
    assert status.pending_events == []


async def test_submit_event_signal_reaches_workflow(env):
    handle = await start_and_settle(env)

    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="payment_confirmed"))

    status = await wait_for(handle, lambda s: s.events_received == 1)
    assert [e.event_type for e in status.pending_events] == ["payment_confirmed"]


async def test_signal_delivered_at_workflow_startup_sees_initialized_state(env):
    """Regression: a Signal in the same first activation as the workflow start.

    In the Temporal Python SDK the Signal handler task is queued before the
    workflow's run() task. Signal-with-start delivers exactly that situation,
    and we do NOT wait for the first reasoning pass before signalling. The
    handler reads configuration (order_status_by_event, important_event_types),
    so this only works because OrderWorkflow initializes that state in
    @workflow.init rather than in run(). Without it the handler would fail.
    """
    order_input = OrderWorkflowInput(
        order_id=ORDER_ID,
        order_status_by_event={"shipment_delayed": "shipment_delayed"},
    )
    handle = await env.client.start_workflow(
        OrderWorkflow.run,
        order_input,
        id=order_workflow_id(ORDER_ID),
        task_queue=TASK_QUEUE,
        start_signal="submit_event",
        start_signal_args=[OrderEvent(event_type="shipment_delayed")],
    )

    status = await wait_for(
        handle, lambda s: s.reasoning_count >= 1 and s.state == WorkflowState.SLEEPING
    )
    # Handler used configured state: the event was recorded and the status mapping applied.
    assert status.events_received == 1
    assert status.order_status == "shipment_delayed"
    # Wake decision: the event is important, but the workflow-start reasoning pass
    # already covers it, so it is consumed by that pass instead of causing a second one.
    assert status.reasoning_count == 1
    assert status.last_wake_reason == WakeReason.WORKFLOW_START
    assert status.pending_events == []
    assert [e.event_type for e in status.recent_events] == ["shipment_delayed"]


async def test_order_status_unchanged_without_configured_mapping(env):
    """No built-in event->status mapping: even `delivered` changes nothing by default."""
    handle = await start_and_settle(env, order_status="created")

    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="delivered"))
    status = await wait_for(handle, lambda s: s.events_received == 1)

    assert status.order_status == "created"
    assert status.state == WorkflowState.SLEEPING
    assert status.terminal_order_status_reached is False
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING


async def test_configured_mapping_updates_order_status(env):
    handle = await start_and_settle(env, order_status_by_event={"payment_confirmed": "paid"})

    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="payment_confirmed"))

    status = await wait_for(handle, lambda s: s.order_status == "paid")
    assert status.terminal_order_status_reached is False


async def test_important_event_wakes_reasoning(env):
    handle = await start_and_settle(env)

    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="shipment_delayed"))

    status = await wait_for(handle, lambda s: s.reasoning_count == 2)
    assert status.last_wake_reason == WakeReason.IMPORTANT_EVENT
    assert status.state == WorkflowState.SLEEPING
    assert status.pending_events == []
    assert [e.event_type for e in status.recent_events] == ["shipment_delayed"]


async def test_unimportant_event_is_recorded_but_does_not_wake_reasoning(env):
    handle = await start_and_settle(env)

    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="payment_confirmed"))
    await wait_for(handle, lambda s: s.events_received == 1)
    await env.sleep(timedelta(minutes=10))  # well before the 60-minute scheduled wake-up

    status = await get_status(handle)
    assert status.reasoning_count == 1  # only the workflow-start pass
    assert status.state == WorkflowState.SLEEPING
    assert [e.event_type for e in status.pending_events] == ["payment_confirmed"]  # not discarded


async def test_scheduled_wakeup_uses_temporal_timer(env):
    handle = await start_and_settle(env, default_wake_interval_minutes=60)
    first_wake = (await get_status(handle)).next_wake_at

    await env.sleep(timedelta(minutes=30))
    assert (await get_status(handle)).reasoning_count == 1  # timer has not fired yet

    await env.sleep(timedelta(minutes=31))
    status = await wait_for(handle, lambda s: s.reasoning_count == 2)
    assert status.last_wake_reason == WakeReason.SCHEDULED_WAKEUP
    assert status.state == WorkflowState.SLEEPING
    assert status.next_wake_at > first_wake  # a new wake-up was scheduled


async def test_pause_prevents_reasoning_and_wakeups(env):
    handle = await start_and_settle(env, default_wake_interval_minutes=60)

    await handle.signal(OrderWorkflow.pause)
    await wait_for(handle, lambda s: s.state == WorkflowState.PAUSED)

    # An important event is still recorded, but must not wake reasoning while paused.
    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="shipment_delayed"))
    await wait_for(handle, lambda s: s.events_received == 1)
    # Nor may the scheduled wake-up (would have fired at 60 minutes).
    await env.sleep(timedelta(hours=3))

    status = await get_status(handle)
    assert status.state == WorkflowState.PAUSED
    assert status.reasoning_count == 1
    assert status.next_wake_at is None
    assert [e.event_type for e in status.pending_events] == ["shipment_delayed"]


async def test_resume_releases_pause_and_reevaluates(env):
    handle = await start_and_settle(env)
    await handle.signal(OrderWorkflow.pause)
    await wait_for(handle, lambda s: s.state == WorkflowState.PAUSED)
    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="shipment_delayed"))
    await wait_for(handle, lambda s: s.events_received == 1)

    await handle.signal(OrderWorkflow.resume)

    status = await wait_for(handle, lambda s: s.reasoning_count == 2)
    assert status.last_wake_reason == WakeReason.RESUME
    assert status.state == WorkflowState.SLEEPING
    assert status.pending_events == []  # events recorded during the pause were seen
    assert status.next_wake_at is not None


async def test_interrupt_keeps_workflow_alive(env):
    handle = await start_and_settle(env)

    await handle.signal(OrderWorkflow.interrupt)
    status = await wait_for(handle, lambda s: s.interrupt_count == 1)
    assert status.state == WorkflowState.SLEEPING
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

    # Still responsive to future events after the interrupt.
    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="refund_requested"))
    status = await wait_for(handle, lambda s: s.reasoning_count == 2)
    assert status.last_wake_reason == WakeReason.IMPORTANT_EVENT


async def test_terminate_is_a_client_level_hard_stop(env):
    """Terminate is Temporal's own hard stop (what the API layer will call later),
    not a workflow Signal: the workflow ends TERMINATED, not COMPLETED."""
    handle = await start_and_settle(env)

    await handle.terminate(reason="operator hard stop")

    with pytest.raises(WorkflowFailureError) as exc_info:
        await handle.result()
    assert isinstance(exc_info.value.cause, TerminatedError)
    assert (await handle.describe()).status == WorkflowExecutionStatus.TERMINATED


async def test_terminate_works_while_paused(env):
    handle = await start_and_settle(env)
    await handle.signal(OrderWorkflow.pause)
    await wait_for(handle, lambda s: s.state == WorkflowState.PAUSED)

    await handle.terminate(reason="operator hard stop")

    with pytest.raises(WorkflowFailureError):
        await handle.result()
    assert (await handle.describe()).status == WorkflowExecutionStatus.TERMINATED


async def test_terminal_order_status_stops_workflow_at_terminal_seam(env):
    """Step 3: terminal status detected -> workflow stops at the terminal seam.

    Step 4 added final output after the seam; it is tested in
    test_workflow_activities.py (final output, fallback, complete_run).
    """
    handle = await start_and_settle(env, order_status_by_event={"delivered": "delivered"})

    await handle.signal(OrderWorkflow.submit_event, OrderEvent(event_type="delivered"))
    result = await handle.result()

    assert result.terminal_order_status_reached is True
    assert result.state == WorkflowState.TERMINAL
    assert result.order_status == "delivered"
    assert result.next_wake_at is None  # no further wake-ups scheduled
    assert result.reasoning_count == 1  # no reasoning cycle was started for the terminal event
