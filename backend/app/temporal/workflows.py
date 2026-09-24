"""OrderWorkflow: one long-running, durable Temporal workflow per order.

The workflow owns *when* the supervisor reasons (start, important event,
scheduled wake-up, resume, run instruction added) and *what is allowed to
happen*. All external work runs in Activities, called by name (see
``contracts.py``) so this module imports only plain data types. It must stay
deterministic: no I/O, no randomness, time only via ``workflow.now()`` /
Temporal timers, ids only via ``workflow.uuid4()``.

Lifecycle (never a tight loop; each pass flushes, reasons once, or blocks):

    loop: flush records -> terminal? -> paused? -> reason (if due) -> sleep
    reasoning cycle:
        LLM Activity -> checkpoint (pause / interrupt / terminal)
        -> validate decision -> at most ONE tool (Activities)
        -> memory snapshot -> schedule next wake
    terminal: flush -> final-output Activity (or deterministic fallback)
        -> complete_run Activity -> workflow ends

Signal handlers stay synchronous and side-effect-free: they only update state
and queue records; the main loop persists them through Activities.

Human controls
    pause / resume / interrupt / add_run_instruction are Signals.
    Terminate is NOT a Signal: it is Temporal's client-side hard stop
    (``handle.terminate(...)``), which workflow code cannot observe.
"""

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from app.temporal.constants import ACTION_LOG_LIMIT, RECENT_EVENTS_LIMIT
    from app.temporal.contracts import (
        COMPLETE_RUN,
        EXECUTE_TOOL,
        GENERATE_FINAL_OUTPUT,
        GENERATE_REASONING_DECISION,
        RECORD_ACTION_FINISHED,
        RECORD_ACTION_STARTED,
        RECORD_EVENT,
        RECORD_TIMELINE_ENTRIES,
        SAVE_MEMORY_SNAPSHOT,
        SAVE_RUN_INSTRUCTIONS,
        ActionFinishRecord,
        ActionStartRecord,
        CompleteRunRecord,
        EventRecord,
        FinalOutputContent,
        FinalOutputInput,
        MemorySnapshotRecord,
        ReasoningDecision,
        ReasoningInput,
        RunInstructionsRecord,
        TimelineBatch,
        TimelineRecord,
        ToolRequest,
        ToolResult,
    )
    from app.temporal.decision_rules import (
        fallback_final_output,
        initial_memory,
        updated_memory,
        validate_decision,
    )
    from app.temporal.types import (
        OrderEvent,
        OrderWorkflowInput,
        OrderWorkflowStatus,
        RunInstruction,
        WakeReason,
        WorkflowState,
    )
    from app.temporal.wake_policy import is_important_event
    from app.tools.registry import TOOL_SPECS

# ------------------------------------------------ Activity timeouts / retries
# Persistence: idempotent (row ids from workflow.uuid4() + ON CONFLICT), so retried.
PERSISTENCE_TIMEOUT = timedelta(seconds=10)
PERSISTENCE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=10), maximum_attempts=5
)
# LLM: limited retries; invalid output is retried because a retry re-asks the model.
REASONING_TIMEOUT = timedelta(seconds=60)
FINAL_OUTPUT_TIMEOUT = timedelta(seconds=90)
LLM_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)
# Tools: read-only tools are retried; side-effecting tools get exactly one attempt
# so a retry can never duplicate a customer message or an escalation.
TOOL_TIMEOUT = timedelta(seconds=10)
READ_TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)
SIDE_EFFECT_TOOL_RETRY = RetryPolicy(maximum_attempts=1)


def _failure_message(err: BaseException) -> str:
    """The most specific message in a Temporal failure chain."""
    cause: Optional[BaseException] = err
    while getattr(cause, "cause", None) is not None:
        cause = cause.cause  # type: ignore[union-attr]
    return str(cause) or type(cause).__name__


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
        self._pending_events: List[OrderEvent] = []  # not yet seen by reasoning
        self._recent_events: List[OrderEvent] = []  # bounded window already seen

        # Human controls
        self._paused = False
        self._interrupt_count = 0

        # Run-specific instructions (source of truth: runs.run_instructions)
        self._run_instructions: List[RunInstruction] = list(input.run_instructions)
        self._run_instructions_dirty = False

        # Reasoning state
        self._memory: Dict[str, Any] = initial_memory()
        self._last_decision: Optional[Dict[str, Any]] = None
        self._last_cycle_outcome: Optional[str] = None
        self._last_action_result: Optional[Dict[str, Any]] = None
        self._action_log: List[Dict[str, Any]] = []
        self._tool_in_progress = False

        # Records queued by Signal handlers, persisted by the main loop.
        self._unpersisted_events: List[EventRecord] = []
        self._pending_timeline: List[TimelineRecord] = []

        self._terminal_order_status_reached = False
        self._final_output_persisted = False

    # ------------------------------------------------------------------ run

    @workflow.run
    async def run(self, input: OrderWorkflowInput) -> OrderWorkflowStatus:
        # Initial reasoning trigger: workflow start.
        self._wake_reason = WakeReason.WORKFLOW_START

        while True:
            await self._flush_records()
            if self._order_is_terminal():
                break
            if self._paused:
                await self._wait_while_paused()
            elif self._wake_reason is not None:
                await self._run_reasoning_cycle()
            else:
                await self._sleep_until_wake()

        await self._handle_terminal_order_status()
        return self._status()

    # -------------------------------------------------------------- signals

    @workflow.signal(name="submit_event")
    def submit_event(self, event: OrderEvent) -> None:
        """Something happened. Record it; the wake policy decides whether to wake now.

        No I/O here: the event is queued and persisted by the main loop, even if
        it does not wake reasoning (event recorded != reasoning wake).
        """
        received_at = workflow.now()
        self._events_received += 1
        self._pending_events.append(event)

        # Order status changes only where the configuration says so; no built-in mapping.
        new_status = self._config.order_status_by_event.get(event.event_type)
        if new_status is not None:
            self._order_status = new_status

        important = is_important_event(event.event_type, self._config.important_event_types)
        # While paused events are only recorded (resume re-evaluates everything),
        # and once the order is terminal no further reasoning is scheduled.
        if not self._paused and not self._order_is_terminal() and self._wake_reason is None and important:
            self._wake_reason = WakeReason.IMPORTANT_EVENT

        message = f"Event received: {event.event_type}"
        message += " (important: wakes the supervisor)" if important else " (recorded; does not wake the supervisor)"
        if new_status is not None:
            message += f"; order status -> {new_status}"
        self._unpersisted_events.append(
            EventRecord(
                event_id=str(workflow.uuid4()),
                timeline_entry_id=str(workflow.uuid4()),
                run_id=self._config.run_id,
                event_type=event.event_type,
                payload=dict(event.payload),
                occurred_at=event.occurred_at or received_at,
                received_at=received_at,
                timeline_message=message,
                resulting_order_status=new_status,
            )
        )

    @workflow.signal
    def pause(self) -> None:
        """Stop future reasoning until resume. Honored at the next checkpoint."""
        if self._paused:
            return
        self._paused = True
        self._state = WorkflowState.PAUSED
        self._wake_reason = None  # a queued-but-unstarted cycle does not run while paused
        self._next_wake_at = None  # scheduled wake-ups are suspended while paused
        self._queue_timeline("control", "Supervisor paused")

    @workflow.signal
    def resume(self) -> None:
        """Release the pause and re-evaluate the current situation."""
        if not self._paused:
            return
        self._paused = False
        self._state = WorkflowState.SLEEPING
        self._wake_reason = WakeReason.RESUME
        self._queue_timeline("control", "Supervisor resumed; re-evaluating")

    @workflow.signal
    def interrupt(self) -> None:
        """Cancel the current/queued reasoning cycle; the workflow stays alive.

        - LLM call in flight: it is abandoned and its decision is discarded.
        - Tool already running: it is allowed to finish (its external effect
          cannot be undone), its outcome is recorded, and the current cycle then
          completes normally. Completed tool work is never discarded retroactively.
        - Sleeping: a queued-but-unstarted wake is cleared.
        """
        self._interrupt_count += 1
        self._wake_reason = None
        if self._tool_in_progress:
            self._queue_timeline("control", "Interrupt received while a tool was executing; the tool is not cancelled")
        else:
            self._queue_timeline("control", "Interrupt received")

    @workflow.signal
    def add_run_instruction(self, instruction: str) -> None:
        """Add a run-specific instruction; it becomes part of the reasoning context.

        Stored on ``runs.run_instructions`` via an Activity from the main loop.
        Wakes a sleeping supervisor to re-evaluate (not while paused/terminal).
        """
        text = (instruction or "").strip()
        if not text:
            return
        self._run_instructions.append(RunInstruction(text=text, added_at=workflow.now()))
        self._run_instructions_dirty = True
        self._queue_timeline("instruction", f"Run instruction added: {text}")
        if not self._paused and not self._order_is_terminal() and self._wake_reason is None:
            self._wake_reason = WakeReason.INSTRUCTION_ADDED

    # ---------------------------------------------------------------- query

    @workflow.query
    def get_status(self) -> OrderWorkflowStatus:
        return self._status()

    # ------------------------------------------------------------- helpers

    def _order_is_terminal(self) -> bool:
        return (
            self._order_status is not None
            and self._order_status in self._config.terminal_order_statuses
        )

    def _queue_timeline(self, entry_type: str, message: str) -> None:
        self._pending_timeline.append(
            TimelineRecord(
                entry_id=str(workflow.uuid4()),
                run_id=self._config.run_id,
                entry_type=entry_type,
                message=message,
                created_at=workflow.now(),
            )
        )

    def _has_unflushed_records(self) -> bool:
        return bool(self._unpersisted_events or self._pending_timeline or self._run_instructions_dirty)

    async def _persist(self, activity_name: str, arg: Any) -> None:
        await workflow.execute_activity(
            activity_name,
            arg,
            start_to_close_timeout=PERSISTENCE_TIMEOUT,
            retry_policy=PERSISTENCE_RETRY,
        )

    async def _flush_records(self) -> None:
        """Persist everything Signal handlers queued. Items are removed only once saved."""
        while self._unpersisted_events:
            await self._persist(RECORD_EVENT, self._unpersisted_events[0])
            self._unpersisted_events.pop(0)
        if self._pending_timeline:
            batch = list(self._pending_timeline)
            await self._persist(RECORD_TIMELINE_ENTRIES, TimelineBatch(entries=batch))
            del self._pending_timeline[: len(batch)]
        if self._run_instructions_dirty:
            self._run_instructions_dirty = False
            await self._persist(
                SAVE_RUN_INSTRUCTIONS,
                RunInstructionsRecord(
                    run_id=self._config.run_id,
                    instructions=[
                        {"text": i.text, "added_at": i.added_at.isoformat() if i.added_at else None}
                        for i in self._run_instructions
                    ],
                ),
            )

    def _schedule_next_wake(self, minutes: Optional[int] = None) -> None:
        self._next_wake_at = workflow.now() + timedelta(
            minutes=minutes if minutes is not None else self._config.default_wake_interval_minutes
        )

    async def _sleep_until_wake(self) -> None:
        """Durably wait for a wake trigger, a control signal, records to flush, or the timer."""
        self._state = WorkflowState.SLEEPING
        if self._next_wake_at is None:
            self._schedule_next_wake()

        remaining = self._next_wake_at - workflow.now()
        if remaining <= timedelta(0):
            self._wake_reason = WakeReason.SCHEDULED_WAKEUP
            return

        try:
            # Real Temporal timer. Records to flush end the wait early only so the
            # loop can persist them; `remaining` is recomputed from the fixed
            # next_wake_at, so unimportant events never move the wake-up.
            await workflow.wait_condition(
                lambda: self._wake_reason is not None
                or self._paused
                or self._order_is_terminal()
                or self._has_unflushed_records(),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            if self._wake_reason is None:
                self._wake_reason = WakeReason.SCHEDULED_WAKEUP

    async def _wait_while_paused(self) -> None:
        """Block until resume or terminal; events keep being recorded meanwhile."""
        await workflow.wait_condition(
            lambda: not self._paused or self._order_is_terminal() or self._has_unflushed_records()
        )

    # ------------------------------------------------------- reasoning cycle

    def _reasoning_input(self, wake_reason: WakeReason, new_events: List[OrderEvent]) -> ReasoningInput:
        cfg = self._config
        return ReasoningInput(
            order_id=cfg.order_id,
            order_status=self._order_status,
            wake_reason=wake_reason.value,
            supervisor_instructions=cfg.supervisor_instructions,
            supervisor_version=cfg.supervisor_version,
            run_instructions=[i.text for i in self._run_instructions],
            enabled_tools=list(cfg.enabled_tools),
            memory=dict(self._memory),
            new_events=list(new_events),
            recent_events=list(self._recent_events),
            last_action_result=self._last_action_result,
            default_wake_interval_minutes=cfg.default_wake_interval_minutes,
            min_wake_interval_minutes=cfg.min_wake_interval_minutes,
            max_wake_interval_minutes=cfg.max_wake_interval_minutes,
            now=workflow.now(),
        )

    def _end_cycle_without_decision(self, outcome: str, message: str) -> None:
        """Cycle produced no usable decision: no tool, events stay pending for next time."""
        self._last_cycle_outcome = outcome
        self._queue_timeline("system", message)
        if self._paused:
            self._state = WorkflowState.PAUSED
        else:
            self._schedule_next_wake()
            self._state = WorkflowState.SLEEPING

    async def _run_reasoning_cycle(self) -> None:
        wake_reason = self._wake_reason
        self._wake_reason = None
        self._state = WorkflowState.REASONING
        interrupts_at_start = self._interrupt_count
        new_events = list(self._pending_events)  # snapshot; later arrivals stay pending

        handle = workflow.start_activity(
            GENERATE_REASONING_DECISION,
            self._reasoning_input(wake_reason, new_events),
            result_type=ReasoningDecision,
            start_to_close_timeout=REASONING_TIMEOUT,
            retry_policy=LLM_RETRY,
            # An HTTP call cannot heartbeat, so interrupt abandons it instead of
            # waiting for acknowledgement; a late result is discarded.
            cancellation_type=workflow.ActivityCancellationType.ABANDON,
        )
        await workflow.wait_condition(lambda: handle.done() or self._interrupt_count != interrupts_at_start)
        if not handle.done():
            handle.cancel()
            try:
                await handle
            except (ActivityError, asyncio.CancelledError):
                pass
            self._end_cycle_without_decision(
                "interrupted", "Reasoning cycle interrupted; the in-flight LLM call was abandoned"
            )
            return

        try:
            decision: ReasoningDecision = await handle
        except ActivityError as err:
            self._end_cycle_without_decision(
                "llm_failed", f"Reasoning failed after retries; no tool executed: {_failure_message(err)}"
            )
            return

        # Checkpoint: pause / interrupt / terminal are honored before any action.
        if self._interrupt_count != interrupts_at_start:
            self._end_cycle_without_decision("interrupted", "Decision discarded: interrupted before any action")
            return
        if self._paused:
            self._end_cycle_without_decision("discarded_paused", "Decision discarded: supervisor paused before any action")
            return
        if self._order_is_terminal():
            self._last_cycle_outcome = "discarded_terminal"
            self._queue_timeline("system", "Decision discarded: order reached a terminal status")
            return

        cfg = self._config
        validated = validate_decision(
            decision,
            enabled_tools=cfg.enabled_tools,
            default_wake=cfg.default_wake_interval_minutes,
            min_wake=cfg.min_wake_interval_minutes,
            max_wake=cfg.max_wake_interval_minutes,
        )
        if validated.rejection_reason is not None:
            self._queue_timeline("system", f"Tool request rejected: {validated.rejection_reason}")

        # One tool maximum: a single optional tool, executed at most once.
        tool_outcome: Optional[Dict[str, Any]] = None
        if validated.tool is not None:
            tool_outcome = await self._execute_tool(validated.tool, validated.tool_input, decision.assessment)

        cycle_count = self._reasoning_count + 1
        last_action = None
        if tool_outcome is not None:
            last_action = f"{tool_outcome['tool']}: {'succeeded' if tool_outcome['success'] else 'failed'}"
        new_memory = updated_memory(
            decision, last_action=last_action, wake_reason=wake_reason.value, cycle_count=cycle_count
        )
        await self._persist(
            SAVE_MEMORY_SNAPSHOT,
            MemorySnapshotRecord(
                snapshot_id=str(workflow.uuid4()),
                run_id=cfg.run_id,
                memory=new_memory,
                created_at=workflow.now(),
            ),
        )

        # Commit the cycle (no awaits below, so queries never see a half-finished cycle).
        self._memory = new_memory
        self._reasoning_count = cycle_count
        self._last_wake_reason = wake_reason
        self._recent_events = (self._recent_events + new_events)[-RECENT_EVENTS_LIMIT:]
        self._pending_events = self._pending_events[len(new_events):]
        if tool_outcome is not None:
            self._last_action_result = tool_outcome
        self._last_decision = {
            "assessment": decision.assessment,
            "requested_tool": decision.tool,
            "executed_tool": validated.tool,
            "rejection_reason": validated.rejection_reason,
            "next_wake_in_minutes": validated.wake_minutes,
        }
        self._last_cycle_outcome = "completed"
        tool_note = validated.tool or "none"
        self._queue_timeline(
            "decision", f"Reasoning ({wake_reason.value}): {decision.assessment} [tool: {tool_note}]"
        )
        if self._paused:
            self._state = WorkflowState.PAUSED
        else:
            self._schedule_next_wake(validated.wake_minutes)
            self._state = WorkflowState.SLEEPING

    async def _execute_tool(self, tool: str, tool_input: Dict[str, Any], assessment: str) -> Dict[str, Any]:
        """Record the decision, run exactly one tool, record what actually happened."""
        cfg = self._config
        action_id = str(workflow.uuid4())
        tool_execution_id = str(workflow.uuid4())
        await self._persist(
            RECORD_ACTION_STARTED,
            ActionStartRecord(
                action_id=action_id,
                tool_execution_id=tool_execution_id,
                run_id=cfg.run_id,
                tool_name=tool,
                tool_input=tool_input,
                reasoning=assessment,
                started_at=workflow.now(),
            ),
        )

        side_effecting = TOOL_SPECS[tool].side_effecting
        self._tool_in_progress = True
        try:
            result: ToolResult = await workflow.execute_activity(
                EXECUTE_TOOL,
                ToolRequest(
                    tool_execution_id=tool_execution_id,
                    order_id=cfg.order_id,
                    tool_name=tool,
                    tool_input=tool_input,
                ),
                result_type=ToolResult,
                start_to_close_timeout=TOOL_TIMEOUT,
                retry_policy=SIDE_EFFECT_TOOL_RETRY if side_effecting else READ_TOOL_RETRY,
            )
        except ActivityError as err:
            result = ToolResult(success=False, output={}, error=_failure_message(err))
        finally:
            self._tool_in_progress = False

        message = f"Tool {tool} {'succeeded' if result.success else 'failed'}"
        if result.error:
            message += f": {result.error}"
        await self._persist(
            RECORD_ACTION_FINISHED,
            ActionFinishRecord(
                action_id=action_id,
                tool_execution_id=tool_execution_id,
                run_id=cfg.run_id,
                timeline_entry_id=str(workflow.uuid4()),
                success=result.success,
                result=result.output if result.success else (result.output or None),
                error=result.error,
                completed_at=workflow.now(),
                timeline_message=message,
            ),
        )
        outcome = {"tool": tool, "success": result.success, "output": result.output, "error": result.error}
        self._action_log = (self._action_log + [outcome])[-ACTION_LOG_LIMIT:]
        return outcome

    # --------------------------------------------------------------- terminal

    async def _handle_terminal_order_status(self) -> None:
        """TERMINAL: final output -> persist it + mark run completed -> workflow ends.

        A failed LLM summary never undoes the terminal state: after retries the
        deterministic fallback (built from recorded state) is persisted instead.
        """
        self._terminal_order_status_reached = True
        self._state = WorkflowState.TERMINAL
        self._wake_reason = None
        self._next_wake_at = None
        self._queue_timeline("system", f"Order reached terminal status {self._order_status!r}")
        await self._flush_records()

        cfg = self._config
        try:
            content: FinalOutputContent = await workflow.execute_activity(
                GENERATE_FINAL_OUTPUT,
                FinalOutputInput(
                    order_id=cfg.order_id,
                    final_order_status=self._order_status,
                    supervisor_instructions=cfg.supervisor_instructions,
                    run_instructions=[i.text for i in self._run_instructions],
                    memory=dict(self._memory),
                    recent_events=(self._recent_events + self._pending_events)[-RECENT_EVENTS_LIMIT:],
                    action_log=list(self._action_log),
                    events_received=self._events_received,
                    reasoning_count=self._reasoning_count,
                ),
                result_type=FinalOutputContent,
                start_to_close_timeout=FINAL_OUTPUT_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
            source = "llm"
        except ActivityError as err:
            content = fallback_final_output(
                order_id=cfg.order_id,
                final_order_status=self._order_status,
                memory=self._memory,
                action_log=self._action_log,
                events_received=self._events_received,
                reasoning_count=self._reasoning_count,
                failure=_failure_message(err),
            )
            source = "fallback"

        # Signals keep arriving while the final output is generated (the run is
        # still open); persist what they queued so no event is silently lost.
        await self._flush_records()
        await self._persist(
            COMPLETE_RUN,
            CompleteRunRecord(
                final_output_id=str(workflow.uuid4()),
                run_id=cfg.run_id,
                output={
                    "summary": content.summary,
                    "key_actions": list(content.key_actions),
                    "key_learnings": list(content.key_learnings),
                    "recommendations": list(content.recommendations),
                    "source": source,
                },
                completed_at=workflow.now(),
            ),
        )
        self._final_output_persisted = True
        # Drain anything queued while complete_run was in flight before the workflow ends.
        while self._has_unflushed_records():
            await self._flush_records()

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
            memory=dict(self._memory),
            last_decision=self._last_decision,
            last_cycle_outcome=self._last_cycle_outcome,
            run_instructions=list(self._run_instructions),
            final_output_persisted=self._final_output_persisted,
        )
