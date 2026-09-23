"""PostgreSQL persistence Activities (async SQLAlchemy).

Granularity follows transaction boundaries, not tables: each Activity is one
transaction covering records that must appear together.

Idempotency: every row id comes from the workflow (``workflow.uuid4()``), and
inserts use ``ON CONFLICT DO NOTHING``, so a retried Activity never creates a
duplicate. Updates set absolute values and are safe to repeat.

Failures:
- missing run_id or an integrity violation (e.g. unknown run) -> non-retryable
  (a retry cannot fix it; the error surfaces in the workflow);
- anything else (connection loss, timeouts) -> retryable.
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.db.models import Action, Event, FinalOutput, MemorySnapshot, Run, TimelineEntry, ToolExecution
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


def _run_uuid(run_id: Optional[str]) -> uuid.UUID:
    if not run_id:
        raise ApplicationError(
            "run_id is required to persist run data", type="MissingRunId", non_retryable=True
        )
    return uuid.UUID(run_id)


class PersistenceActivities:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    yield session
        except IntegrityError as err:
            raise ApplicationError(
                f"Integrity violation: {err.orig}", type="PersistenceIntegrityError", non_retryable=True
            ) from err

    @activity.defn(name=RECORD_EVENT)
    async def record_event(self, record: EventRecord) -> None:
        run_id = _run_uuid(record.run_id)
        async with self._transaction() as session:
            await session.execute(
                insert(Event)
                .values(
                    id=uuid.UUID(record.event_id),
                    run_id=run_id,
                    event_type=record.event_type,
                    payload=record.payload,
                    occurred_at=record.occurred_at,
                    received_at=record.received_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(
                insert(TimelineEntry)
                .values(
                    id=uuid.UUID(record.timeline_entry_id),
                    run_id=run_id,
                    entry_type="event",
                    message=record.timeline_message,
                    created_at=record.received_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            if record.resulting_order_status is not None:
                await session.execute(
                    update(Run).where(Run.id == run_id).values(order_status=record.resulting_order_status)
                )

    @activity.defn(name=RECORD_TIMELINE_ENTRIES)
    async def record_timeline_entries(self, batch: TimelineBatch) -> None:
        if not batch.entries:
            return
        rows = [
            {
                "id": uuid.UUID(entry.entry_id),
                "run_id": _run_uuid(entry.run_id),
                "entry_type": entry.entry_type,
                "message": entry.message,
                "created_at": entry.created_at,
            }
            for entry in batch.entries
        ]
        async with self._transaction() as session:
            await session.execute(insert(TimelineEntry).values(rows).on_conflict_do_nothing(index_elements=["id"]))

    @activity.defn(name=RECORD_ACTION_STARTED)
    async def record_action_started(self, record: ActionStartRecord) -> None:
        run_id = _run_uuid(record.run_id)
        async with self._transaction() as session:
            await session.execute(
                insert(Action)
                .values(
                    id=uuid.UUID(record.action_id),
                    run_id=run_id,
                    action_type=record.tool_name,
                    status="pending",
                    reasoning=record.reasoning,
                    created_at=record.started_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(
                insert(ToolExecution)
                .values(
                    id=uuid.UUID(record.tool_execution_id),
                    action_id=uuid.UUID(record.action_id),
                    tool_name=record.tool_name,
                    status="pending",
                    input=record.tool_input,
                    started_at=record.started_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )

    @activity.defn(name=RECORD_ACTION_FINISHED)
    async def record_action_finished(self, record: ActionFinishRecord) -> None:
        run_id = _run_uuid(record.run_id)
        async with self._transaction() as session:
            await session.execute(
                update(Action)
                .where(Action.id == uuid.UUID(record.action_id))
                .values(status="completed" if record.success else "failed", completed_at=record.completed_at)
            )
            await session.execute(
                update(ToolExecution)
                .where(ToolExecution.id == uuid.UUID(record.tool_execution_id))
                .values(
                    status="success" if record.success else "failed",
                    result=record.result,
                    error=record.error,
                    completed_at=record.completed_at,
                )
            )
            await session.execute(
                insert(TimelineEntry)
                .values(
                    id=uuid.UUID(record.timeline_entry_id),
                    run_id=run_id,
                    entry_type="action",
                    message=record.timeline_message,
                    created_at=record.completed_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )

    @activity.defn(name=SAVE_MEMORY_SNAPSHOT)
    async def save_memory_snapshot(self, record: MemorySnapshotRecord) -> None:
        run_id = _run_uuid(record.run_id)
        async with self._transaction() as session:
            await session.execute(
                insert(MemorySnapshot)
                .values(id=uuid.UUID(record.snapshot_id), run_id=run_id, memory=record.memory, created_at=record.created_at)
                .on_conflict_do_nothing(index_elements=["id"])
            )

    @activity.defn(name=SAVE_RUN_INSTRUCTIONS)
    async def save_run_instructions(self, record: RunInstructionsRecord) -> None:
        run_id = _run_uuid(record.run_id)
        async with self._transaction() as session:
            await session.execute(update(Run).where(Run.id == run_id).values(run_instructions=record.instructions))

    @activity.defn(name=COMPLETE_RUN)
    async def complete_run(self, record: CompleteRunRecord) -> None:
        run_id = _run_uuid(record.run_id)
        async with self._transaction() as session:
            # final_outputs.run_id is UNIQUE: a repeat is a no-op, never a second row.
            await session.execute(
                insert(FinalOutput)
                .values(id=uuid.UUID(record.final_output_id), run_id=run_id, output=record.output, created_at=record.completed_at)
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
            await session.execute(
                update(Run).where(Run.id == run_id).values(status="completed", completed_at=record.completed_at)
            )
