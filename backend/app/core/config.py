"""Global process config for the GridFleet backend."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    request_timeout_sec: float = 30.0
    environment: str = "local"

    model_config = SettingsConfigDict(env_prefix="GRIDFLEET_", extra="ignore")


DOCS_ENABLED_ENVIRONMENTS = frozenset({"local", "test", "staging"})

settings = Settings()
