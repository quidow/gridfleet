from __future__ import annotations

from app.settings.registry import _DEFINITIONS


def test_agent_log_retention_days_defined() -> None:
    by_key = {definition.key: definition for definition in _DEFINITIONS}
    setting = by_key["retention.agent_log_days"]
    assert setting.category == "retention"
    assert setting.setting_type == "int"
    assert setting.default == 7
    assert setting.min_value == 1
    assert setting.max_value == 30
    assert setting.env_var == "GRIDFLEET_AGENT_LOG_RETENTION_DAYS"


def test_agent_log_ship_min_level_defined() -> None:
    by_key = {definition.key: definition for definition in _DEFINITIONS}
    setting = by_key["agent.log_ship_min_level"]
    assert setting.category == "agent"
    assert setting.setting_type == "string"
    assert setting.default == "INFO"
    assert setting.allowed_values == ["DEBUG", "INFO", "WARNING", "ERROR"]
    assert setting.env_var == "GRIDFLEET_AGENT_LOG_SHIP_MIN_LEVEL"
