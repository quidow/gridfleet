from __future__ import annotations

import pytest

from app.settings import registry as settings_registry
from app.settings.registry import SETTINGS_REGISTRY


def test_resolve_default_deep_copies_mutable_defaults() -> None:
    definition = settings_registry.SETTINGS_REGISTRY["notifications.toast_events"]
    value = settings_registry.resolve_default(definition)
    assert isinstance(value, list)
    value.append("mutated")
    assert settings_registry.resolve_default(definition) != value


def test_capacity_snapshot_settings_are_registered() -> None:
    snapshot_retention = settings_registry.SETTINGS_REGISTRY["retention.capacity_snapshots_days"]

    assert snapshot_retention.default == 30
    assert snapshot_retention.min_value == 1
    assert snapshot_retention.max_value == 3650


def test_session_viability_defaults_to_hourly_probe() -> None:
    interval = settings_registry.SETTINGS_REGISTRY["general.session_viability_interval_sec"]

    assert interval.default == 3600
    assert interval.min_value == 0
    assert interval.max_value == 604800


def test_session_first_command_grace_setting_is_registered() -> None:
    setting = settings_registry.SETTINGS_REGISTRY["grid.session_first_command_grace_sec"]

    assert setting.category == "grid"
    assert setting.setting_type == "int"
    assert setting.default == 180
    assert setting.min_value == 30
    assert setting.max_value == 3600


def test_device_cooldown_settings_are_registered() -> None:
    max_cooldown = settings_registry.SETTINGS_REGISTRY["general.device_cooldown_max_sec"]

    assert max_cooldown.default == 3600
    assert max_cooldown.min_value == 60
    assert max_cooldown.max_value == 86400


def test_intent_reconciler_settings_are_not_registered() -> None:
    assert "general.intent_reconcile_interval_sec" not in settings_registry.SETTINGS_REGISTRY
    assert "general.intent_reconcile_full_scan_every_cycles" not in settings_registry.SETTINGS_REGISTRY


def test_appium_reconciler_restart_window_setting_is_registered() -> None:
    setting = settings_registry.SETTINGS_REGISTRY["appium_reconciler.restart_window_sec"]
    assert setting.category == "grid"
    assert setting.setting_type == "int"
    assert setting.default == 120
    assert setting.min_value == 30
    assert setting.max_value == 600


@pytest.mark.parametrize(
    "key,expected_default,expected_min,expected_max,expected_type",
    [
        ("device_checks.ip_ping.fail_window_sec", 120, 0, 3600, "int"),
        ("device_checks.ip_ping.timeout_sec", 2.0, 0.5, 30.0, "float"),
        ("device_checks.ip_ping.count_per_cycle", 1, 1, 10, "int"),
    ],
)
def test_ip_ping_settings_registered(
    key: str,
    expected_default: float,
    expected_min: float,
    expected_max: float,
    expected_type: str,
) -> None:
    assert key in SETTINGS_REGISTRY, f"{key} should be registered"
    setting = SETTINGS_REGISTRY[key]
    assert setting.setting_type == expected_type
    assert setting.default == expected_default
    assert setting.min_value == expected_min
    assert setting.max_value == expected_max
    assert setting.category == "device_checks"


def test_device_cooldown_escalation_threshold_default_and_bounds() -> None:
    setting = settings_registry.SETTINGS_REGISTRY["general.device_cooldown_escalation_threshold"]

    assert setting.setting_type == "int"
    assert setting.default == 3
    assert setting.min_value == 0
    assert setting.max_value == 100
    assert setting.category == "general"


def test_removed_plumbing_settings_are_not_registered() -> None:
    removed = {
        "general.heartbeat_interval_sec",
        "general.partition_probe_interval_sec",
        "general.intent_reconcile_interval_sec",
        "grid.session_poll_interval_sec",
        "appium_reconciler.host_parallelism",
        "agent.http_pool_enabled",
        "agent.http_pool_max_keepalive",
        "agent.http_pool_idle_seconds",
        "agent.circuit_breaker_failure_threshold",
        "agent.circuit_breaker_cooldown_seconds",
        "general.node_max_failures",
        "device_checks.ip_ping.consecutive_fail_threshold",
        "device_checks.probe_unanswered.consecutive_fail_threshold",
        "device_checks.probe_failed.consecutive_fail_threshold",
    }
    assert not removed & set(settings_registry.SETTINGS_REGISTRY)


def test_auto_accept_hosts_defaults_to_false() -> None:
    definition = settings_registry.SETTINGS_REGISTRY["agent.auto_accept_hosts"]
    assert definition.default is False, "secure by default (D5): operators approve hosts manually"


def test_unbounded_table_retention_settings_are_registered() -> None:
    for key in (
        "retention.system_events_days",
        "retention.test_runs_days",
        "retention.jobs_days",
    ):
        definition = settings_registry.SETTINGS_REGISTRY[key]
        assert definition.category == "retention"
        assert definition.setting_type == "int"
        assert definition.default == 30
        assert definition.min_value == 1
        assert definition.max_value == 3650


def test_run_failure_escalation_setting_is_registered() -> None:
    definition = SETTINGS_REGISTRY["general.run_failure_escalates_to_maintenance"]
    assert definition.category == "general"
    assert definition.setting_type == "bool"
    assert definition.default is True
