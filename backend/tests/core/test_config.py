import pytest

from app.agent_comm.config import AgentCommConfig


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
