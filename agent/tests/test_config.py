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
