import math
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


class ManagerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: SecretStr | None = None
    backend_url: str | None = None

    @model_validator(mode="after")
    def validate_auth_pair(self) -> "ManagerSettings":
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
    def validate_auth_pair(self) -> "ApiAuthSettings":
        has_username = bool(self.api_auth_username)
        has_password = self.api_auth_password is not None and bool(self.api_auth_password.get_secret_value())
        if has_username != has_password:
            raise ValueError("AGENT_API_AUTH_USERNAME and AGENT_API_AUTH_PASSWORD must be set together")
        return self


class GridNodeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    # Host-run agent: default to the loopback hub like the sibling event-bus URLs
    # below (and like cli.py / installer plan.py, both of which default to
    # localhost:4444). Docker/networked deployments override via AGENT_GRID_HUB_URL.
    # NOTE: this is the first setting actually used for an HTTP call to the hub
    # (the relay registers over ZMQ); a docker-internal default here is unreachable
    # from the host the agent runs on.
    grid_hub_url: str = "http://localhost:4444"
    grid_publish_url: str = "tcp://localhost:4442"
    grid_subscribe_url: str = "tcp://localhost:4443"
    grid_node_heartbeat_sec: float = 5.0
    # Selenium Grid session inactivity timeout. The agent's `expire_idle` will
    # close any active session that has not seen a WebDriver call within this
    # window. The previous 300s default was tight for workflows that quietly
    # block on external-login or post-test artifact collection between
    # WebDriver calls — those legitimate idle gaps would trigger spurious
    # `session timed out due to inactivity` removals. 1800s (30min) is the
    # new floor; override via `AGENT_GRID_NODE_SESSION_TIMEOUT_SEC`.
    grid_node_session_timeout_sec: float = 1800.0
    grid_node_proxy_timeout_sec: float = 60.0
    grid_node_bind_host: str = "0.0.0.0"
    grid_node_port_start: int = 5555
    # Relay fast lane: spawn the gridfleet-relay-proxy sidecar per node so
    # WebDriver commands bypass this process. "auto" enables it when the
    # binary is installed (the gridfleet-agent-relay package), "on" fails
    # node start without it, "off" forces in-process proxying.
    relay_fast_lane: Literal["auto", "on", "off"] = "auto"
    # Loopback port range for the Python relay's control listener (the
    # node's advertised port goes to the sidecar in fast-lane mode).
    relay_control_port_start: int = 7900
    # Explicit path override for the sidecar binary; empty = discover
    # `gridfleet-relay-proxy` on PATH.
    relay_binary: str = ""

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


class AgentSettings:
    """Top-level settings facade composed of per-domain BaseSettings groups."""

    def __init__(self) -> None:
        self.core = CoreSettings()
        self.manager = ManagerSettings()
        self.api_auth = ApiAuthSettings()
        self.grid_node = GridNodeSettings()
        self.runtime = RuntimeSettings()


agent_settings = AgentSettings()
