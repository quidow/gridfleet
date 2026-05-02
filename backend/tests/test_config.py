import pytest

from app.config import Settings


def test_settings_defaults_for_terminal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "GRIDFLEET_AGENT_TERMINAL_TOKEN",
        "GRIDFLEET_ENABLE_WEB_TERMINAL",
        "GRIDFLEET_WEB_TERMINAL_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.agent_terminal_token is None
    assert not hasattr(settings, "enable_web_terminal")
    assert not hasattr(settings, "web_terminal_allowed_origins")


def test_settings_reads_terminal_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_TERMINAL_TOKEN", "s3cret")
    settings = Settings()
    assert settings.agent_terminal_token == "s3cret"


def test_settings_defaults_terminal_agent_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRIDFLEET_AGENT_TERMINAL_SCHEME", raising=False)
    settings = Settings()
    assert settings.agent_terminal_scheme == "ws"


def test_settings_reads_terminal_agent_scheme_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_TERMINAL_SCHEME", "wss")
    settings = Settings()
    assert settings.agent_terminal_scheme == "wss"


def test_settings_rejects_invalid_terminal_agent_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_TERMINAL_SCHEME", "http")
    with pytest.raises(ValueError, match="agent_terminal_scheme"):
        Settings()
