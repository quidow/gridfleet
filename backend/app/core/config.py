"""Global process config for the GridFleet backend."""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    request_timeout_sec: float = 30.0

    model_config = SettingsConfigDict(env_prefix="GRIDFLEET_", extra="ignore")


settings = Settings()


def freeze_background_loops_enabled() -> bool:
    """Return whether leader-owned background loops should be skipped."""
    value = os.getenv("GRIDFLEET_FREEZE_BACKGROUND_LOOPS", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def reconciler_convergence_enabled() -> bool:
    """Return whether the Appium reconciler should drive desired-state convergence."""
    value = os.getenv("GRIDFLEET_RECONCILER_CONVERGENCE_ENABLED", "").strip().lower()
    if value == "":
        return True
    return value in {"1", "true", "yes", "on"}
