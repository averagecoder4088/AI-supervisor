"""Shared helpers for the Step 6 API tests (decision J).

API tests use a normal POOLED engine (not the Step 4/5 single rolled-back
connection): an HTTP request and a worker Activity may hit PostgreSQL at the
same time, which one asyncpg connection cannot serve. Every row the tests
create uses the deterministic ``API-TEST-`` prefix and is deleted before and
after each test, so the database ends clean.
"""

import copy
from typing import Any, AsyncIterator, Dict

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Run, Supervisor

PREFIX = "API-TEST-"

SUPERVISOR_BODY: Dict[str, Any] = {
    "name": f"{PREFIX}Shipment Supervisor",
    "description": "Supervises order shipment issues.",
    "instructions": "Monitor shipment health and respond to delays.",
    "wake_policy": {"important_event_types": ["shipment_delayed", "customer_message_received"]},
    "enabled_tools": ["get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"],
    "default_wake_interval_minutes": 30,
    "min_wake_interval_minutes": 5,
    "max_wake_interval_minutes": 120,
    "terminal_order_statuses": ["delivered", "cancelled"],
    "order_status_by_event": {"delivered": "delivered", "order_cancelled": "cancelled"},
}


def supervisor_body(**changes: Any) -> Dict[str, Any]:
    body = copy.deepcopy(SUPERVISOR_BODY)
    body.update(changes)
    return body


async def cleanup(factory: async_sessionmaker) -> None:
    async with factory() as session:
        # Runs first (their history cascades); supervisors are RESTRICTed by runs.
        await session.execute(delete(Run).where(Run.order_id.like(f"{PREFIX}%")))
        await session.execute(delete(Supervisor).where(Supervisor.name.like(f"{PREFIX}%")))
        await session.commit()


@pytest_asyncio.fixture
async def pooled_session_factory() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    await cleanup(factory)
    try:
        yield factory
    finally:
        await cleanup(factory)
        await engine.dispose()
