"""FastAPI application entrypoint for Order Supervisor."""

import uvicorn
from fastapi import FastAPI
from app.api.routes import router as api_router
from app.config import get_settings


def create_app() -> FastAPI:
    """Factory creating and configuring the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        description="Order Supervisor POC Backend API",
        version="0.1.0",
    )

    # Include routes
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

