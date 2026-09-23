"""Test doubles for workflow-level tests.

``InMemoryPersistence`` registers Activities under the SAME names as the real
PostgreSQL ones and records what the workflow asked to persist. Workflow tests
use it with the REAL reasoning Activity (driven by FakeLLMClient) and the REAL
tool Activity (default stubs or injected handlers). The real persistence
Activities are tested separately against PostgreSQL.
"""

from typing import Any, Callable, List, Mapping, Optional

from temporalio import activity

from app.llm.fake import FakeLLMClient
from app.temporal.activities.reasoning import ReasoningActivities
from app.temporal.activities.tools import ToolActivities
from app.temporal.contracts import (
    COMPLETE_RUN,
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
    MemorySnapshotRecord,
    RunInstructionsRecord,
    TimelineBatch,
)
from app.tools.registry import ToolHandler


class InMemoryPersistence:
    def __init__(self) -> None:
        self.events: List[EventRecord] = []
        self.timeline: List[Any] = []
        self.actions_started: List[ActionStartRecord] = []
        self.actions_finished: List[ActionFinishRecord] = []
        self.memory_snapshots: List[MemorySnapshotRecord] = []
        self.run_instructions: List[RunInstructionsRecord] = []
        self.completed_runs: List[CompleteRunRecord] = []

    @activity.defn(name=RECORD_EVENT)
    async def record_event(self, record: EventRecord) -> None:
        self.events.append(record)

    @activity.defn(name=RECORD_TIMELINE_ENTRIES)
    async def record_timeline_entries(self, batch: TimelineBatch) -> None:
        self.timeline.extend(batch.entries)

    @activity.defn(name=RECORD_ACTION_STARTED)
    async def record_action_started(self, record: ActionStartRecord) -> None:
        self.actions_started.append(record)

    @activity.defn(name=RECORD_ACTION_FINISHED)
    async def record_action_finished(self, record: ActionFinishRecord) -> None:
        self.actions_finished.append(record)

    @activity.defn(name=SAVE_MEMORY_SNAPSHOT)
    async def save_memory_snapshot(self, record: MemorySnapshotRecord) -> None:
        self.memory_snapshots.append(record)

    @activity.defn(name=SAVE_RUN_INSTRUCTIONS)
    async def save_run_instructions(self, record: RunInstructionsRecord) -> None:
        self.run_instructions.append(record)

    @activity.defn(name=COMPLETE_RUN)
    async def complete_run(self, record: CompleteRunRecord) -> None:
        self.completed_runs.append(record)

    def activities(self) -> List[Callable[..., Any]]:
        return [
            self.record_event,
            self.record_timeline_entries,
            self.record_action_started,
            self.record_action_finished,
            self.save_memory_snapshot,
            self.save_run_instructions,
            self.complete_run,
        ]

    def timeline_messages(self) -> List[str]:
        return [entry.message for entry in self.timeline]


def fake_activities(
    persistence: InMemoryPersistence,
    llm_client: Optional[FakeLLMClient] = None,
    tool_registry: Optional[Mapping[str, ToolHandler]] = None,
) -> List[Callable[..., Any]]:
    reasoning = ReasoningActivities(llm_client or FakeLLMClient())
    tools = ToolActivities(tool_registry)
    return persistence.activities() + [
        reasoning.generate_reasoning_decision,
        reasoning.generate_final_output,
        tools.execute_tool,
    ]
