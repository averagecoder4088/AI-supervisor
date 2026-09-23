"""FastAPI application entrypoint for Order Supervisor."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.client import Client

from app.api.errors import install_error_handlers
from app.api.routes import router as api_router
from app.config import get_settings

logger = logging.getLogger(__name__)


def create_app(
    *,
    temporal_client: Optional[Client] = None,
    session_factory: Optional[async_sessionmaker] = None,
    connect_temporal: bool = True,
) -> FastAPI:
    """Factory creating and configuring the FastAPI application.

    One Temporal client per application, created in the lifespan (never at
    import time) unless one is injected. If Temporal cannot be reached at
    startup, the app still serves database-only endpoints; Temporal-dependent
    endpoints return 503 TEMPORAL_UNAVAILABLE.
    """
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_client = False
        if app.state.temporal_client is None and connect_temporal:
            try:
                app.state.temporal_client = await Client.connect(
                    settings.temporal_address, namespace=settings.temporal_namespace
                )
                owns_client = True
            except Exception as err:  # noqa: BLE001 - startup must not fail on Temporal (decision F)
                logger.warning("Temporal unavailable at startup (%s); Temporal endpoints will return 503", err)
        yield
        if owns_client:
            # temporalio clients have no close(); dropping the reference releases the connection.
            app.state.temporal_client = None

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        description="Order Supervisor POC Backend API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.temporal_client = temporal_client
    if session_factory is None:
        from app.db.session import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    app.state.session_factory = session_factory

    install_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
