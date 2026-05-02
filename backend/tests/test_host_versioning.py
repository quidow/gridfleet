from collections.abc import Generator

import pytest

from app.models.host import Host, OSType
from app.services.host_versioning import AgentVersionStatus, get_agent_version_status
from app.services.settings_service import settings_service


@pytest.fixture(autouse=True)
def restore_agent_min_version() -> Generator[None, None, None]:
    original = settings_service._cache.get("agent.min_version")
    yield
    settings_service._cache["agent.min_version"] = original


def test_agent_version_status_disabled_when_min_version_empty() -> None:
    settings_service._cache["agent.min_version"] = ""
    host = Host(hostname="lab-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, agent_version="0.0.1")

    assert get_agent_version_status(host) == AgentVersionStatus.disabled


def test_agent_version_status_outdated_for_lower_version() -> None:
    settings_service._cache["agent.min_version"] = "0.2.0"
    host = Host(hostname="lab-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, agent_version="0.1.9")

    assert get_agent_version_status(host) == AgentVersionStatus.outdated


def test_agent_version_status_ok_for_equal_or_higher_version() -> None:
    settings_service._cache["agent.min_version"] = "0.2.0"
    host = Host(hostname="lab-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, agent_version="0.2")

    assert get_agent_version_status(host) == AgentVersionStatus.ok


def test_agent_version_status_unknown_for_missing_or_unparseable_version() -> None:
    settings_service._cache["agent.min_version"] = "0.2.0"
    missing = Host(hostname="missing-host", ip="10.0.0.2", os_type=OSType.linux, agent_port=5100, agent_version=None)
    weird = Host(hostname="weird-host", ip="10.0.0.3", os_type=OSType.linux, agent_port=5100, agent_version="dev-build")

    assert get_agent_version_status(missing) == AgentVersionStatus.unknown
    assert get_agent_version_status(weird) == AgentVersionStatus.unknown


def test_recommended_agent_version_reads_trimmed_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(
        host_versioning.settings_service, "get", lambda key: " 0.3.0 " if key == "agent.recommended_version" else ""
    )

    assert host_versioning.get_recommended_agent_version() == "0.3.0"


def test_recommended_agent_version_empty_setting_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(
        host_versioning.settings_service, "get", lambda key: " " if key == "agent.recommended_version" else ""
    )

    assert host_versioning.get_recommended_agent_version() is None


def test_agent_recommended_version_setting_is_registered() -> None:
    from app.services.settings_registry import SETTINGS_REGISTRY

    definition = SETTINGS_REGISTRY["agent.recommended_version"]

    assert definition.default == ""
    assert definition.category == "agent"
    assert "Recommended agent version" in definition.description


def test_is_agent_update_available_true_when_below_recommended(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(
        host_versioning.settings_service, "get", lambda key: "0.3.0" if key == "agent.recommended_version" else ""
    )

    assert host_versioning.is_agent_update_available("0.2.0") is True


def test_is_agent_update_available_false_when_current(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(
        host_versioning.settings_service, "get", lambda key: "0.3.0" if key == "agent.recommended_version" else ""
    )

    assert host_versioning.is_agent_update_available("0.3.0") is False


def test_is_agent_update_available_false_when_no_recommendation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(host_versioning.settings_service, "get", lambda key: "")

    assert host_versioning.is_agent_update_available("0.2.0") is False


def test_is_agent_update_available_false_when_agent_version_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(
        host_versioning.settings_service, "get", lambda key: "0.3.0" if key == "agent.recommended_version" else ""
    )

    assert host_versioning.is_agent_update_available(None) is False


def test_is_agent_update_available_false_when_above_recommended(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import host_versioning

    monkeypatch.setattr(
        host_versioning.settings_service, "get", lambda key: "0.3.0" if key == "agent.recommended_version" else ""
    )

    assert host_versioning.is_agent_update_available("0.4.0") is False
