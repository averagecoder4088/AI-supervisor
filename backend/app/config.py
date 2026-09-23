"""Centralized application configuration loaded from environment variables and .env."""

from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    app_name: str = "Order Supervisor API"
    app_env: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000

    # PostgreSQL / Supabase connection string (async, via the asyncpg driver).
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/order_supervisor"

    # Temporal server connection (used by the worker; see app.temporal).
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"

    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
