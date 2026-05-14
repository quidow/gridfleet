import os
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    request_timeout_sec: float = 30.0
    auth_enabled: bool = False
    auth_username: str | None = None
    auth_password: str | None = None
    auth_session_secret: str | None = None
    auth_session_ttl_sec: int = 28_800
    auth_cookie_secure: bool = True
    machine_auth_username: str | None = None
    machine_auth_password: str | None = None
    agent_auth_username: str | None = None
    agent_auth_password: str | None = None
    agent_terminal_token: str | None = None
    agent_terminal_scheme: Literal["ws", "wss"] = "ws"
    driver_pack_storage_dir: Path = Path("/var/lib/gridfleet/driver-packs")

    model_config = {"env_prefix": "GRIDFLEET_"}

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        if self.auth_session_ttl_sec < 1:
            raise ValueError("GRIDFLEET_AUTH_SESSION_TTL_SEC must be at least 1 second")

        if not self.auth_enabled:
            return self

        required_values = {
            "GRIDFLEET_AUTH_USERNAME": self.auth_username,
            "GRIDFLEET_AUTH_PASSWORD": self.auth_password,
            "GRIDFLEET_AUTH_SESSION_SECRET": self.auth_session_secret,
            "GRIDFLEET_MACHINE_AUTH_USERNAME": self.machine_auth_username,
            "GRIDFLEET_MACHINE_AUTH_PASSWORD": self.machine_auth_password,
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Auth is enabled but required settings are missing: {joined}")
        return self

    @model_validator(mode="after")
    def validate_agent_auth_pair(self) -> "Settings":
        has_username = bool(self.agent_auth_username)
        has_password = bool(self.agent_auth_password)
        if has_username != has_password:
            raise ValueError("GRIDFLEET_AGENT_AUTH_USERNAME and GRIDFLEET_AGENT_AUTH_PASSWORD must be set together")
        return self


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
