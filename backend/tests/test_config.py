import pytest

from app.core import config as config
from app.core.config import Settings


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


def test_agent_auth_pair_required_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_USERNAME", "ops")
    monkeypatch.delenv("GRIDFLEET_AGENT_AUTH_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="GRIDFLEET_AGENT_AUTH"):
        Settings()


def test_agent_auth_pair_set_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_USERNAME", "ops")
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_PASSWORD", "secret")
    s = Settings()
    assert s.agent_auth_username == "ops"
    assert s.agent_auth_password == "secret"


def test_agent_auth_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRIDFLEET_AGENT_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("GRIDFLEET_AGENT_AUTH_PASSWORD", raising=False)
    s = Settings()
    assert s.agent_auth_username is None
    assert s.agent_auth_password is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ("", True),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("0", False),
        ("false", False),
        ("off", False),
    ],
)
def test_reconciler_convergence_enabled_default_true(
    raw: str | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if raw is None:
        monkeypatch.delenv("GRIDFLEET_RECONCILER_CONVERGENCE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GRIDFLEET_RECONCILER_CONVERGENCE_ENABLED", raw)
    assert config.reconciler_convergence_enabled() is expected
