import pytest

from agent_app.config import AgentSettings


def test_agent_settings_default_disables_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AGENT_ENABLE_WEB_TERMINAL", "AGENT_TERMINAL_TOKEN", "AGENT_TERMINAL_SHELL"):
        monkeypatch.delenv(key, raising=False)
    settings = AgentSettings()
    assert settings.enable_web_terminal is False
    assert settings.terminal_token is None
    assert settings.terminal_shell is None


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
    assert settings.api_auth_username == "ops"
    assert settings.api_auth_password == "secret"


def test_grid_node_settings_defaults() -> None:
    settings = AgentSettings()
    assert settings.grid_node_heartbeat_sec == 5.0
    assert settings.grid_node_session_timeout_sec == 300.0
    assert settings.grid_node_proxy_timeout_sec == 60.0
