"""Activity names and input/output contracts (Step 4).

OrderWorkflow calls Activities by these names rather than importing the
Activity implementations, so the workflow's import graph contains only plain
data types: no SQLAlchemy, no LLM client, no tool code.

All contracts are dataclasses with plain-string "enums" (see types.py for why
``str, Enum`` is avoided in Temporal payloads on Python 3.9). Row ids are
generated in the workflow with ``workflow.uuid4()`` and double as idempotency
keys: persistence Activities insert with ``ON CONFLICT DO NOTHING``, so a
retried Activity cannot create a duplicate row.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.temporal.types import OrderEvent

# ---------------------------------------------------------------- names

RECORD_EVENT = "record_event"
RECORD_TIMELINE_ENTRIES = "record_timeline_entries"
RECORD_ACTION_STARTED = "record_action_started"
RECORD_ACTION_FINISHED = "record_action_finished"
SAVE_MEMORY_SNAPSHOT = "save_memory_snapshot"
SAVE_RUN_INSTRUCTIONS = "save_run_instructions"
COMPLETE_RUN = "complete_run"
GENERATE_REASONING_DECISION = "generate_reasoning_decision"
GENERATE_FINAL_OUTPUT = "generate_final_output"
EXECUTE_TOOL = "execute_tool"

# ------------------------------------------------------ persistence I/O


@dataclass
class EventRecord:
    """One incoming event plus its "event received" timeline entry."""

    event_id: str
    timeline_entry_id: str
    run_id: Optional[str]
    event_type: str
    payload: Dict[str, Any]
    occurred_at: datetime
    received_at: datetime
    timeline_message: str
    # Set only when the supervisor's order_status_by_event maps this event.
    resulting_order_status: Optional[str] = None


@dataclass
class TimelineRecord:
    """A system/control timeline entry (pause, resume, interrupt, decision, ...)."""

    entry_id: str
    run_id: Optional[str]
    entry_type: str
    message: str
    created_at: datetime


@dataclass
class TimelineBatch:
    entries: List[TimelineRecord] = field(default_factory=list)


@dataclass
class ActionStartRecord:
    """The AI decided to call a tool: an Action plus its pending ToolExecution."""

    action_id: str
    tool_execution_id: str
    run_id: Optional[str]
    tool_name: str
    tool_input: Dict[str, Any]
    reasoning: str
    started_at: datetime


@dataclass
class ActionFinishRecord:
    """What actually happened when the tool ran."""

    action_id: str
    tool_execution_id: str
    run_id: Optional[str]
    timeline_entry_id: str
    success: bool
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    completed_at: datetime
    timeline_message: str


@dataclass
class MemorySnapshotRecord:
    snapshot_id: str
    run_id: Optional[str]
    memory: Dict[str, Any]
    created_at: datetime


@dataclass
class RunInstructionsRecord:
    """The full, current list of run-specific instructions (replaces the stored list)."""

    run_id: Optional[str]
    instructions: List[Dict[str, Any]]


@dataclass
class CompleteRunRecord:
    final_output_id: str
    run_id: Optional[str]
    output: Dict[str, Any]
    completed_at: datetime


# -------------------------------------------------------------- LLM I/O


@dataclass
class ReasoningInput:
    """Compact reasoning context. Deliberately NOT the full historical timeline."""

    order_id: str
    order_status: Optional[str]
    wake_reason: str
    supervisor_instructions: str
    supervisor_version: Optional[int]
    run_instructions: List[str]
    enabled_tools: List[str]
    memory: Dict[str, Any]
    # Events not yet seen by any reasoning cycle.
    new_events: List[OrderEvent]
    # Bounded window of events already seen.
    recent_events: List[OrderEvent]
    last_action_result: Optional[Dict[str, Any]]
    default_wake_interval_minutes: int
    min_wake_interval_minutes: int
    max_wake_interval_minutes: int
    now: datetime


@dataclass
class ReasoningDecision:
    """Validated structured decision returned by the LLM Activity.

    ``tool`` is a single optional name: the shape itself allows at most one
    tool per cycle. The workflow re-validates everything before acting.
    """

    assessment: str
    tool: Optional[str]
    tool_input: Dict[str, Any]
    next_wake_in_minutes: Optional[int]
    situation_summary: str
    open_concerns: List[str]


@dataclass
class FinalOutputInput:
    order_id: str
    final_order_status: Optional[str]
    supervisor_instructions: str
    run_instructions: List[str]
    memory: Dict[str, Any]
    recent_events: List[OrderEvent]
    action_log: List[Dict[str, Any]]
    events_received: int
    reasoning_count: int


@dataclass
class FinalOutputContent:
    summary: str
    key_actions: List[str]
    key_learnings: List[str]
    recommendations: List[str]


# ------------------------------------------------------------- tool I/O


@dataclass
class ToolRequest:
    # Idempotency key for side-effecting tools.
    tool_execution_id: str
    order_id: str
    tool_name: str
    tool_input: Dict[str, Any]


@dataclass
class ToolResult:
    """``success=False`` is a business-level failure, not an infrastructure crash."""

    success: bool
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
