"""OrderWorkflow: one long-running, durable Temporal workflow per order.

Step 3 foundation only. The workflow owns *when* the supervisor should reason
(start, important event, scheduled wake-up, resume); the reasoning itself is a
placeholder state transition. LLM calls, tools and PostgreSQL writes belong in
Activities added in later steps. This module must stay deterministic: no I/O,
no randomness, time only via ``workflow.now()`` / Temporal timers.

Lifecycle (never a tight loop — every pass either reasons once or blocks on a
durable wait):

    start -> reason -> sleep (Temporal timer) -> wake -> reason -> sleep ...
                          ^ important event / resume wakes early
    pause: checkpointed between cycles, blocks until resume

Human controls
    pause / resume / interrupt are Signals handled here.
    Terminate is deliberately NOT modelled in the workflow: it is Temporal's own
    hard-stop, invoked from the client (``handle.terminate(...)``) by the API
    layer in a later step. Workflow code cannot observe or intercept it.

Terminal seam (see ``_handle_terminal_order_status``)
    Step 3:      terminal order status detected -> workflow stops at the seam
    Later step:  terminal seam -> final-output Activity -> persist final output
                 -> mark run completed -> workflow ends
"""

import asyncio
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.temporal.constants import RECENT_EVENTS_LIMIT
    from app.temporal.types import (
        OrderEvent,
        OrderWorkflowInput,
        OrderWorkflowStatus,
        WakeReason,
        WorkflowState,
    )
    from app.temporal.wake_policy import is_important_event


@workflow.defn
class OrderWorkflow:
    """Supervises exactly one order. Workflow ID convention: ``order-<order_id>``."""

    # @workflow.init runs before any Signal can be delivered, so Signal handlers
    # always see the order's configuration.
    @workflow.init
    def __init__(self, input: OrderWorkflowInput) -> None:
        self._config = input
        self._order_status: Optional[str] = input.order_status
        self._state = WorkflowState.SLEEPING

        # Scheduling
        self._next_wake_at = None
        self._wake_reason: Optional[WakeReason] = None  # set => a reasoning cycle is due
        self._last_wake_reason: Optional[WakeReason] = None
        self._reasoning_count = 0

        # Events (compact operational state only; full history lives in PostgreSQL)
        self._events_received = 0
        self._pending_events: List[OrderEvent] = []
        self._recent_events: List[OrderEvent] = []

        # Human controls
        self._paused = False
        self._interrupt_count = 0

        self._terminal_order_status_reached = False

    # ------------------------------------------------------------------ run

    @workflow.run
    async def run(self, input: OrderWorkflowInput) -> OrderWorkflowStatus:
        # Initial reasoning trigger: workflow start.
        self._wake_reason = WakeReason.WORKFLOW_START

        while not self._order_is_terminal():
            if self._paused:
                await self._wait_while_paused()
            elif self._wake_reason is not None:
                self._run_reasoning_placeholder()
            else:
                await self._sleep_until_wake()

        self._handle_terminal_order_status()
        return self._status()

    # -------------------------------------------------------------- signals

    @workflow.signal(name="submit_event")
    def submit_event(self, event: OrderEvent) -> None:
        """Something happened. Record it; the wake policy decides whether to wake now.

        Fast and side-effect-light: no I/O. Every event is retained, even if it
        does not wake reasoning.
        """
        self._events_received += 1
        self._pending_events.append(event)

        # Order status changes only where the configuration says so; no built-in mapping.
        new_status = self._config.order_status_by_event.get(event.event_type)
        if new_status is not None:
            self._order_status = new_status

        # While paused events are only recorded (resume re-evaluates everything),
        # and once the order is terminal no further reasoning is scheduled.
        if (
            not self._paused
            and not self._order_is_terminal()
            and self._wake_reason is None
            and is_important_event(event.event_type, self._config.important_event_types)
        ):
            self._wake_reason = WakeReason.IMPORTANT_EVENT

    @workflow.signal
    def pause(self) -> None:
        """Stop future reasoning until resume. Honored at the next checkpoint."""
        if self._paused:
            return
        self._paused = True
        self._state = WorkflowState.PAUSED
        self._wake_reason = None  # a queued-but-unstarted cycle does not run while paused
        self._next_wake_at = None  # scheduled wake-ups are suspended while paused

    @workflow.signal
    def resume(self) -> None:
        """Release the pause and re-evaluate the current situation."""
        if not self._paused:
            return
        self._paused = False
        self._state = WorkflowState.SLEEPING
        self._wake_reason = WakeReason.RESUME

    @workflow.signal
    def interrupt(self) -> None:
        """Cancel the current/queued reasoning cycle; the workflow stays alive.

        Step 3 placeholder: reasoning is a synchronous state transition, so the
        only cycle that can be cancelled is one that is due but has not started.
        Cancelling an in-progress LLM/tool reasoning cycle will be implemented
        when real Activities are introduced. Recorded events and the scheduled
        wake-up are untouched.
        """
        self._interrupt_count += 1
        self._wake_reason = None

    # ---------------------------------------------------------------- query

    @workflow.query
    def get_status(self) -> OrderWorkflowStatus:
        return self._status()

    # -------------------------------------------------------------- helpers

    def _order_is_terminal(self) -> bool:
        return (
            self._order_status is not None
            and self._order_status in self._config.terminal_order_statuses
        )

    def _run_reasoning_placeholder(self) -> None:
        """Placeholder for a reasoning cycle (LLM Activity arrives in a later step).

        Consumes pending events, records why we woke, then schedules the next wake-up.
        """
        self._state = WorkflowState.REASONING
        self._last_wake_reason = self._wake_reason
        self._reasoning_count += 1
        self._wake_reason = None

        self._recent_events.extend(self._pending_events)
        self._recent_events = self._recent_events[-RECENT_EVENTS_LIMIT:]
        self._pending_events = []

        self._schedule_next_wake()
        self._state = WorkflowState.SLEEPING

    def _schedule_next_wake(self) -> None:
        """Set the next scheduled wake-up using the configured default interval.

        Extension point: once the LLM requests wake intervals, the requested
        value will be validated against min/max_wake_interval_minutes here.
        """
        self._next_wake_at = workflow.now() + timedelta(
            minutes=self._config.default_wake_interval_minutes
        )

    async def _sleep_until_wake(self) -> None:
        """Durably wait for an important event, a control signal, or the scheduled time."""
        self._state = WorkflowState.SLEEPING
        if self._next_wake_at is None:
            self._schedule_next_wake()

        remaining = self._next_wake_at - workflow.now()
        if remaining <= timedelta(0):
            self._wake_reason = WakeReason.SCHEDULED_WAKEUP
            return

        try:
            # Real Temporal timer. Unimportant events do not reset it, because
            # `remaining` is derived from the fixed next_wake_at on every pass.
            await workflow.wait_condition(
                lambda: self._wake_reason is not None
                or self._paused
                or self._order_is_terminal(),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            if self._wake_reason is None:
                self._wake_reason = WakeReason.SCHEDULED_WAKEUP

    async def _wait_while_paused(self) -> None:
        """Block until resume or terminal; events keep being recorded meanwhile."""
        await workflow.wait_condition(lambda: not self._paused or self._order_is_terminal())

    def _handle_terminal_order_status(self) -> None:
        """TERMINAL SEAM.

        Step 3: a configured terminal order status was detected. Record that,
        stop scheduling wake-ups/reasoning, and let the workflow return.
        NO final output is generated here — that is not implemented yet.

        Later step: this is where the final-output Activity runs, followed by
        persisting the final output and marking the run completed, and only
        then does the workflow end.
        """
        self._terminal_order_status_reached = True
        self._state = WorkflowState.TERMINAL
        self._wake_reason = None
        self._next_wake_at = None

    def _status(self) -> OrderWorkflowStatus:
        return OrderWorkflowStatus(
            order_id=self._config.order_id,
            state=self._state.value,
            order_status=self._order_status,
            next_wake_at=self._next_wake_at,
            last_wake_reason=(
                self._last_wake_reason.value if self._last_wake_reason is not None else None
            ),
            reasoning_count=self._reasoning_count,
            interrupt_count=self._interrupt_count,
            events_received=self._events_received,
            pending_events=list(self._pending_events),
            recent_events=list(self._recent_events),
            terminal_order_status_reached=self._terminal_order_status_reached,
        )
