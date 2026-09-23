"""Plain data types exchanged with OrderWorkflow (inputs, Signals, query results)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from app.temporal.wake_policy import DEFAULT_IMPORTANT_EVENT_TYPES


class WorkflowState(str, Enum):
    """Conceptual workflow states from the architecture."""

    REASONING = "reasoning"  # transient: a reasoning cycle is running
    SLEEPING = "sleeping"  # waiting for an important event or the next scheduled wake-up
    PAUSED = "paused"  # human pause: no reasoning until resume
    TERMINAL = "terminal"  # a configured terminal order status was detected (see workflows.py)


class WakeReason(str, Enum):
    """Why a reasoning cycle was triggered."""

    WORKFLOW_START = "workflow_start"
    IMPORTANT_EVENT = "important_event"
    SCHEDULED_WAKEUP = "scheduled_wakeup"
    RESUME = "resume"
    INSTRUCTION_ADDED = "instruction_added"


@dataclass
class OrderEvent:
    """An order event delivered through the ``submit_event`` Signal."""

    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None


@dataclass
class RunInstruction:
    """A run-specific human instruction (stored on ``runs.run_instructions``)."""

    text: str
    added_at: Optional[datetime] = None


@dataclass
class OrderWorkflowInput:
    """Initial context for one OrderWorkflow (a compact snapshot of supervisor config)."""

    order_id: str
    order_status: Optional[str] = None
    # Event types that wake the supervisor immediately (the wake policy).
    important_event_types: List[str] = field(default_factory=lambda: list(DEFAULT_IMPORTANT_EVENT_TYPES))
    # Order statuses that end supervision.
    terminal_order_statuses: List[str] = field(default_factory=lambda: ["delivered"])
    # Optional event_type -> order_status updates. Empty by default: the workflow
    # assumes no business mapping. Supplied from the versioned supervisor
    # configuration (supervisors.order_status_by_event, Step 4 B3).
    order_status_by_event: Dict[str, str] = field(default_factory=dict)
    # Scheduling. LLM-requested wake intervals are clamped to [min, max];
    # the default is used when the LLM requests none.
    default_wake_interval_minutes: int = 60
    min_wake_interval_minutes: int = 5
    max_wake_interval_minutes: int = 1440
    # Step 4 (B1). Safe defaults keep Step 3 constructors valid. Without a
    # run_id the real persistence Activities refuse to write (MissingRunId).
    run_id: Optional[str] = None
    supervisor_instructions: str = ""
    # No tools unless the supervisor enables them.
    enabled_tools: List[str] = field(default_factory=list)
    supervisor_version: Optional[int] = None
    run_instructions: List[RunInstruction] = field(default_factory=list)


@dataclass
class OrderWorkflowStatus:
    """Compact operational state of an OrderWorkflow (query result and final result)."""

    order_id: str
    # Plain strings holding WorkflowState / WakeReason values. The SDK's default
    # JSON converter cannot decode `str, Enum` types on Python < 3.11 (it
    # returns a list of characters), so enums are not used in this payload.
    state: str
    order_status: Optional[str]
    next_wake_at: Optional[datetime]
    last_wake_reason: Optional[str]
    reasoning_count: int
    interrupt_count: int
    events_received: int
    # Events recorded but not yet seen by a reasoning cycle.
    pending_events: List[OrderEvent]
    # Bounded window of events already seen by a reasoning cycle.
    recent_events: List[OrderEvent]
    # True once a configured terminal order status was detected. Final output
    # exists only once final_output_persisted is also True.
    terminal_order_status_reached: bool = False
    # Step 4 (B1).
    memory: Dict[str, Any] = field(default_factory=dict)
    # The last reasoning decision and the workflow's verdict on it.
    last_decision: Optional[Dict[str, Any]] = None
    # completed | llm_failed | interrupted | discarded_paused | discarded_terminal
    last_cycle_outcome: Optional[str] = None
    run_instructions: List[RunInstruction] = field(default_factory=list)
    final_output_persisted: bool = False
