"""FastAPI dependencies: request-scoped DB sessions and the app's Temporal client.

Both come from ``app.state`` (set by ``create_app``), so tests inject a session
factory and a Temporal client without an extra DI framework.
"""

from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.api.errors import TEMPORAL_UNAVAILABLE, ApiError


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_temporal_client(request: Request) -> Client:
    client = request.app.state.temporal_client
    if client is None:
        raise ApiError(503, TEMPORAL_UNAVAILABLE, "Temporal is not connected")
    return client
