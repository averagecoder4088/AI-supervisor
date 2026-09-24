"""HTTP request/response schemas (Pydantic).

Request schemas check shape only. Supervisor configuration semantics are
validated by the shared Step 4 validator (``validate_supervisor_config``),
not re-implemented here.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.temporal.constants import EVENT_TYPES


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupervisorCreate(_Request):
    name: str = Field(min_length=1)
    description: Optional[str] = None
    instructions: str = Field(min_length=1)
    wake_policy: Dict[str, Any]
    enabled_tools: List[str]
    default_wake_interval_minutes: int
    min_wake_interval_minutes: int
    max_wake_interval_minutes: int
    terminal_order_statuses: List[str]
    # Optional event_type -> order_status mapping (Step 4 B3; Step 6 decision C).
    order_status_by_event: Dict[str, str] = Field(default_factory=dict)


class SupervisorOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    instructions: str
    wake_policy: Dict[str, Any]
    enabled_tools: List[str]
    default_wake_interval_minutes: int
    min_wake_interval_minutes: int
    max_wake_interval_minutes: int
    terminal_order_statuses: List[str]
    order_status_by_event: Dict[str, str]
    version: int
    created_at: datetime
    updated_at: datetime


class RunCreate(_Request):
    order_id: str = Field(min_length=1)
    supervisor_id: uuid.UUID
    run_instructions: List[str] = Field(default_factory=list)

    @field_validator("order_id")
    @classmethod
    def _order_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("order_id must not be blank")
        return value

    @field_validator("run_instructions")
    @classmethod
    def _instructions_not_blank(cls, value: List[str]) -> List[str]:
        if any(not item.strip() for item in value):
            raise ValueError("run instructions must be non-empty strings")
        return [item.strip() for item in value]


class RunOut(BaseModel):
    id: uuid.UUID
    order_id: str
    supervisor_id: uuid.UUID
    # Read from the linked immutable supervisor row (decision A), not a runs column.
    supervisor_version: int
    status: str
    order_status: Optional[str]
    run_instructions: List[Dict[str, Any]]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class EventCreate(_Request):
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _known_event_type(cls, value: str) -> str:
        if value not in EVENT_TYPES:
            raise ValueError(f"unknown event_type {value!r}; allowed: {sorted(EVENT_TYPES)}")
        return value


class InstructionCreate(_Request):
    instruction: str = Field(min_length=1)

    @field_validator("instruction")
    @classmethod
    def _instruction_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instruction must not be blank")
        return value.strip()


class Accepted(BaseModel):
    """202 body: the request reached the workflow boundary. It is NOT yet processed."""

    run_id: uuid.UUID
    request: str
    accepted: bool = True


# ------------------------------------------------------------------ Step 7
# Read-only observation responses. Fields mirror the existing models exactly.


class _FromModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TimelineEntryOut(_FromModel):
    id: uuid.UUID
    run_id: uuid.UUID
    entry_type: str
    message: str
    created_at: datetime


class TimelineOut(BaseModel):
    run_id: uuid.UUID
    entries: List[TimelineEntryOut]


class MemorySnapshotOut(_FromModel):
    id: uuid.UUID
    run_id: uuid.UUID
    memory: Dict[str, Any]
    created_at: datetime


class MemoryOut(BaseModel):
    run_id: uuid.UUID
    snapshots: List[MemorySnapshotOut]


class ActionOut(_FromModel):
    id: uuid.UUID
    run_id: uuid.UUID
    action_type: str
    status: str
    reasoning: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]


class ActionsOut(BaseModel):
    run_id: uuid.UUID
    actions: List[ActionOut]


class ToolExecutionOut(_FromModel):
    id: uuid.UUID
    action_id: uuid.UUID
    tool_name: str
    status: str
    input: Dict[str, Any]
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]


class ToolExecutionsOut(BaseModel):
    run_id: uuid.UUID
    tool_executions: List[ToolExecutionOut]


class FinalOutputOut(BaseModel):
    run_id: uuid.UUID
    # The stored Step 4 structure: summary, key_actions, key_learnings,
    # recommendations, source ("llm" | "fallback"). null until the run completes.
    final_output: Optional[Dict[str, Any]]
    created_at: Optional[datetime]
