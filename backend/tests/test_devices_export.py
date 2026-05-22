import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.schemas.portability import (
    ExportBundle,
    ExportedDevice,
    ImportPreview,
    ImportRowStatus,
    OriginalHost,
)


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
                "auto_manage": True,
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
        "auto_manage": True,
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
    from app.devices.services.portability_export import build_export_bundle
    from tests.helpers import seed_host_and_device

    host, device = await seed_host_and_device(db_session, identity="EXPORT-1")
    device.tags = {"team": "qa"}
    device.test_data = {"creds": {"u": "a"}}
    device.device_config = {"foo": "bar"}
    await db_session.commit()

    bundle = await build_export_bundle(db_session, source_instance="alpha")

    assert bundle.schema_version == 1
    assert bundle.source_instance == "alpha"
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
    assert exported.auto_manage == device.auto_manage
    assert exported.tags == {"team": "qa"}
    assert exported.device_config == {"foo": "bar"}
    assert exported.test_data == {"creds": {"u": "a"}}
    assert exported.original_host.hostname == host.hostname
    assert exported.original_host.host_id == host.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_export_bundle_does_not_include_runtime_fields(db_session: AsyncSession) -> None:
    from app.devices.services.portability_export import build_export_bundle
    from tests.helpers import seed_host_and_device

    await seed_host_and_device(db_session, identity="EXPORT-2")
    bundle = await build_export_bundle(db_session)
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

    response = await client.get("/api/devices/export")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert len(body["devices"]) == 1
    cd = response.headers["content-disposition"]
    assert cd.startswith("attachment; filename=")
    assert cd.endswith('.json"')
