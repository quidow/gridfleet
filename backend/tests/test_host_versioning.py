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
