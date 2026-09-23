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


@dataclass
class OrderEvent:
    """An order event delivered through the ``submit_event`` Signal."""

    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None


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
    # assumes no business mapping. The real source of order status (tools /
    # database) is decided in a later step.
    order_status_by_event: Dict[str, str] = field(default_factory=dict)
    # Scheduling. Step 3 only uses the default interval. Validation/bounding of
    # LLM-requested wake intervals by min/max becomes active once the LLM
    # requests intervals in a later step; until then min/max are carried, unused.
    default_wake_interval_minutes: int = 60
    min_wake_interval_minutes: int = 5
    max_wake_interval_minutes: int = 1440


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
    # True once a configured terminal order status was detected. This does NOT
    # mean final output exists: final-output generation is a later step.
    terminal_order_status_reached: bool = False
