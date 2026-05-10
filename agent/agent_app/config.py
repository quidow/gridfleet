from pydantic import model_validator
from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: str | None = None
    api_auth_username: str | None = None
    api_auth_password: str | None = None
    registration_refresh_interval_sec: int = 30
    agent_port: int = 5100
    grid_hub_url: str = "http://selenium-hub:4444"
    grid_publish_url: str = "tcp://localhost:4442"
    grid_subscribe_url: str = "tcp://localhost:4443"
    grid_node_heartbeat_sec: float = 5.0
    grid_node_session_timeout_sec: float = 300.0
    grid_node_proxy_timeout_sec: float = 60.0
    runtime_root: str = "/opt/gridfleet-agent/runtimes"
    appium_port_range_start: int = 4723
    appium_port_range_end: int = 4823
    grid_node_port_start: int = 5555
    adb_reconnect_port: int = 5555
    advertise_ip: str | None = None
    enable_web_terminal: bool = False
    terminal_token: str | None = None
    terminal_shell: str | None = None

    model_config = {"env_prefix": "AGENT_"}

    @model_validator(mode="after")
    def validate_manager_auth(self) -> "AgentSettings":
        has_username = bool(self.manager_auth_username)
        has_password = bool(self.manager_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together")
        return self

    @model_validator(mode="after")
    def validate_api_auth(self) -> "AgentSettings":
        has_username = bool(self.api_auth_username)
        has_password = bool(self.api_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_API_AUTH_USERNAME and AGENT_API_AUTH_PASSWORD must be set together")
        return self

    @model_validator(mode="after")
    def validate_terminal(self) -> "AgentSettings":
        if self.terminal_token is not None and not self.terminal_token.strip():
            raise ValueError("AGENT_TERMINAL_TOKEN must not be blank when set")
        if self.enable_web_terminal and not self.terminal_token:
            raise ValueError("AGENT_TERMINAL_TOKEN must be set when AGENT_ENABLE_WEB_TERMINAL=true")
        return self

    @model_validator(mode="after")
    def validate_grid_node_intervals(self) -> "AgentSettings":
        # Non-positive heartbeat would crash the supervisor's `_clock.sleep`
        # loop and silently break drain semantics; non-positive timeouts
        # would force-close every session on the first tick.
        if self.grid_node_heartbeat_sec <= 0:
            raise ValueError("AGENT_GRID_NODE_HEARTBEAT_SEC must be > 0")
        if self.grid_node_session_timeout_sec <= 0:
            raise ValueError("AGENT_GRID_NODE_SESSION_TIMEOUT_SEC must be > 0")
        if self.grid_node_proxy_timeout_sec <= 0:
            raise ValueError("AGENT_GRID_NODE_PROXY_TIMEOUT_SEC must be > 0")
        return self


agent_settings = AgentSettings()
