"""B8: real PostgreSQL timestamp round-trip through the existing ORM models.

The live columns are ``timestamptz``; the models declare
``DateTime(timezone=True)`` (fixed in Step 4 after this test exposed plain
``Mapped[datetime]``). Temporal's ``workflow.now()`` produces timezone-aware
UTC datetimes, so that is what the persistence Activities write.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import Event, Run, Supervisor

pytestmark = pytest.mark.asyncio


async def _make_run(session) -> Run:
    supervisor = Supervisor(
        name=f"tz-test-{uuid.uuid4().hex[:8]}",
        instructions="test",
        wake_policy={},
        enabled_tools=[],
        default_wake_interval=60,
        min_wake_interval=5,
        max_wake_interval=1440,
        terminal_order_statuses=["delivered"],
        version=1,
    )
    session.add(supervisor)
    await session.flush()
    run = Run(order_id=f"tz-{uuid.uuid4().hex[:8]}", supervisor_id=supervisor.id)
    session.add(run)
    await session.flush()
    return run


async def test_aware_utc_datetime_round_trips_through_timestamptz(db_session_factory):
    written = datetime(2026, 9, 24, 10, 30, 15, 123456, tzinfo=timezone.utc)
    async with db_session_factory() as session:
        run = await _make_run(session)
        event = Event(run_id=run.id, event_type="order_created", payload={}, occurred_at=written)
        session.add(event)
        await session.commit()
        event_id = event.id

    async with db_session_factory() as session:
        read = (await session.execute(select(Event.occurred_at).where(Event.id == event_id))).scalar_one()

    assert read.tzinfo is not None
    assert read == written  # same instant


async def test_aware_non_utc_datetime_keeps_the_same_instant(db_session_factory):
    written = datetime(2026, 9, 24, 16, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    async with db_session_factory() as session:
        run = await _make_run(session)
        event = Event(run_id=run.id, event_type="order_created", payload={}, occurred_at=written)
        session.add(event)
        await session.commit()
        event_id = event.id

    async with db_session_factory() as session:
        read = (await session.execute(select(Event.occurred_at).where(Event.id == event_id))).scalar_one()

    assert read == written
    assert read.astimezone(timezone.utc) == datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)


async def test_server_default_timestamps_are_timezone_aware(db_session_factory):
    async with db_session_factory() as session:
        run = await _make_run(session)
        await session.commit()
        run_id = run.id

    async with db_session_factory() as session:
        created = (await session.execute(select(Run.created_at).where(Run.id == run_id))).scalar_one()

    assert created.tzinfo is not None
