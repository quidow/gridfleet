import pytest

from agent_app.config import AgentSettings


def test_runtime_root_default() -> None:
    settings = AgentSettings()
    assert settings.runtime_root == "/opt/gridfleet-agent/runtimes"


def test_runtime_root_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ROOT", "/tmp/custom-runtimes")
    settings = AgentSettings()
    assert settings.runtime_root == "/tmp/custom-runtimes"
