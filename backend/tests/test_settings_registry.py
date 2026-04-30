from __future__ import annotations

import pytest

from app.services import settings_registry


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("YES", True), ("0", False), ("off", False)],
)
def test_parse_bool_accepts_supported_values(raw: str, expected: bool) -> None:
    assert settings_registry._parse_bool(raw, "GRIDFLEET_TEST") is expected


def test_parse_bool_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="Invalid boolean value"):
        settings_registry._parse_bool("maybe", "GRIDFLEET_TEST")


def test_parse_env_value_supports_int_bool_json_and_string() -> None:
    definition = settings_registry.SettingDefinition(
        key="demo",
        category="general",
        setting_type="int",
        default=1,
        description="demo",
    )
    assert settings_registry._parse_env_value(definition, "5") == 5

    definition = settings_registry.SettingDefinition(
        key="demo",
        category="general",
        setting_type="bool",
        default=False,
        description="demo",
        env_var="GRIDFLEET_BOOL",
    )
    assert settings_registry._parse_env_value(definition, "true") is True

    definition = settings_registry.SettingDefinition(
        key="demo",
        category="general",
        setting_type="json",
        default=[],
        description="demo",
    )
    assert settings_registry._parse_env_value(definition, '["a"]') == ["a"]

    definition = settings_registry.SettingDefinition(
        key="demo",
        category="general",
        setting_type="string",
        default="x",
        description="demo",
    )
    assert settings_registry._parse_env_value(definition, "value") == "value"


def test_resolve_default_prefers_env_override_and_deep_copies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = settings_registry.SettingDefinition(
        key="demo",
        category="general",
        setting_type="json",
        default={"a": ["b"]},
        description="demo",
        env_var="GRIDFLEET_DEMO",
    )

    monkeypatch.setenv("GRIDFLEET_DEMO", '{"x": 1}')
    assert settings_registry.resolve_default(definition) == {"x": 1}
    monkeypatch.delenv("GRIDFLEET_DEMO")

    value = settings_registry.resolve_default(definition)
    value["a"].append("c")
    assert settings_registry.resolve_default(definition) == {"a": ["b"]}


def test_capacity_snapshot_settings_are_registered() -> None:
    snapshot_interval = settings_registry.SETTINGS_REGISTRY["general.fleet_capacity_snapshot_interval_sec"]
    snapshot_retention = settings_registry.SETTINGS_REGISTRY["retention.capacity_snapshots_days"]

    assert snapshot_interval.default == 60
    assert snapshot_interval.min_value == 10
    assert snapshot_interval.max_value == 3600
    assert snapshot_retention.default == 30
    assert snapshot_retention.min_value == 1
    assert snapshot_retention.max_value == 3650


def test_session_viability_defaults_to_hourly_probe() -> None:
    interval = settings_registry.SETTINGS_REGISTRY["general.session_viability_interval_sec"]

    assert interval.default == 3600
    assert interval.min_value == 0
    assert interval.max_value == 604800


def test_terminal_settings_are_registered_under_agent_category() -> None:
    toggle = settings_registry.SETTINGS_REGISTRY["agent.enable_web_terminal"]
    origins = settings_registry.SETTINGS_REGISTRY["agent.web_terminal_allowed_origins"]

    assert toggle.category == "agent"
    assert toggle.setting_type == "bool"
    assert toggle.default is False
    assert toggle.env_var == "GRIDFLEET_ENABLE_WEB_TERMINAL"

    assert origins.category == "agent"
    assert origins.setting_type == "string"
    assert origins.default == ""
    assert origins.env_var == "GRIDFLEET_WEB_TERMINAL_ALLOWED_ORIGINS"


def test_terminal_toggle_env_fallback_resolves_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_ENABLE_WEB_TERMINAL", "true")
    defn = settings_registry.SETTINGS_REGISTRY["agent.enable_web_terminal"]
    assert settings_registry.resolve_default(defn) is True
