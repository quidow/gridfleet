from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.timeutil import now_utc
from app.devices.models import ConnectionType
from app.devices.services.property_refresh import PropertyRefreshService
from app.hosts.models import Host, HostStatus, OSType
from app.packs.services.discovery import PackDiscoveryService
from tests.helpers import create_device_record


def _properties_section(*connection_targets: str) -> dict[str, object]:
    stamp = now_utc().isoformat()
    return {
        "reported_at": stamp,
        "devices": {
            target: {"identity_value": target, "detected_properties": {}, "observed_at": stamp}
            for target in connection_targets
        },
    }


async def test_fold_applies_only_to_section_devices_on_the_host(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    host = Host(hostname="fold-host", ip="10.0.0.10", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    other_host = Host(
        hostname="other-host", ip="10.0.0.11", os_type=OSType.linux, agent_port=5100, status=HostStatus.online
    )
    db_session.add_all([host, other_host])
    await db_session.flush()

    in_section = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-001", connection_target="refresh-001", name="One"
    )
    absent = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-002", connection_target="refresh-002", name="Two"
    )
    other = await create_device_record(
        db_session, host_id=other_host.id, identity_value="refresh-003", connection_target="refresh-003", name="Three"
    )

    apply = AsyncMock()

    class _DiscoveryDouble:
        apply_pack_device_properties = apply

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await svc.fold_host_device_properties(db, host.id, _properties_section("refresh-001"))

    applied = [await_call.args[1].identity_value for await_call in apply.await_args_list]
    assert in_section.identity_value in applied
    assert absent.identity_value not in applied  # not in section
    assert other.identity_value not in applied  # different host


async def test_fold_continues_after_device_failure(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    host = Host(hostname="fold-host", ip="10.0.0.12", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    first = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-a", connection_target="refresh-a", name="Refresh A"
    )
    second = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-b", connection_target="refresh-b", name="Refresh B"
    )

    # Capture identity_value in-context: the fold rolls back on the first
    # failure, which expires the passed instances, so reading identity_value
    # after the session closes would trigger a lazy load outside the greenlet.
    applied: list[str] = []

    async def _apply(_session: object, device: object, _data: object) -> None:
        applied.append(device.identity_value)  # type: ignore[attr-defined]
        if len(applied) == 1:
            raise RuntimeError("boom")

    class _DiscoveryDouble:
        apply_pack_device_properties = staticmethod(_apply)

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await svc.fold_host_device_properties(db, host.id, _properties_section("refresh-a", "refresh-b"))

    assert sorted(applied) == sorted([first.identity_value, second.identity_value])


def _discovery_service() -> PackDiscoveryService:
    return PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(return_value={"candidates": []}),
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
