from __future__ import annotations

import pytest

from app.events import PUBLIC_EVENT_NAME_SET
from app.events.catalog import (
    PUBLIC_EVENT_CATALOG,
    PUBLIC_EVENT_NAMES,
    allowed_severities_for,
    default_severity_for,
)


def test_device_health_changed_is_registered() -> None:
    assert "device.health_changed" in PUBLIC_EVENT_NAME_SET


def test_device_crashed_is_registered() -> None:
    assert "device.crashed" in PUBLIC_EVENT_NAME_SET


def test_every_event_has_default_and_allowed_severities() -> None:
    for definition in PUBLIC_EVENT_CATALOG:
        assert definition.default_severity in definition.allowed_severities, (
            f"{definition.name}: default_severity {definition.default_severity!r} "
            f"is not in allowed_severities {sorted(definition.allowed_severities)!r}"
        )
        assert definition.allowed_severities, f"{definition.name}: allowed_severities is empty"


def test_helpers_return_catalog_values() -> None:
    for definition in PUBLIC_EVENT_CATALOG:
        assert default_severity_for(definition.name) == definition.default_severity
        assert allowed_severities_for(definition.name) == definition.allowed_severities


def test_helpers_raise_for_unknown_event() -> None:
    with pytest.raises(KeyError):
        default_severity_for("not.a.real.event")
    with pytest.raises(KeyError):
        allowed_severities_for("not.a.real.event")


@pytest.mark.parametrize(
    ("event_name", "expected_default"),
    [
        ("device.operational_state_changed", "info"),
        ("device.hold_changed", "info"),
        ("device.hardware_health_changed", "warning"),
        ("device.crashed", "critical"),
        ("node.crash", "critical"),
        ("host.heartbeat_lost", "critical"),
        ("host.circuit_breaker.opened", "critical"),
        ("host.circuit_breaker.closed", "success"),
        ("host.registered", "success"),
        ("run.completed", "success"),
        ("run.cancelled", "warning"),
        ("run.expired", "critical"),
        ("bulk.operation_completed", "success"),
        ("settings.changed", "neutral"),
        ("config.updated", "neutral"),
        ("system.cleanup_completed", "neutral"),
        ("pack_feature.degraded", "warning"),
        ("pack_feature.recovered", "success"),
    ],
)
def test_default_severity_spec(event_name: str, expected_default: str) -> None:
    assert event_name in PUBLIC_EVENT_NAMES
    assert default_severity_for(event_name) == expected_default
