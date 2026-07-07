from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.devices.models import ConnectionType
from app.devices.services.property_refresh import PropertyRefreshService
from app.hosts.models import Host, HostStatus, OSType
from app.packs.services.discovery import PackDiscoveryService
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
        operational_state="available",
    )
    offline_device = await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="refresh-002",
        connection_target="refresh-002",
        name="Refresh Two",
        operational_state="offline",
    )
    offline_host_device = await create_device_record(
        db_session,
        host_id=offline_host.id,
        identity_value="refresh-003",
        connection_target="refresh-003",
        name="Refresh Three",
        operational_state="available",
    )

    fetch_props = AsyncMock(return_value=None)

    class _DiscoveryDouble:
        fetch_pack_device_properties = fetch_props
        apply_pack_device_properties = AsyncMock()

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await svc.refresh_all_properties(db)

    refreshed_identity_values = [await_call.args[1].identity_value for await_call in fetch_props.await_args_list]
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
        operational_state="available",
    )
    second = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="refresh-b",
        connection_target="refresh-b",
        name="Refresh B",
        operational_state="available",
    )

    fetch_props = AsyncMock(side_effect=[RuntimeError("boom"), None])

    class _DiscoveryDouble:
        fetch_pack_device_properties = fetch_props
        apply_pack_device_properties = AsyncMock()

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await svc.refresh_all_properties(db)

    refreshed_identity_values = sorted(await_call.args[1].identity_value for await_call in fetch_props.await_args_list)
    assert refreshed_identity_values == sorted([first.identity_value, second.identity_value])


def _discovery_service(fetcher: AsyncMock | None = None) -> PackDiscoveryService:
    return PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(return_value={"candidates": []}),
        agent_get_pack_device_properties=fetcher or AsyncMock(return_value=None),
        settings=MagicMock(),
        circuit_breaker=MagicMock(),
        serializer=MagicMock(),
        identity_guard=MagicMock(),
    )


def _roku_device(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "identity_value": "SER123",
        "connection_target": "10.0.0.5",
        "pack_id": "roku",
        "connection_type": ConnectionType.network,
        "os_version": None,
        "os_version_display": None,
        "software_versions": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_fetch_pack_device_properties_passes_identity_value() -> None:
    fetcher = AsyncMock(return_value=None)
    svc = _discovery_service(fetcher)
    host = SimpleNamespace(ip="192.168.1.10", agent_port=5100)
    await svc.fetch_pack_device_properties(host, _roku_device())  # type: ignore[arg-type]
    assert fetcher.await_args is not None
    assert fetcher.await_args.kwargs["identity_value"] == "SER123"
    assert fetcher.await_args.args[2] == "10.0.0.5"  # still queries the known target


@pytest.mark.asyncio
async def test_apply_updates_connection_target_for_verified_identity() -> None:
    svc = _discovery_service()
    device = _roku_device()
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "SER123",
            "detected_properties": {"connection_target": "10.0.0.9", "os_version": "14.5"},
        },
    )
    assert device.connection_target == "10.0.0.9"
    assert device.os_version == "14.5"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_ignores_connection_target_on_identity_mismatch() -> None:
    svc = _discovery_service()
    device = _roku_device()
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "OTHER-SERIAL",
            "detected_properties": {"connection_target": "10.0.0.9"},
        },
    )
    assert device.connection_target == "10.0.0.5"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_no_commit_when_connection_target_unchanged() -> None:
    svc = _discovery_service()
    device = _roku_device()
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "SER123",
            "detected_properties": {"connection_target": "10.0.0.5"},
        },
    )
    assert device.connection_target == "10.0.0.5"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_skips_connection_target_for_non_network_device() -> None:
    """Emulator/USB connection targets are owned by intake/verification, not refresh.

    The android pack's discover reports the live adb serial while normalize
    reports the stable AVD name — letting refresh write both forms would make
    the row oscillate every cycle. Only network devices (the DHCP-move case)
    get the connection_target heal.
    """
    svc = _discovery_service()
    device = _roku_device(
        identity_value="avd:Television_1080p",
        connection_target="emulator-5554",
        pack_id="appium-uiautomator2",
        connection_type=ConnectionType.virtual,
    )
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "avd:Television_1080p",
            "detected_properties": {"connection_target": "Television_1080p"},
        },
    )
    assert device.connection_target == "emulator-5554"
    session.commit.assert_not_awaited()
