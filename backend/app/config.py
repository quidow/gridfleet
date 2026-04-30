from pathlib import Path

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
    agent_terminal_token: str | None = None
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


settings = Settings()
