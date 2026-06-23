from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.devices.models import ConnectionType, Device, DeviceType
from app.devices.services import readiness as device_readiness
from app.packs.models import DriverPackPlatform
from tests.helpers import create_device_record, create_host
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_network_device_does_not_require_ip_without_pack_field(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="network-no-ip",
        connection_target="network-no-ip",
        name="Network No IP",
        connection_type="network",
        ip_address=None,
        verified=False,
    )

    readiness = await device_readiness.assess_device_async(db_session, device)

    assert readiness.readiness_state == "verification_required"
    assert readiness.missing_setup_fields == []
    assert readiness.can_verify_now is True


@pytest.mark.asyncio
async def test_network_device_requires_ip_when_pack_field_declares_it(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="network-needs-ip",
        connection_target="network-needs-ip",
        name="Network Needs IP",
        connection_type="network",
        ip_address=None,
        verified=False,
    )
    platform = (
        await db_session.execute(
            select(DriverPackPlatform).where(
                DriverPackPlatform.manifest_platform_id == "android_mobile",
            )
        )
    ).scalar_one()
    platform.data = {
        **platform.data,
        "device_fields_schema": [
            {
                "id": "ip_address",
                "label": "IP Address",
                "type": "network_endpoint",
                "required_for_session": True,
            }
        ],
    }
    await db_session.commit()

    readiness = await device_readiness.assess_device_async(db_session, device)

    assert readiness.readiness_state == "setup_required"
    assert readiness.missing_setup_fields == ["ip_address"]
    assert readiness.can_verify_now is False


def test_payload_requires_reverification_for_readiness_impacting_change() -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="impact-1",
        connection_target="impact-1",
        name="Impact Device",
        os_version="14",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    assert device_readiness.payload_requires_reverification(
        device,
        {"connection_target": "192.168.1.10:5555"},
    )
    assert not device_readiness.payload_requires_reverification(device, {"name": "Renamed Device"})


def test_readiness_impacting_fields_includes_tags() -> None:
    assert "tags" in device_readiness.READINESS_IMPACTING_FIELDS


def test_payload_requires_reverification_when_tags_change() -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="tag-test-1",
        connection_target="tag-test-1",
        name="Tag Test Device",
        os_version="14",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.tags = {"screen_type": "hd"}

    assert device_readiness.payload_requires_reverification(device, {"tags": {"screen_type": "4k"}})
    assert not device_readiness.payload_requires_reverification(device, {"tags": {"screen_type": "hd"}})


async def test_readiness_async_verified_and_unknown_assessment_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = SimpleNamespace(manifest_platform_id="android_mobile", data={})
    release = SimpleNamespace(platforms=[platform])
    pack = SimpleNamespace(id="pack", releases=[release], current_release=None)
    scalars_result = SimpleNamespace(all=lambda: [pack])
    session = SimpleNamespace(scalars=AsyncMock(return_value=scalars_result))
    device = SimpleNamespace(pack_id="pack", platform_id="android_mobile", device_type=None)
    monkeypatch.setattr(device_readiness, "selected_release", lambda _releases, _current: release)
    monkeypatch.setattr(
        device_readiness,
        "assess_device_from_required_fields",
        lambda _device, _fields: device_readiness.DeviceAssessment(
            readiness_state="verified",
            missing_setup_fields=[],
        ),
    )

    readiness = await device_readiness.assess_device_async(session, device)  # type: ignore[arg-type]

    assert readiness.readiness_state == "verified"
    assert await device_readiness.is_ready_for_use_async(session, device) is True  # type: ignore[arg-type]

    monkeypatch.setattr(
        device_readiness,
        "assess_device_from_required_fields",
        lambda _device, _fields: device_readiness.DeviceAssessment(
            readiness_state="unexpected",
            missing_setup_fields=[],
        ),
    )
    with pytest.raises(ValueError, match="Unknown readiness state"):
        await device_readiness.assess_device_async(session, device)  # type: ignore[arg-type]


async def test_assess_devices_async_batches_pack_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the batch helper loads all needed packs in one query and maps each device correctly."""
    platform_alpha = SimpleNamespace(manifest_platform_id="android_mobile", data={})
    release_alpha = SimpleNamespace(platforms=[platform_alpha])
    pack_alpha = SimpleNamespace(id="alpha", releases=[release_alpha], current_release=None)

    platform_beta = SimpleNamespace(manifest_platform_id="ios", data={})
    release_beta = SimpleNamespace(platforms=[platform_beta])
    pack_beta = SimpleNamespace(id="beta", releases=[release_beta], current_release=None)

    scalars_result = SimpleNamespace(all=lambda: [pack_alpha, pack_beta])
    scalars_mock = AsyncMock(return_value=scalars_result)
    session = SimpleNamespace(scalars=scalars_mock)

    import uuid as _uuid

    dev_alpha_id = _uuid.uuid4()
    dev_beta_id = _uuid.uuid4()
    dev_no_pack_id = _uuid.uuid4()

    devices = [
        SimpleNamespace(id=dev_alpha_id, pack_id="alpha", platform_id="android_mobile", device_type=None),
        SimpleNamespace(id=dev_beta_id, pack_id="beta", platform_id="ios", device_type=None),
        SimpleNamespace(id=dev_no_pack_id, pack_id=None, platform_id=None, device_type=None),
    ]

    monkeypatch.setattr(
        device_readiness, "selected_release", lambda releases, _current: releases[0] if releases else None
    )
    monkeypatch.setattr(
        device_readiness,
        "assess_device_from_required_fields",
        lambda _device, _fields: device_readiness.DeviceAssessment(
            readiness_state="verified",
            missing_setup_fields=[],
        ),
    )

    result = await device_readiness.assess_devices_async(session, devices)  # type: ignore[arg-type]

    # Exactly one batch scalars() call regardless of device count.
    assert scalars_mock.await_count == 1
    assert result[dev_alpha_id].readiness_state == "verified"
    assert result[dev_beta_id].readiness_state == "verified"
    assert result[dev_no_pack_id].readiness_state == "setup_required"
    assert result[dev_no_pack_id].missing_setup_fields == ["driver_pack"]


async def test_assess_devices_async_empty_input_skips_query(monkeypatch: pytest.MonkeyPatch) -> None:
    scalars_mock = AsyncMock()
    session = SimpleNamespace(scalars=scalars_mock)

    result = await device_readiness.assess_devices_async(session, [])  # type: ignore[arg-type]

    assert result == {}
    scalars_mock.assert_not_awaited()


async def test_assess_device_async_uses_provided_packs_without_querying(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the caller supplies the pack catalog, no per-device pack load is issued."""
    platform = SimpleNamespace(manifest_platform_id="android_mobile", data={})
    release = SimpleNamespace(platforms=[platform])
    pack = SimpleNamespace(id="alpha", releases=[release], current_release=None)
    scalars_mock = AsyncMock()
    session = SimpleNamespace(scalars=scalars_mock)
    monkeypatch.setattr(
        device_readiness, "selected_release", lambda releases, _current: releases[0] if releases else None
    )
    monkeypatch.setattr(
        device_readiness,
        "assess_device_from_required_fields",
        lambda _device, _fields: device_readiness.DeviceAssessment(readiness_state="verified", missing_setup_fields=[]),
    )
    device = SimpleNamespace(id="d", pack_id="alpha", platform_id="android_mobile", device_type=None)

    result = await device_readiness.assess_device_async(session, device, packs={"alpha": pack})  # type: ignore[arg-type]

    assert result.readiness_state == "verified"
    scalars_mock.assert_not_awaited()


@pytest.mark.db
async def test_assess_device_async_falls_back_when_supplied_catalog_missing_pack(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A caller-supplied catalog that lacks the device's pack_id (e.g. pack_id changed after
    a batch prefetch) must fall back to a DB load, not wrongly return setup_required."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session, host_id=host["id"], identity_value="catalog-fallback", name="catalog-fallback", verified=True
    )

    via_missing = await device_readiness.assess_device_async(db_session, device, packs={})
    baseline = await device_readiness.assess_device_async(db_session, device)

    assert via_missing == baseline
    assert via_missing.readiness_state == "verified"  # not setup_required despite the empty catalog


async def test_readiness_error_detail_setup_and_verification_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object()
    device = object()
    monkeypatch.setattr(
        device_readiness,
        "assess_device_async",
        AsyncMock(
            return_value=device_readiness.DeviceReadiness(
                readiness_state="setup_required",
                missing_setup_fields=["ip_address", "os_version"],
                can_verify_now=False,
            )
        ),
    )
    assert (
        await device_readiness.readiness_error_detail_async(session, device, action="start")
        == "Device cannot start until setup is complete (ip_address, os_version)"
    )

    device_readiness.assess_device_async.return_value = device_readiness.DeviceReadiness(
        readiness_state="verification_required",
        missing_setup_fields=[],
        can_verify_now=True,
    )
    assert (
        await device_readiness.readiness_error_detail_async(session, device, action="start")
        == "Device cannot start until verification succeeds"
    )
