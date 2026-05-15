import math
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    environment: Literal["local", "dev", "staging", "prod"] = "local"
    agent_port: int = 5100
    registration_refresh_interval_sec: int = 30
    advertise_ip: str | None = None
    host_id: str | None = None


class ManagerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: str | None = None
    backend_url: str | None = None

    @model_validator(mode="after")
    def validate_auth_pair(self) -> "ManagerSettings":
        has_username = bool(self.manager_auth_username)
        has_password = bool(self.manager_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together")
        return self

    @property
    def effective_backend_url(self) -> str:
        return self.backend_url or self.manager_url


class ApiAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    api_auth_username: str | None = None
    api_auth_password: str | None = None

    @model_validator(mode="after")
    def validate_auth_pair(self) -> "ApiAuthSettings":
        has_username = bool(self.api_auth_username)
        has_password = bool(self.api_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_API_AUTH_USERNAME and AGENT_API_AUTH_PASSWORD must be set together")
        return self


class GridNodeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    grid_hub_url: str = "http://selenium-hub:4444"
    grid_publish_url: str = "tcp://localhost:4442"
    grid_subscribe_url: str = "tcp://localhost:4443"
    grid_node_heartbeat_sec: float = 5.0
    grid_node_session_timeout_sec: float = 300.0
    grid_node_proxy_timeout_sec: float = 60.0
    grid_node_bind_host: str = "0.0.0.0"
    grid_node_port_start: int = 5555

    @model_validator(mode="after")
    def validate_intervals_finite_positive(self) -> "GridNodeSettings":
        # Non-positive (or non-finite) heartbeat would crash the supervisor's
        # `_clock.sleep` loop and silently break drain semantics; non-positive
        # timeouts would force-close every session on the first tick. NaN /
        # +inf must also be rejected at startup instead of after process start.
        for name, value in (
            ("AGENT_GRID_NODE_HEARTBEAT_SEC", self.grid_node_heartbeat_sec),
            ("AGENT_GRID_NODE_SESSION_TIMEOUT_SEC", self.grid_node_session_timeout_sec),
            ("AGENT_GRID_NODE_PROXY_TIMEOUT_SEC", self.grid_node_proxy_timeout_sec),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        return self


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    runtime_root: str = "/opt/gridfleet-agent/runtimes"
    appium_port_range_start: int = 4723
    appium_port_range_end: int = 4823
    adb_reconnect_port: int = 5555


class TerminalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    enable_web_terminal: bool = False
    terminal_token: str | None = None
    terminal_shell: str | None = None

    @model_validator(mode="after")
    def validate_token_and_enabled(self) -> "TerminalSettings":
        if self.terminal_token is not None and not self.terminal_token.strip():
            raise ValueError("AGENT_TERMINAL_TOKEN must not be blank when set")
        if self.enable_web_terminal and not self.terminal_token:
            raise ValueError("AGENT_TERMINAL_TOKEN must be set when AGENT_ENABLE_WEB_TERMINAL=true")
        return self


class AgentSettings:
    """Top-level settings facade composed of per-domain BaseSettings groups."""

    def __init__(self) -> None:
        self.core = CoreSettings()
        self.manager = ManagerSettings()
        self.api_auth = ApiAuthSettings()
        self.grid_node = GridNodeSettings()
        self.runtime = RuntimeSettings()
        self.terminal = TerminalSettings()


agent_settings = AgentSettings()
