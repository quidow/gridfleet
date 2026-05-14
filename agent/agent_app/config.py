import math
from typing import Any, Literal, cast

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    environment: Literal["local", "dev", "staging", "prod"] = "local"
    agent_port: int = 5100
    registration_refresh_interval_sec: int = 30
    advertise_ip: str | None = None


class ManagerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: str | None = None

    @model_validator(mode="after")
    def validate_auth_pair(self) -> "ManagerSettings":
        has_username = bool(self.manager_auth_username)
        has_password = bool(self.manager_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together")
        return self


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

    def __init__(self, **overrides: object) -> None:
        core_overrides = _pick(
            overrides, "environment", "agent_port", "registration_refresh_interval_sec", "advertise_ip"
        )
        manager_overrides = _pick(overrides, "manager_url", "manager_auth_username", "manager_auth_password")
        api_auth_overrides = _pick(overrides, "api_auth_username", "api_auth_password")
        grid_node_overrides = _pick(
            overrides,
            "grid_hub_url",
            "grid_publish_url",
            "grid_subscribe_url",
            "grid_node_heartbeat_sec",
            "grid_node_session_timeout_sec",
            "grid_node_proxy_timeout_sec",
            "grid_node_bind_host",
            "grid_node_port_start",
        )
        runtime_overrides = _pick(
            overrides,
            "runtime_root",
            "appium_port_range_start",
            "appium_port_range_end",
            "adb_reconnect_port",
        )
        terminal_overrides = _pick(overrides, "enable_web_terminal", "terminal_token", "terminal_shell")
        known = (
            set(core_overrides)
            | set(manager_overrides)
            | set(api_auth_overrides)
            | set(grid_node_overrides)
            | set(runtime_overrides)
            | set(terminal_overrides)
        )
        unknown = set(overrides) - known
        if unknown:
            fields = ", ".join(sorted(unknown))
            raise TypeError(f"unknown AgentSettings override(s): {fields}")

        self.core = CoreSettings(**cast("Any", core_overrides))
        self.manager = ManagerSettings(**cast("Any", manager_overrides))
        self.api_auth = ApiAuthSettings(**cast("Any", api_auth_overrides))
        self.grid_node = GridNodeSettings(**cast("Any", grid_node_overrides))
        self.runtime = RuntimeSettings(**cast("Any", runtime_overrides))
        self.terminal = TerminalSettings(**cast("Any", terminal_overrides))

    # Transitional flat-attribute compatibility shims. Keep these read/write
    # so existing test monkeypatches work until all consumers are migrated.
    @property
    def environment(self) -> Literal["local", "dev", "staging", "prod"]:
        return self.core.environment

    @environment.setter
    def environment(self, value: Literal["local", "dev", "staging", "prod"]) -> None:
        self.core.environment = value

    @property
    def agent_port(self) -> int:
        return self.core.agent_port

    @agent_port.setter
    def agent_port(self, value: int) -> None:
        self.core.agent_port = value

    @property
    def registration_refresh_interval_sec(self) -> int:
        return self.core.registration_refresh_interval_sec

    @registration_refresh_interval_sec.setter
    def registration_refresh_interval_sec(self, value: int) -> None:
        self.core.registration_refresh_interval_sec = value

    @property
    def advertise_ip(self) -> str | None:
        return self.core.advertise_ip

    @advertise_ip.setter
    def advertise_ip(self, value: str | None) -> None:
        self.core.advertise_ip = value

    @advertise_ip.deleter
    def advertise_ip(self) -> None:
        self.core.advertise_ip = None

    @property
    def manager_url(self) -> str:
        return self.manager.manager_url

    @manager_url.setter
    def manager_url(self, value: str) -> None:
        self.manager.manager_url = value

    @property
    def manager_auth_username(self) -> str | None:
        return self.manager.manager_auth_username

    @manager_auth_username.setter
    def manager_auth_username(self, value: str | None) -> None:
        self.manager.manager_auth_username = value

    @manager_auth_username.deleter
    def manager_auth_username(self) -> None:
        self.manager.manager_auth_username = None

    @property
    def manager_auth_password(self) -> str | None:
        return self.manager.manager_auth_password

    @manager_auth_password.setter
    def manager_auth_password(self, value: str | None) -> None:
        self.manager.manager_auth_password = value

    @manager_auth_password.deleter
    def manager_auth_password(self) -> None:
        self.manager.manager_auth_password = None

    @property
    def api_auth_username(self) -> str | None:
        return self.api_auth.api_auth_username

    @api_auth_username.setter
    def api_auth_username(self, value: str | None) -> None:
        self.api_auth.api_auth_username = value

    @property
    def api_auth_password(self) -> str | None:
        return self.api_auth.api_auth_password

    @api_auth_password.setter
    def api_auth_password(self, value: str | None) -> None:
        self.api_auth.api_auth_password = value

    @property
    def grid_hub_url(self) -> str:
        return self.grid_node.grid_hub_url

    @grid_hub_url.setter
    def grid_hub_url(self, value: str) -> None:
        self.grid_node.grid_hub_url = value

    @property
    def grid_publish_url(self) -> str:
        return self.grid_node.grid_publish_url

    @grid_publish_url.setter
    def grid_publish_url(self, value: str) -> None:
        self.grid_node.grid_publish_url = value

    @property
    def grid_subscribe_url(self) -> str:
        return self.grid_node.grid_subscribe_url

    @grid_subscribe_url.setter
    def grid_subscribe_url(self, value: str) -> None:
        self.grid_node.grid_subscribe_url = value

    @property
    def grid_node_heartbeat_sec(self) -> float:
        return self.grid_node.grid_node_heartbeat_sec

    @grid_node_heartbeat_sec.setter
    def grid_node_heartbeat_sec(self, value: float) -> None:
        self.grid_node.grid_node_heartbeat_sec = value

    @property
    def grid_node_session_timeout_sec(self) -> float:
        return self.grid_node.grid_node_session_timeout_sec

    @grid_node_session_timeout_sec.setter
    def grid_node_session_timeout_sec(self, value: float) -> None:
        self.grid_node.grid_node_session_timeout_sec = value

    @property
    def grid_node_proxy_timeout_sec(self) -> float:
        return self.grid_node.grid_node_proxy_timeout_sec

    @grid_node_proxy_timeout_sec.setter
    def grid_node_proxy_timeout_sec(self, value: float) -> None:
        self.grid_node.grid_node_proxy_timeout_sec = value

    @property
    def grid_node_bind_host(self) -> str:
        return self.grid_node.grid_node_bind_host

    @grid_node_bind_host.setter
    def grid_node_bind_host(self, value: str) -> None:
        self.grid_node.grid_node_bind_host = value

    @property
    def grid_node_port_start(self) -> int:
        return self.grid_node.grid_node_port_start

    @grid_node_port_start.setter
    def grid_node_port_start(self, value: int) -> None:
        self.grid_node.grid_node_port_start = value

    @property
    def runtime_root(self) -> str:
        return self.runtime.runtime_root

    @runtime_root.setter
    def runtime_root(self, value: str) -> None:
        self.runtime.runtime_root = value

    @property
    def appium_port_range_start(self) -> int:
        return self.runtime.appium_port_range_start

    @appium_port_range_start.setter
    def appium_port_range_start(self, value: int) -> None:
        self.runtime.appium_port_range_start = value

    @property
    def appium_port_range_end(self) -> int:
        return self.runtime.appium_port_range_end

    @appium_port_range_end.setter
    def appium_port_range_end(self, value: int) -> None:
        self.runtime.appium_port_range_end = value

    @property
    def adb_reconnect_port(self) -> int:
        return self.runtime.adb_reconnect_port

    @adb_reconnect_port.setter
    def adb_reconnect_port(self, value: int) -> None:
        self.runtime.adb_reconnect_port = value

    @property
    def enable_web_terminal(self) -> bool:
        return self.terminal.enable_web_terminal

    @enable_web_terminal.setter
    def enable_web_terminal(self, value: bool) -> None:
        self.terminal.enable_web_terminal = value

    @property
    def terminal_token(self) -> str | None:
        return self.terminal.terminal_token

    @terminal_token.setter
    def terminal_token(self, value: str | None) -> None:
        self.terminal.terminal_token = value

    @property
    def terminal_shell(self) -> str | None:
        return self.terminal.terminal_shell

    @terminal_shell.setter
    def terminal_shell(self, value: str | None) -> None:
        self.terminal.terminal_shell = value


def _pick(source: dict[str, object], *keys: str) -> dict[str, object]:
    return {key: source[key] for key in keys if key in source}


agent_settings = AgentSettings()
