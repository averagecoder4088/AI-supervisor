"""Persistence Activities against the REAL local PostgreSQL, fully rolled back.

Each test runs inside an outer transaction (see conftest.db_session_factory);
nothing survives the test.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from app.db.models import Action, Event, FinalOutput, MemorySnapshot, Run, Supervisor, TimelineEntry, ToolExecution
from app.temporal.activities.persistence import PersistenceActivities
from app.temporal.contracts import (
    ActionFinishRecord,
    ActionStartRecord,
    CompleteRunRecord,
    EventRecord,
    MemorySnapshotRecord,
    RunInstructionsRecord,
    TimelineBatch,
    TimelineRecord,
)

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def run_id(db_session_factory) -> str:
    async with db_session_factory() as session:
        supervisor = Supervisor(
            name=f"test-{uuid.uuid4().hex[:8]}",
            instructions="Supervise.",
            wake_policy={"important_event_types": ["shipment_delayed"]},
            enabled_tools=["get_order_status"],
            default_wake_interval=60,
            min_wake_interval=5,
            max_wake_interval=1440,
            terminal_order_statuses=["delivered"],
            order_status_by_event={"delivered": "delivered"},
            version=1,
        )
        session.add(supervisor)
        await session.flush()
        run = Run(order_id=f"order-{uuid.uuid4().hex[:8]}", supervisor_id=supervisor.id)
        session.add(run)
        await session.commit()
        return str(run.id)


def _event(run_id: str, status=None, **overrides) -> EventRecord:
    values = dict(
        event_id=_id(),
        timeline_entry_id=_id(),
        run_id=run_id,
        event_type="delivered",
        payload={"carrier": "x"},
        occurred_at=T0,
        received_at=T0 + timedelta(seconds=5),
        timeline_message="Event received: delivered",
        resulting_order_status=status,
    )
    values.update(overrides)
    return EventRecord(**values)


async def _count(factory, model, *where) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _get(factory, model, row_id):
    async with factory() as session:
        return await session.get(model, uuid.UUID(row_id) if isinstance(row_id, str) else row_id)


async def test_record_event_writes_event_timeline_and_mapped_status(db_session_factory, run_id):
    p = PersistenceActivities(db_session_factory)
    record = _event(run_id, status="delivered")
    await p.record_event(record)

    event = await _get(db_session_factory, Event, record.event_id)
    assert event.event_type == "delivered"
    assert event.payload == {"carrier": "x"}
    assert event.occurred_at == T0
    assert event.received_at == T0 + timedelta(seconds=5)
    entry = await _get(db_session_factory, TimelineEntry, record.timeline_entry_id)
    assert entry.entry_type == "event"
    assert entry.message == "Event received: delivered"
    assert (await _get(db_session_factory, Run, run_id)).order_status == "delivered"


async def test_record_event_without_mapping_leaves_order_status(db_session_factory, run_id):
    await PersistenceActivities(db_session_factory).record_event(_event(run_id, status=None, event_type="payment_confirmed"))
    assert (await _get(db_session_factory, Run, run_id)).order_status is None


async def test_persistence_retry_is_idempotent(db_session_factory, run_id):
    p = PersistenceActivities(db_session_factory)
    record = _event(run_id)
    await p.record_event(record)
    await p.record_event(record)  # a Temporal retry after a lost acknowledgement
    run_uuid = uuid.UUID(run_id)
    assert await _count(db_session_factory, Event, Event.run_id == run_uuid) == 1
    assert await _count(db_session_factory, TimelineEntry, TimelineEntry.run_id == run_uuid) == 1


async def test_record_timeline_entries_batch(db_session_factory, run_id):
    entries = [
        TimelineRecord(entry_id=_id(), run_id=run_id, entry_type="control", message="Supervisor paused", created_at=T0),
        TimelineRecord(entry_id=_id(), run_id=run_id, entry_type="system", message="Tool request rejected", created_at=T0),
    ]
    p = PersistenceActivities(db_session_factory)
    await p.record_timeline_entries(TimelineBatch(entries=entries))
    await p.record_timeline_entries(TimelineBatch(entries=entries))
    assert await _count(db_session_factory, TimelineEntry, TimelineEntry.run_id == uuid.UUID(run_id)) == 2


async def test_action_started_then_finished_success(db_session_factory, run_id):
    p = PersistenceActivities(db_session_factory)
    action_id, execution_id = _id(), _id()
    await p.record_action_started(
        ActionStartRecord(
            action_id=action_id,
            tool_execution_id=execution_id,
            run_id=run_id,
            tool_name="escalate_shipment",
            tool_input={"reason": "late", "priority": "high"},
            reasoning="Delayed 8h; escalate.",
            started_at=T0,
        )
    )
    action = await _get(db_session_factory, Action, action_id)
    execution = await _get(db_session_factory, ToolExecution, execution_id)
    assert (action.status, action.action_type, action.reasoning) == ("pending", "escalate_shipment", "Delayed 8h; escalate.")
    assert execution.status == "pending"  # observable if the worker crashes mid-tool
    assert execution.input == {"reason": "late", "priority": "high"}
    assert execution.result is None and execution.error is None

    done = T0 + timedelta(seconds=2)
    finish = ActionFinishRecord(
        action_id=action_id,
        tool_execution_id=execution_id,
        run_id=run_id,
        timeline_entry_id=_id(),
        success=True,
        result={"escalation_id": "esc-1"},
        error=None,
        completed_at=done,
        timeline_message="Tool escalate_shipment succeeded",
    )
    await p.record_action_finished(finish)
    await p.record_action_finished(finish)  # idempotent
    action = await _get(db_session_factory, Action, action_id)
    execution = await _get(db_session_factory, ToolExecution, execution_id)
    assert (action.status, action.completed_at) == ("completed", done)
    assert (execution.status, execution.result, execution.error, execution.completed_at) == (
        "success",
        {"escalation_id": "esc-1"},
        None,
        done,
    )
    assert await _count(db_session_factory, TimelineEntry, TimelineEntry.entry_type == "action", TimelineEntry.run_id == uuid.UUID(run_id)) == 1


async def test_action_finished_failure_records_error(db_session_factory, run_id):
    p = PersistenceActivities(db_session_factory)
    action_id, execution_id = _id(), _id()
    await p.record_action_started(
        ActionStartRecord(action_id, execution_id, run_id, "send_customer_update", {"message": "hi"}, "notify", T0)
    )
    await p.record_action_finished(
        ActionFinishRecord(action_id, execution_id, run_id, _id(), False, None, "gateway timeout", T0, "Tool failed")
    )
    assert (await _get(db_session_factory, Action, action_id)).status == "failed"
    execution = await _get(db_session_factory, ToolExecution, execution_id)
    assert (execution.status, execution.result, execution.error) == ("failed", None, "gateway timeout")


async def test_memory_snapshot_and_run_instructions(db_session_factory, run_id):
    p = PersistenceActivities(db_session_factory)
    snapshot_id = _id()
    memory = {"situation_summary": "Delayed.", "open_concerns": ["late"], "cycle_count": 2}
    await p.save_memory_snapshot(MemorySnapshotRecord(snapshot_id, run_id, memory, T0))
    await p.save_memory_snapshot(MemorySnapshotRecord(snapshot_id, run_id, memory, T0))
    assert (await _get(db_session_factory, MemorySnapshot, snapshot_id)).memory == memory
    assert await _count(db_session_factory, MemorySnapshot, MemorySnapshot.run_id == uuid.UUID(run_id)) == 1

    instructions = [{"text": "Prioritize speed over cost.", "added_at": T0.isoformat()}]
    await p.save_run_instructions(RunInstructionsRecord(run_id=run_id, instructions=instructions))
    assert (await _get(db_session_factory, Run, run_id)).run_instructions == instructions


async def test_new_columns_default_for_rows_created_without_them(db_session_factory, run_id):
    run = await _get(db_session_factory, Run, run_id)
    assert run.run_instructions == []
    async with db_session_factory() as session:
        supervisor = await session.get(Supervisor, run.supervisor_id)
        assert supervisor.order_status_by_event == {"delivered": "delivered"}


async def test_complete_run_persists_final_output_once_and_marks_completed(db_session_factory, run_id):
    p = PersistenceActivities(db_session_factory)
    output = {"summary": "Done", "key_actions": [], "key_learnings": [], "recommendations": [], "source": "llm"}
    done = T0 + timedelta(hours=3)
    await p.complete_run(CompleteRunRecord(final_output_id=_id(), run_id=run_id, output=output, completed_at=done))
    # Even a retry with a NEW id cannot create a second final output (run_id UNIQUE).
    await p.complete_run(CompleteRunRecord(final_output_id=_id(), run_id=run_id, output=output, completed_at=done))

    assert await _count(db_session_factory, FinalOutput, FinalOutput.run_id == uuid.UUID(run_id)) == 1
    run = await _get(db_session_factory, Run, run_id)
    assert (run.status, run.completed_at) == ("completed", done)


async def test_missing_run_id_is_non_retryable(db_session_factory):
    with pytest.raises(ApplicationError) as exc:
        await PersistenceActivities(db_session_factory).record_event(_event(None))
    assert exc.value.type == "MissingRunId"
    assert exc.value.non_retryable is True


async def test_unknown_run_is_a_non_retryable_integrity_error(db_session_factory):
    with pytest.raises(ApplicationError) as exc:
        await PersistenceActivities(db_session_factory).record_event(_event(_id()))
    assert exc.value.type == "PersistenceIntegrityError"
    assert exc.value.non_retryable is True
