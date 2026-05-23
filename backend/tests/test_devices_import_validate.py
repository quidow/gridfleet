import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.schemas.portability import ExportBundle, ExportedDevice, OriginalHost
from app.devices.services.portability_import import validate_bundle
from tests.helpers import seed_existing_device, seed_host_named


def _bundle(devices: list[ExportedDevice]) -> ExportBundle:
    return ExportBundle(
        schema_version=1,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        devices=devices,
    )


def _device(
    *,
    hostname: str = "lab-04",
    host_id: uuid.UUID | None = None,
    identity_value: str = "R58",
    identity_scope: str = "host",
    identity_scheme: str = "android_serial",
) -> ExportedDevice:
    return ExportedDevice(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme=identity_scheme,
        identity_scope=identity_scope,  # type: ignore[arg-type]
        identity_value=identity_value,
        name="Pixel",
        device_type="real_device",
        connection_type="usb",
        original_host=OriginalHost(hostname=hostname, host_id=host_id),
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_classifies_new_row_as_valid_new(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    host = await seed_host_named(db_session, "lab-04")
    preview = await validate_bundle(db_session, _bundle([_device(hostname="lab-04", host_id=host.id)]))
    assert preview.rows[0].status.value == "valid_new"
    assert preview.rows[0].host_suggestion is not None
    assert preview.rows[0].host_suggestion.id == host.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_hostname_match_is_case_insensitive(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    host = await seed_host_named(db_session, "Lab-Host-04")
    preview = await validate_bundle(db_session, _bundle([_device(hostname="lab-host-04")]))
    assert preview.rows[0].host_suggestion is not None
    assert preview.rows[0].host_suggestion.id == host.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_flags_intra_bundle_duplicates(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    await seed_host_named(db_session, "lab-04")
    preview = await validate_bundle(db_session, _bundle([_device(identity_value="R58"), _device(identity_value="R58")]))
    assert {row.status.value for row in preview.rows} == {"duplicate_in_bundle"}


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_flags_existing_global_identity_as_conflict_skip(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    await seed_host_named(db_session, "lab-04")
    await seed_existing_device(
        db_session,
        identity_scheme="udid",
        identity_value="GLOBAL-1",
        identity_scope="global",
    )
    preview = await validate_bundle(
        db_session,
        _bundle([_device(identity_value="GLOBAL-1", identity_scope="global", identity_scheme="udid")]),
    )
    assert preview.rows[0].status.value == "conflict_skip"


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_returns_bundle_hash_and_available_hosts(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    preview = await validate_bundle(db_session, _bundle([_device(hostname="lab-04", host_id=host.id)]))
    assert preview.bundle_hash.startswith("sha256:")
    assert any(h.id == host.id for h in preview.available_hosts)


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_endpoint_returns_preview(
    client: AsyncClient, db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    body = {
        "schema_version": 1,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "source_instance": "alpha",
        "devices": [
            {
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "identity_scheme": "android_serial",
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
            }
        ],
    }
    response = await client.post("/api/devices/import/validate", json=body)
    assert response.status_code == 200
    preview = response.json()
    assert preview["bundle_hash"].startswith("sha256:")
    assert preview["rows"][0]["status"] == "valid_new"
    assert preview["rows"][0]["host_suggestion"]["id"] == str(host.id)


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_endpoint_rejects_unknown_fields(client: AsyncClient) -> None:
    body = {
        "schema_version": 1,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "devices": [],
        "unexpected": True,
    }
    response = await client.post("/api/devices/import/validate", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_endpoint_rejects_unsupported_schema_version(client: AsyncClient) -> None:
    body = {
        "schema_version": 99,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "devices": [],
    }
    response = await client.post("/api/devices/import/validate", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_flags_unknown_pack_as_invalid(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    await seed_host_named(db_session, "lab-04")
    device = _device()
    device_dict = device.model_dump()
    device_dict["pack_id"] = "no-such-pack"
    device_dict["platform_id"] = "no-such-platform"
    bundle = _bundle([ExportedDevice.model_validate(device_dict)])
    preview = await validate_bundle(db_session, bundle)
    assert preview.rows[0].status.value == "invalid"
    assert any("pack" in i.lower() for i in preview.rows[0].issues)


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_same_identity_on_different_host_is_valid_new(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host_a = await seed_host_named(db_session, "lab-A")
    host_b = await seed_host_named(db_session, "lab-B")
    await seed_existing_device(
        db_session,
        host_id=host_a.id,
        identity_scheme="android_serial",
        identity_value="SAME-ID",
        identity_scope="host",
    )
    # Bundle device targets host-B (different host), same identity_value — should be valid_new.
    device = _device(hostname="lab-B", host_id=host_b.id, identity_value="SAME-ID")
    preview = await validate_bundle(db_session, _bundle([device]))
    assert preview.rows[0].status.value == "valid_new"


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_host_scoped_identity_on_original_host_is_conflict(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    await seed_existing_device(
        db_session,
        host_id=host.id,
        identity_scheme="android_serial",
        identity_value="SAME-ID",
        identity_scope="host",
    )
    device = _device(hostname="lab-04", host_id=host.id, identity_value="SAME-ID")
    preview = await validate_bundle(db_session, _bundle([device]))
    assert preview.rows[0].status.value == "conflict_skip"
