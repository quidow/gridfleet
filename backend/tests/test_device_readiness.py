from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceType
from app.models.driver_pack import DriverPackPlatform
from app.services import device_readiness
from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs


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
    pack = SimpleNamespace(releases=[release], current_release=None)
    session = SimpleNamespace(scalar=AsyncMock(return_value=pack))
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
