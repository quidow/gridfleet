from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from app.devices.models import DeviceGroup, DeviceGroupMembership, GroupType
from app.portability.schemas import (
    ExportBundle,
    ExportedDevice,
    ImportPreview,
    ImportRowStatus,
    OriginalHost,
)

if TYPE_CHECKING:
    import uuid

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def test_exported_device_strict_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        ExportedDevice.model_validate(
            {
                "pack_id": "appium-uiautomator2",
                "platform_id": "android",
                "identity_scheme": "serial",
                "identity_scope": "host",
                "identity_value": "R58",
                "name": "Pixel",
                "device_type": "real_device",
                "connection_type": "usb",
                "tags": {},
                "device_config": {},
                "test_data": {},
                "original_host": {"hostname": "lab-04"},
                "unexpected": True,
            }
        )


def test_export_bundle_schema_version_required() -> None:
    with pytest.raises(ValidationError):
        ExportBundle.model_validate({"exported_at": "2026-05-23T00:00:00Z", "devices": []})


def test_original_host_host_id_optional() -> None:
    host = OriginalHost.model_validate({"hostname": "lab-04"})
    assert host.host_id is None


def test_import_preview_schema_version_required() -> None:
    with pytest.raises(ValidationError):
        ImportPreview.model_validate(
            {
                "exported_at": "2026-05-23T00:00:00Z",
                "bundle_hash": "sha256:x",
                "available_hosts": [],
                "rows": [],
            }
        )


def test_exported_device_identity_scope_rejects_unknown_value() -> None:
    payload = {
        "pack_id": "appium-uiautomator2",
        "platform_id": "android",
        "identity_scheme": "serial",
        "identity_scope": "fleet",
        "identity_value": "R58",
        "name": "Pixel",
        "device_type": "real_device",
        "connection_type": "usb",
        "tags": {},
        "device_config": {},
        "test_data": {},
        "original_host": {"hostname": "lab-04"},
    }
    with pytest.raises(ValidationError):
        ExportedDevice.model_validate(payload)


def test_import_row_status_enum_values() -> None:
    assert ImportRowStatus.VALID_NEW == "valid_new"
    assert ImportRowStatus.CONFLICT_SKIP == "conflict_skip"
    assert ImportRowStatus.DUPLICATE_IN_BUNDLE == "duplicate_in_bundle"
    assert ImportRowStatus.INVALID == "invalid"
    assert {m.value for m in ImportRowStatus} == {
        "valid_new",
        "conflict_skip",
        "duplicate_in_bundle",
        "invalid",
    }


@pytest.mark.asyncio
@pytest.mark.db
async def test_build_export_bundle_includes_all_devices(db_session: AsyncSession) -> None:
    from app.portability.services.export import PortabilityExportService
    from tests.helpers import seed_host_and_device

    host, device = await seed_host_and_device(db_session, identity="EXPORT-1")
    device.tags = {"team": "qa"}
    device.test_data = {"creds": {"u": "a"}}
    device.device_config = {"foo": "bar"}
    await db_session.commit()

    bundle = await PortabilityExportService().build_export_bundle(db_session)

    assert bundle.schema_version == 1
    assert bundle.source_instance is None
    assert len(bundle.devices) == 1
    exported = bundle.devices[0]
    assert exported.pack_id == device.pack_id
    assert exported.platform_id == device.platform_id
    assert exported.identity_scheme == device.identity_scheme
    assert exported.identity_scope == device.identity_scope
    assert exported.identity_value == device.identity_value
    assert exported.name == device.name
    assert exported.device_type == device.device_type
    assert exported.connection_type == device.connection_type
    assert exported.connection_target == device.connection_target
    assert exported.tags == {"team": "qa"}
    assert exported.device_config == {"foo": "bar"}
    assert exported.test_data == {"creds": {"u": "a"}}
    assert exported.original_host.hostname == host.hostname
    assert exported.original_host.host_id == host.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_export_bundle_does_not_include_runtime_fields(db_session: AsyncSession) -> None:
    from app.portability.services.export import PortabilityExportService
    from tests.helpers import seed_host_and_device

    await seed_host_and_device(db_session, identity="EXPORT-2")
    bundle = await PortabilityExportService().build_export_bundle(db_session)
    exported = bundle.devices[0]
    dumped = exported.model_dump()
    forbidden = {
        "operational_state",
        "hold",
        "lifecycle_policy_state",
        "verified_at",
        "battery_level_percent",
        "review_required",
        "session_viability_status",
        "host_id",
        "id",
    }
    assert not (forbidden & dumped.keys())


@pytest.mark.asyncio
@pytest.mark.db
async def test_export_endpoint_returns_bundle(client: AsyncClient, db_session: AsyncSession) -> None:
    from tests.helpers import seed_host_and_device

    await seed_host_and_device(db_session, identity="ENDPOINT-1")

    response = await client.get("/api/portability/export")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert len(body["devices"]) == 1
    cd = response.headers["content-disposition"]
    assert cd.startswith("attachment; filename=")
    assert cd.endswith('.json"')


@pytest.mark.asyncio
@pytest.mark.db
async def test_inventory_endpoint_filters_by_group(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """The inventory export honors repeated ?group= params with AND semantics."""
    from tests.helpers import create_device_record

    east_tv = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="inv-east-tv",
        connection_target="inv-east-tv",
        name="Inv East TV",
        operational_state="available",
    )
    east_phone = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="inv-east-phone",
        connection_target="inv-east-phone",
        name="Inv East Phone",
        operational_state="available",
    )

    async def _add_static_group(key: str, device_ids: list[uuid.UUID]) -> None:
        group = DeviceGroup(key=key, name=key, group_type=GroupType.static)
        db_session.add(group)
        await db_session.flush()
        for device_id in device_ids:
            db_session.add(DeviceGroupMembership(group_id=group.id, device_id=device_id))
        await db_session.commit()

    await _add_static_group("east", [east_tv.id, east_phone.id])
    await _add_static_group("tv", [east_tv.id])

    response = await client.get(
        "/api/portability/inventory",
        params=[("group", "east"), ("group", "tv"), ("columns", "identity.value")],
    )
    assert response.status_code == 200
    payload = response.json()
    values = {row["identity"]["value"] for row in payload}
    assert values == {"inv-east-tv"}


@pytest.mark.asyncio
@pytest.mark.db
async def test_inventory_endpoint_rejects_unknown_group(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    response = await client.get("/api/portability/inventory", params=[("group", "missing")])
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "unknown device groups: missing"
