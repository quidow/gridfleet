from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.schemas.host import HostCreate, HostRegister, HostUpdate
from app.services import host_service
from tests.helpers import create_device_record


def test_coerce_missing_prerequisites_filters_duplicates_and_invalid_items() -> None:
    assert host_service._coerce_missing_prerequisites(["adb", "adb", 1, "java"]) == ["adb", "java"]
    assert host_service._coerce_missing_prerequisites("bad") is None


def test_normalize_capabilities_handles_missing_prerequisites() -> None:
    assert host_service._normalize_capabilities(None) is None
    assert host_service._normalize_capabilities({"missing_prerequisites": ["adb", "adb", 3]}) == {
        "missing_prerequisites": ["adb"]
    }


def test_update_missing_prerequisites_from_health_updates_host_capabilities() -> None:
    host = Host(hostname="host-1", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host_service.update_missing_prerequisites_from_health(host, ["adb", "java", "adb"])

    assert host.capabilities == {"missing_prerequisites": ["adb", "java"]}
    host_service.update_missing_prerequisites_from_health(host, "bad")
    assert host.capabilities == {"missing_prerequisites": ["adb", "java"]}


async def test_create_update_and_delete_host(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.host_service.settings_service.get", lambda key: 6200)

    host = await host_service.create_host(
        db_session,
        HostCreate(hostname="create-host", ip="10.0.0.10", os_type=OSType.linux, agent_port=None),
    )
    assert host.agent_port == 6200

    updated = await host_service.update_host(
        db_session,
        host.id,
        HostUpdate(capabilities={"missing_prerequisites": ["adb", "adb"]}, agent_version="1.2.3"),
    )
    assert updated is not None
    assert updated.capabilities == {"missing_prerequisites": ["adb"]}
    assert updated.agent_version == "1.2.3"

    assert await host_service.delete_host(db_session, host.id) is True
    assert await host_service.get_host(db_session, host.id) is None


async def test_delete_host_rejects_attached_devices(db_session: AsyncSession) -> None:
    host = await host_service.create_host(
        db_session,
        HostCreate(hostname="busy-host", ip="10.0.0.11", os_type=OSType.linux, agent_port=5100),
    )
    await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="busy-1",
        name="Busy Device",
        device_type=DeviceType.real_device.value,
        connection_type=ConnectionType.usb.value,
    )

    with pytest.raises(ValueError, match="Cannot delete host"):
        await host_service.delete_host(db_session, host.id)


async def test_register_host_updates_existing_offline_host(db_session: AsyncSession) -> None:
    host = Host(
        hostname="re-register",
        ip="10.0.0.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add(host)
    await db_session.commit()

    registered, is_new = await host_service.register_host(
        db_session,
        HostRegister(
            hostname="re-register",
            ip="10.0.0.99",
            os_type=OSType.macos,
            agent_port=None,
            agent_version="2.0.0",
            capabilities={"missing_prerequisites": ["adb", 5]},
        ),
    )

    assert is_new is False
    assert registered.ip == "10.0.0.99"
    assert registered.os_type == OSType.macos
    assert registered.status == HostStatus.online
    assert registered.capabilities == {"missing_prerequisites": ["adb"]}


async def test_register_host_creates_pending_or_online_host_based_on_setting(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.host_service.settings_service.get",
        lambda key: {"agent.auto_accept_hosts": False, "agent.default_port": 5151}[key],
    )
    host, is_new = await host_service.register_host(
        db_session,
        HostRegister(hostname="pending-host", ip="10.0.0.20", os_type=OSType.linux, agent_port=None),
    )

    assert is_new is True
    assert host.status == HostStatus.pending
    assert host.agent_port == 5151

    monkeypatch.setattr(
        "app.services.host_service.settings_service.get",
        lambda key: {"agent.auto_accept_hosts": True, "agent.default_port": 5200}[key],
    )
    online_host, _ = await host_service.register_host(
        db_session,
        HostRegister(hostname="online-host", ip="10.0.0.21", os_type=OSType.linux, agent_port=None),
    )
    assert online_host.status == HostStatus.online


async def test_approve_and_reject_host_only_work_for_pending(db_session: AsyncSession) -> None:
    pending = Host(
        hostname="pending-approve",
        ip="10.0.0.30",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.pending,
    )
    online = Host(
        hostname="already-online",
        ip="10.0.0.31",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    reject_me = Host(
        hostname="pending-reject",
        ip="10.0.0.32",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.pending,
    )
    db_session.add_all([pending, online, reject_me])
    await db_session.commit()

    approved = await host_service.approve_host(db_session, pending.id)
    assert approved is not None
    assert approved.status == HostStatus.online
    assert await host_service.approve_host(db_session, online.id) is None

    assert await host_service.reject_host(db_session, reject_me.id) is True
    assert await host_service.reject_host(db_session, online.id) is False


async def test_list_hosts_and_missing_host_paths(db_session: AsyncSession) -> None:
    host = Host(hostname="aaa", ip="10.0.0.40", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.commit()

    hosts = await host_service.list_hosts(db_session)
    assert [item.hostname for item in hosts] == ["aaa"]
    assert await host_service.delete_host(db_session, uuid4()) is False
    assert await host_service.update_host(db_session, uuid4(), HostUpdate()) is None


async def test_register_host_updates_agent_port_when_reprovided(db_session: AsyncSession) -> None:
    existing = Host(
        hostname="report-host",
        ip="10.0.0.50",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(existing)
    await db_session.commit()

    updated, is_new = await host_service.register_host(
        db_session,
        HostRegister(
            hostname="report-host",
            ip="10.0.0.51",
            os_type=OSType.linux,
            agent_port=5200,
        ),
    )

    assert is_new is False
    assert updated.agent_port == 5200
