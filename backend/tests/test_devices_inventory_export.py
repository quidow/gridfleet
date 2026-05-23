import json

import pytest
from httpx import AsyncClient
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


@pytest.mark.asyncio
@pytest.mark.db
async def test_iter_inventory_json_serializes_uuid_id(db_session: AsyncSession) -> None:
    _, device = await seed_host_and_device(db_session, identity="INV-4")
    chunks: list[str] = []
    async for chunk in iter_inventory_json(
        db_session,
        columns=[InventoryColumn.ID],
        filters=None,
    ):
        chunks.append(chunk)
    payload = json.loads("".join(chunks))
    assert payload[0]["id"] == str(device.id)


@pytest.mark.asyncio
@pytest.mark.db
async def test_inventory_endpoint_default_json(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_host_and_device(db_session, identity="EP-1")
    response = await client.get("/api/devices/inventory")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert isinstance(payload, list) and len(payload) == 1


@pytest.mark.asyncio
@pytest.mark.db
async def test_inventory_endpoint_csv_with_columns(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_host_and_device(db_session, identity="EP-2")
    response = await client.get(
        "/api/devices/inventory",
        params={"format": "csv", "columns": "name,host.hostname"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert body.startswith("name,host.hostname")


@pytest.mark.asyncio
@pytest.mark.db
async def test_inventory_endpoint_rejects_unknown_column(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_host_and_device(db_session, identity="EP-3")
    response = await client.get("/api/devices/inventory", params={"columns": "name,nope"})
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.db
async def test_inventory_endpoint_filter_pack_id(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_host_and_device(db_session, identity="EP-4")
    response = await client.get("/api/devices/inventory", params={"pack_id": "no-such-pack"})
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
@pytest.mark.db
async def test_iter_inventory_csv_escapes_formula_injection(db_session: AsyncSession) -> None:
    _, device = await seed_host_and_device(db_session, identity="INV-FORMULA")
    device.name = "=CMD()"
    device.tags = {"k": "=evil"}
    await db_session.commit()

    chunks: list[str] = []
    async for chunk in iter_inventory_csv(
        db_session,
        columns=[InventoryColumn.NAME, InventoryColumn.TAGS],
        filters=None,
    ):
        chunks.append(chunk)
    body = "".join(chunks)
    # Name cell should be defanged to '=CMD()
    assert "'=CMD()" in body or '"\'=CMD()"' in body  # csv may quote the cell


def test_parse_columns_param_deduplicates_preserving_order() -> None:
    assert parse_columns_param("name,name,host.hostname,name") == [
        InventoryColumn.NAME,
        InventoryColumn.HOST_HOSTNAME,
    ]
