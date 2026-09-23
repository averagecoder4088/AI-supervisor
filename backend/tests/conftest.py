"""Shared fixtures.

``db_session_factory`` gives tests a session factory bound to ONE connection
inside an outer transaction that is always rolled back. Code under test may
``commit()`` freely: with ``join_transaction_mode="create_savepoint"`` those
commits only release SAVEPOINTs, so no test row survives the test. This runs
against the real local PostgreSQL (``DATABASE_URL``), not a mock.
"""

from typing import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        outer = await connection.begin()
        try:
            yield async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
        finally:
            await outer.rollback()
    await engine.dispose()
