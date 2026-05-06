from app.services import host_versioning
from app.services.host_versioning import AgentVersionStatus, get_agent_version_status


def test_agent_version_status_disabled_when_min_version_empty() -> None:
    assert get_agent_version_status("0.0.1", None) == AgentVersionStatus.disabled


def test_agent_version_status_outdated_for_lower_version() -> None:
    assert get_agent_version_status("0.1.9", "0.2.0") == AgentVersionStatus.outdated


def test_agent_version_status_ok_for_equal_or_higher_version() -> None:
    assert get_agent_version_status("0.2", "0.2.0") == AgentVersionStatus.ok


def test_agent_version_status_unknown_for_missing_or_unparseable_version() -> None:
    assert get_agent_version_status(None, "0.2.0") == AgentVersionStatus.unknown
    assert get_agent_version_status("dev-build", "0.2.0") == AgentVersionStatus.unknown


def test_normalize_agent_version_setting_trims_value() -> None:
    assert host_versioning.normalize_agent_version_setting(" 0.3.0 ") == "0.3.0"


def test_normalize_agent_version_setting_empty_value_returns_none() -> None:
    assert host_versioning.normalize_agent_version_setting(" ") is None


def test_agent_recommended_version_setting_is_registered() -> None:
    from app.services.settings_registry import SETTINGS_REGISTRY

    definition = SETTINGS_REGISTRY["agent.recommended_version"]

    assert definition.default == ""
    assert definition.category == "agent"
    assert "Recommended agent version" in definition.description


def test_is_agent_update_available_true_when_below_recommended() -> None:
    assert host_versioning.is_agent_update_available("0.2.0", "0.3.0") is True


def test_is_agent_update_available_false_when_current() -> None:
    assert host_versioning.is_agent_update_available("0.3.0", "0.3.0") is False


def test_is_agent_update_available_false_when_no_recommendation() -> None:
    assert host_versioning.is_agent_update_available("0.2.0", None) is False


def test_is_agent_update_available_false_when_agent_version_unknown() -> None:
    assert host_versioning.is_agent_update_available(None, "0.3.0") is False


def test_is_agent_update_available_false_when_above_recommended() -> None:
    assert host_versioning.is_agent_update_available("0.4.0", "0.3.0") is False
