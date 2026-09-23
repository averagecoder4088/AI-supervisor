"""Temporal worker: hosts OrderWorkflow and its Activities on the task queue.

Run from the backend directory (a Temporal server and PostgreSQL must be reachable):

    python -m app.temporal.worker

Dependencies are built here, never at import time, and can be injected
(tests pass a rolled-back session factory, FakeLLMClient, stub tools, or a
complete fake activity list).
"""

import asyncio
from typing import Any, Callable, List, Mapping, Optional, Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from app.config import get_settings
from app.llm.client import LLMClient, OpenAILLMClient
from app.temporal.activities.persistence import PersistenceActivities
from app.temporal.activities.reasoning import ReasoningActivities
from app.temporal.activities.tools import ToolActivities
from app.temporal.constants import TASK_QUEUE
from app.temporal.workflows import OrderWorkflow
from app.tools.mock_operations import build_mock_tool_handlers
from app.tools.registry import ToolHandler


def build_activities(
    *,
    session_factory: async_sessionmaker,
    llm_client: LLMClient,
    tool_registry: Optional[Mapping[str, ToolHandler]] = None,
) -> List[Callable[..., Any]]:
    persistence = PersistenceActivities(session_factory)
    reasoning = ReasoningActivities(llm_client)
    # Step 5: by default the tools act on the PostgreSQL mock operational state.
    tools = ToolActivities(
        tool_registry if tool_registry is not None else build_mock_tool_handlers(session_factory)
    )
    return [
        persistence.record_event,
        persistence.record_timeline_entries,
        persistence.record_action_started,
        persistence.record_action_finished,
        persistence.save_memory_snapshot,
        persistence.save_run_instructions,
        persistence.complete_run,
        reasoning.generate_reasoning_decision,
        reasoning.generate_final_output,
        tools.execute_tool,
    ]


def create_worker(
    client: Client,
    *,
    activities: Optional[Sequence[Callable[..., Any]]] = None,
    session_factory: Optional[async_sessionmaker] = None,
    llm_client: Optional[LLMClient] = None,
    tool_registry: Optional[Mapping[str, ToolHandler]] = None,
) -> Worker:
    """Build the worker. ``activities`` replaces the whole list (tests); otherwise
    the real Activities are built from the given or default dependencies."""
    if activities is None:
        if session_factory is None:
            from app.db.session import AsyncSessionLocal

            session_factory = AsyncSessionLocal
        activities = build_activities(
            session_factory=session_factory,
            llm_client=llm_client if llm_client is not None else OpenAILLMClient(),
            tool_registry=tool_registry,
        )
    return Worker(client, task_queue=TASK_QUEUE, workflows=[OrderWorkflow], activities=list(activities))


async def run_worker() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await create_worker(client).run()


if __name__ == "__main__":
    asyncio.run(run_worker())
