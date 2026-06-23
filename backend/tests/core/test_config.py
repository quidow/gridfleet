import pytest

from app.agent_comm.config import AgentCommConfig
from app.core import config


def test_agent_auth_pair_required_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_USERNAME", "ops")
    monkeypatch.delenv("GRIDFLEET_AGENT_AUTH_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="GRIDFLEET_AGENT_AUTH"):
        AgentCommConfig()


def test_agent_auth_pair_set_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_USERNAME", "ops")
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_PASSWORD", "secret")
    s = AgentCommConfig()
    assert s.agent_auth_username == "ops"
    assert s.agent_auth_password == "secret"


def test_agent_auth_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRIDFLEET_AGENT_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("GRIDFLEET_AGENT_AUTH_PASSWORD", raising=False)
    s = AgentCommConfig()
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
