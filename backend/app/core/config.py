"""Application settings.

Single source of truth for configuration. Values come from environment
variables (or a local `.env`), are validated by Pydantic at import time, and
are cached so the file is parsed once per process.

Failing loudly at startup on a missing/malformed setting is deliberate: a
misconfigured deployment should refuse to boot rather than surface as a
confusing 500 on the first request that touches the database.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    project_name: str = "Clinetics"
    environment: Literal["local", "ci", "staging", "production"] = "local"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    database_url: str = "postgresql+asyncpg://clinetics:clinetics_dev_pw@localhost:5432/clinetics"
    database_url_sync: str = (
        "postgresql+psycopg://clinetics:clinetics_dev_pw@localhost:5432/clinetics"
    )
    db_echo: bool = False

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Security (used from Phase 2) ---
    secret_key: str = "dev-only-insecure-key"
    access_token_expire_minutes: int = 60
    algorithm: str = "HS256"

    # --- CORS ---
    # `NoDecode` is required: without it pydantic-settings tries to JSON-decode
    # any complex-typed value coming from `.env` *before* validators run, so
    # `CORS_ORIGINS=http://localhost:3000` raises a parse error. NoDecode hands
    # the raw string to the validator below instead.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # --- LLM (used from Phase 5) ---
    anthropic_api_key: str | None = None
    agent_model_default: str = "claude-haiku-4-5-20251001"
    agent_model_complex: str = "claude-sonnet-5"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept either a comma-separated string (from `.env`) or a real list."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor. Use this everywhere instead of instantiating Settings()."""
    return Settings()


settings = get_settings()
