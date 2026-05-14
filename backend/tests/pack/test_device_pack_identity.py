import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import exc
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import ConnectionType, Device, DeviceType
from app.hosts.models import Host, HostStatus, OSType


@pytest.fixture
async def second_host(db_session: AsyncSession) -> AsyncGenerator[Host]:
    host = Host(
        hostname=f"db-host-{uuid.uuid4().hex[:8]}",
        ip="10.0.0.251",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    yield host


def _device(host_id: uuid.UUID, *, identity_scope: str, identity_value: str) -> Device:
    return Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope=identity_scope,
        identity_value=identity_value,
        connection_target=identity_value,
        name=f"device-{identity_value}",
        os_version="14",
        host_id=host_id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )


def test_device_accepts_pack_identity_columns() -> None:
    host_id = uuid.uuid4()

    device = _device(host_id, identity_scope="global", identity_value="serial-1")

    assert device.pack_id == "appium-uiautomator2"
    assert device.platform_id == "android_mobile"
    assert device.identity_scheme == "android_serial"
    assert device.identity_scope == "global"
    assert device.identity_value == "serial-1"


async def test_global_identity_scheme_value_is_unique(
    db_session: AsyncSession,
    db_host: Host,
    second_host: Host,
) -> None:
    first = _device(db_host.id, identity_scope="global", identity_value="serial-1")
    second = _device(second_host.id, identity_scope="global", identity_value="serial-1")
    db_session.add_all([first, second])

    with pytest.raises(exc.IntegrityError):
        await db_session.flush()


async def test_host_scoped_identity_scheme_value_can_repeat_across_hosts(
    db_session: AsyncSession,
    db_host: Host,
    second_host: Host,
) -> None:
    first = _device(db_host.id, identity_scope="host", identity_value="serial-1")
    second = _device(second_host.id, identity_scope="host", identity_value="serial-1")
    db_session.add_all([first, second])

    await db_session.flush()

    assert first.identity_scope == "host"
    assert second.identity_scope == "host"
