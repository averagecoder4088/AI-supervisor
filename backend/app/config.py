"""Centralized application configuration loaded from environment variables and .env."""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, SecretStr
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

    # LLM provider (OpenAI Responses API, decision B7). Real values live only in the
    # git-ignored backend/.env; with no key or model the LLM client is "not configured".
    llm_api_key: Optional[SecretStr] = None
    llm_model: Optional[str] = None
    # Per-request client timeout; keep it below the 60 s reasoning Activity timeout.
    llm_timeout_seconds: float = Field(default=45.0, gt=0)

    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
