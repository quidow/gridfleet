import pytest

from agent_app.config import AgentSettings


def test_agent_settings_default_disables_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AGENT_ENABLE_WEB_TERMINAL", "AGENT_TERMINAL_TOKEN", "AGENT_TERMINAL_SHELL"):
        monkeypatch.delenv(key, raising=False)
    settings = AgentSettings()
    assert settings.terminal.enable_web_terminal is False
    assert settings.terminal.terminal_token is None
    assert settings.terminal.terminal_shell is None


def test_agent_settings_rejects_terminal_enabled_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ENABLE_WEB_TERMINAL", "true")
    monkeypatch.delenv("AGENT_TERMINAL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="AGENT_TERMINAL_TOKEN must be set when AGENT_ENABLE_WEB_TERMINAL=true"):
        AgentSettings()


def test_agent_settings_rejects_whitespace_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TERMINAL_TOKEN", "   ")
    with pytest.raises(ValueError, match="AGENT_TERMINAL_TOKEN"):
        AgentSettings()


def test_agent_settings_rejects_api_auth_username_without_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_API_AUTH_USERNAME", "ops")
    monkeypatch.delenv("AGENT_API_AUTH_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="AGENT_API_AUTH"):
        AgentSettings()


def test_agent_settings_rejects_api_auth_password_without_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_API_AUTH_USERNAME", raising=False)
    monkeypatch.setenv("AGENT_API_AUTH_PASSWORD", "secret")
    with pytest.raises(ValueError, match="AGENT_API_AUTH"):
        AgentSettings()


def test_agent_settings_accepts_api_auth_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_API_AUTH_USERNAME", "ops")
    monkeypatch.setenv("AGENT_API_AUTH_PASSWORD", "secret")
    settings = AgentSettings()
    assert settings.api_auth.api_auth_username == "ops"
    assert settings.api_auth.api_auth_password == "secret"


def test_grid_node_settings_defaults() -> None:
    settings = AgentSettings()
    assert settings.grid_node.grid_node_heartbeat_sec == 5.0
    assert settings.grid_node.grid_node_session_timeout_sec == 300.0
    assert settings.grid_node.grid_node_proxy_timeout_sec == 60.0


def test_grid_node_settings_reject_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_GRID_NODE_HEARTBEAT_SEC", "0")
    with pytest.raises(ValueError, match="AGENT_GRID_NODE_HEARTBEAT_SEC"):
        AgentSettings()


def test_grid_node_settings_reject_nan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_GRID_NODE_SESSION_TIMEOUT_SEC", "nan")
    with pytest.raises(ValueError, match="AGENT_GRID_NODE_SESSION_TIMEOUT_SEC"):
        AgentSettings()


def test_grid_node_settings_reject_inf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_GRID_NODE_PROXY_TIMEOUT_SEC", "inf")
    with pytest.raises(ValueError, match="AGENT_GRID_NODE_PROXY_TIMEOUT_SEC"):
        AgentSettings()


def test_core_settings_reads_agent_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.config import CoreSettings

    monkeypatch.setenv("AGENT_HOST_ID", "host-123")
    settings = CoreSettings()
    assert settings.host_id == "host-123"


def test_core_settings_host_id_default_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.config import CoreSettings

    monkeypatch.delenv("AGENT_HOST_ID", raising=False)
    settings = CoreSettings()
    assert settings.host_id is None


def test_manager_settings_reads_backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.config import ManagerSettings

    monkeypatch.setenv("AGENT_BACKEND_URL", "http://backend:8000")
    settings = ManagerSettings()
    assert settings.backend_url == "http://backend:8000"
    assert settings.effective_backend_url == "http://backend:8000"


def test_manager_settings_effective_falls_back_to_manager_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.config import ManagerSettings

    monkeypatch.delenv("AGENT_BACKEND_URL", raising=False)
    monkeypatch.setenv("AGENT_MANAGER_URL", "http://manager:8000")
    settings = ManagerSettings()
    assert settings.backend_url is None
    assert settings.effective_backend_url == "http://manager:8000"
