import pytest

from app.devices.schemas.inventory import (
    DEFAULT_INVENTORY_COLUMNS,
    InventoryColumn,
    parse_columns_param,
)


def test_inventory_column_enum_has_expected_dot_paths() -> None:
    expected = {
        "id",
        "name",
        "auto_manage",
        "review_required",
        "created_at",
        "updated_at",
        "host.id",
        "host.hostname",
        "pack_id",
        "platform_id",
        "identity.scheme",
        "identity.scope",
        "identity.value",
        "device_type",
        "connection_type",
        "connection_target",
        "os_version",
        "manufacturer",
        "model",
        "model_number",
        "software_versions",
        "operational_state",
        "hold",
        "tags",
        "device_config",
        "test_data",
        "hardware.battery_level_percent",
        "hardware.battery_temperature_c",
        "hardware.charging_state",
        "hardware.health_status",
        "hardware.telemetry_reported_at",
        "verification.verified_at",
        "verification.session_viability_status",
        "verification.device_checks_healthy",
        "verification.device_checks_checked_at",
    }
    actual = {c.value for c in InventoryColumn}
    assert expected <= actual


def test_parse_columns_param_empty_returns_all() -> None:
    assert parse_columns_param(None) == list(InventoryColumn)
    assert parse_columns_param("") == list(InventoryColumn)


def test_parse_columns_param_validates_each_token() -> None:
    assert parse_columns_param("name,host.hostname") == [
        InventoryColumn.NAME,
        InventoryColumn.HOST_HOSTNAME,
    ]


def test_parse_columns_param_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        parse_columns_param("name,nope")


def test_default_inventory_columns_is_subset_of_enum() -> None:
    assert set(DEFAULT_INVENTORY_COLUMNS) <= set(InventoryColumn)
