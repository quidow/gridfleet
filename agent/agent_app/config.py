from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def secret_value(value: SecretStr | str | None) -> str | None:
    """Return the plaintext value behind a ``SecretStr`` (or ``None``)."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    environment: Literal["local", "dev", "staging", "prod"] = "local"
    agent_port: int = 5100
    registration_refresh_interval_sec: int = 30
    advertise_ip: str | None = None
    host_id: str | None = None
    # Must exceed the backend's largest pooled-connection idle time
    # (agent.http_pool_idle_seconds registry max = 600): if the server-side
    # keep-alive is shorter, the backend pool hands out connections the agent
    # already closed and non-idempotent calls die with RemoteProtocolError.
    http_keepalive_timeout_sec: int = 630


class ManagerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: SecretStr | None = None
    backend_url: str | None = None

    @model_validator(mode="after")
    def validate_auth_pair(self) -> ManagerSettings:
        has_username = bool(self.manager_auth_username)
        has_password = self.manager_auth_password is not None and bool(self.manager_auth_password.get_secret_value())
        if has_username != has_password:
            raise ValueError("AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together")
        return self

    @property
    def effective_backend_url(self) -> str:
        return self.backend_url or self.manager_url


class ApiAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    api_auth_username: str | None = None
    api_auth_password: SecretStr | None = None

    @model_validator(mode="after")
    def validate_auth_pair(self) -> ApiAuthSettings:
        has_username = bool(self.api_auth_username)
        has_password = self.api_auth_password is not None and bool(self.api_auth_password.get_secret_value())
        if has_username != has_password:
            raise ValueError("AGENT_API_AUTH_USERNAME and AGENT_API_AUTH_PASSWORD must be set together")
        return self


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    runtime_root: str = "/opt/gridfleet-agent/runtimes"
    appium_port_range_start: int = 4723
    appium_port_range_end: int = 4823
    node_poll_interval_sec: float = 5.0


class AgentSettings:
    """Top-level settings facade composed of per-domain BaseSettings groups."""

    def __init__(self) -> None:
        self.core = CoreSettings()
        self.manager = ManagerSettings()
        self.api_auth = ApiAuthSettings()
        self.runtime = RuntimeSettings()


agent_settings = AgentSettings()
