import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.schemas.inventory import (
    DEFAULT_INVENTORY_COLUMNS,
    InventoryColumn,
    parse_columns_param,
)
from app.devices.services.inventory_export import iter_inventory_csv, iter_inventory_json
from tests.helpers import seed_host_and_device


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


@pytest.mark.asyncio
@pytest.mark.db
async def test_iter_inventory_json_emits_selected_columns(db_session: AsyncSession) -> None:
    host, device = await seed_host_and_device(db_session, identity="INV-1")
    chunks: list[str] = []
    async for chunk in iter_inventory_json(
        db_session,
        columns=[InventoryColumn.NAME, InventoryColumn.HOST_HOSTNAME, InventoryColumn.IDENTITY_VALUE],
        filters=None,
    ):
        chunks.append(chunk)
    body = "".join(chunks)
    payload = json.loads(body)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert set(payload[0].keys()) == {"name", "host", "identity"}
    assert payload[0]["host"] == {"hostname": host.hostname}
    assert payload[0]["identity"] == {"value": device.identity_value}


@pytest.mark.asyncio
@pytest.mark.db
async def test_iter_inventory_csv_emits_header_and_rows(db_session: AsyncSession) -> None:
    host, _ = await seed_host_and_device(db_session, identity="INV-2")
    chunks: list[str] = []
    async for chunk in iter_inventory_csv(
        db_session,
        columns=[InventoryColumn.NAME, InventoryColumn.HOST_HOSTNAME],
        filters=None,
    ):
        chunks.append(chunk)
    body = "".join(chunks)
    lines = body.strip().splitlines()
    assert lines[0] == "name,host.hostname"
    assert lines[1].endswith(host.hostname)


@pytest.mark.asyncio
@pytest.mark.db
async def test_iter_inventory_csv_serializes_jsonb_as_json_string(db_session: AsyncSession) -> None:
    _, device = await seed_host_and_device(db_session, identity="INV-3")
    device.tags = {"team": "qa"}
    await db_session.commit()
    chunks: list[str] = []
    async for chunk in iter_inventory_csv(
        db_session,
        columns=[InventoryColumn.NAME, InventoryColumn.TAGS],
        filters=None,
    ):
        chunks.append(chunk)
    body = "".join(chunks)
    assert "{" in body
    assert "team" in body
