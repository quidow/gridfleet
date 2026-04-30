from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.host import Host, HostStatus, OSType
from app.services.pack_discovery_service import refresh_device_properties
from app.services.property_refresh import _refresh_all_properties
from tests.helpers import create_device_record


async def test_property_refresh_only_visits_online_hosts_and_non_offline_devices(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    online_host = Host(
        hostname="online-host",
        ip="10.0.0.10",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    offline_host = Host(
        hostname="offline-host",
        ip="10.0.0.11",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add_all([online_host, offline_host])
    await db_session.flush()

    online_device = await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="refresh-001",
        connection_target="refresh-001",
        name="Refresh One",
        availability_status="available",
    )
    offline_device = await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="refresh-002",
        connection_target="refresh-002",
        name="Refresh Two",
        availability_status="offline",
    )
    offline_host_device = await create_device_record(
        db_session,
        host_id=offline_host.id,
        identity_value="refresh-003",
        connection_target="refresh-003",
        name="Refresh Three",
        availability_status="available",
    )

    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    with (
        patch("app.services.property_refresh.async_session", session_factory),
        patch("app.services.property_refresh.pack_refresh_device_properties", new_callable=AsyncMock) as refresh_device,
    ):
        await _refresh_all_properties()

    refreshed_identity_values = [await_call.args[1].identity_value for await_call in refresh_device.await_args_list]
    assert online_device.identity_value in refreshed_identity_values
    assert offline_device.identity_value not in refreshed_identity_values
    assert offline_host_device.identity_value not in refreshed_identity_values


async def test_property_refresh_continues_after_device_failure(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    host = Host(
        hostname="online-host",
        ip="10.0.0.12",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()

    first = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="refresh-a",
        connection_target="refresh-a",
        name="Refresh A",
        availability_status="available",
    )
    second = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="refresh-b",
        connection_target="refresh-b",
        name="Refresh B",
        availability_status="available",
    )

    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    refresh_device = AsyncMock(side_effect=[RuntimeError("boom"), None])
    with (
        patch("app.services.property_refresh.async_session", session_factory),
        patch("app.services.property_refresh.pack_refresh_device_properties", refresh_device),
    ):
        await _refresh_all_properties()

    refreshed_identity_values = [await_call.args[1].identity_value for await_call in refresh_device.await_args_list]
    assert refreshed_identity_values == [first.identity_value, second.identity_value]


async def test_refresh_device_properties_preserves_discovery_identity_fields(
    db_session: AsyncSession,
) -> None:
    host = Host(
        hostname="online-host",
        ip="10.0.0.13",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()

    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        name="Pixel 6",
        platform_id="android_mobile",
        device_type="emulator",
        connection_type="virtual",
        availability_status="available",
    )

    agent_properties = AsyncMock(
        return_value={
            "detected_properties": {
                "os_version": "17",
                "connection_target": "emulator-5554",
                "connection_type": "usb",
                "ip_address": "192.168.1.20",
                "software_versions": {"android": "17"},
            }
        }
    )

    await refresh_device_properties(
        db_session,
        device,
        agent_get_pack_device_properties=agent_properties,
    )

    await db_session.refresh(device)
    assert device.os_version == "17"
    assert device.software_versions == {"android": "17"}
    assert device.connection_target == "Pixel_6"
    assert str(device.connection_type) == "virtual"
    assert device.ip_address is None
