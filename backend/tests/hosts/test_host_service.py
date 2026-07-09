from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import ConnectionType, DeviceType
from app.hosts import service as host_service
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.schemas import HostCreate, HostRegister
from app.hosts.service import HostCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record

CAPS_V5 = {"orchestration_contract_version": 5}


def test_validate_orchestration_contract_ignores_unknown_capability_keys() -> None:
    host_service.validate_orchestration_contract(
        {"orchestration_contract_version": 5, "future_agent_capability": True},
        host_label="newer-agent",
    )


def test_coerce_missing_prerequisites_filters_duplicates_and_invalid_items() -> None:
    assert host_service._coerce_missing_prerequisites(["adb", "adb", 1, "java"]) == ["adb", "java"]
    assert host_service._coerce_missing_prerequisites("bad") is None


def test_normalize_capabilities_handles_missing_prerequisites() -> None:
    assert host_service.normalize_capabilities(None) is None
    assert host_service.orchestration_contract_version({"orchestration_contract_version": True}) is None
    assert host_service.normalize_capabilities({"missing_prerequisites": ["adb", "adb", 3]}) == {
        "missing_prerequisites": ["adb"]
    }
    assert host_service.normalize_capabilities({"tools": {"appium": "3.3.0", "adb": "1.0.41"}}) == {
        "tools": {"adb": "1.0.41"}
    }


def test_update_missing_prerequisites_from_health_updates_host_capabilities() -> None:
    host = Host(hostname="host-1", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host_service.update_missing_prerequisites_from_health(host, ["adb", "java", "adb"])

    assert host.capabilities == {"missing_prerequisites": ["adb", "java"]}
    host_service.update_missing_prerequisites_from_health(host, "bad")
    assert host.capabilities == {"missing_prerequisites": ["adb", "java"]}


async def test_create_and_delete_host(db_session: AsyncSession) -> None:
    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({"agent.default_port": 6200}))
    host = await svc.create_host(
        db_session,
        HostCreate(hostname="create-host", ip="10.0.0.10", os_type=OSType.linux, agent_port=None),
    )
    assert host.agent_port == 6200

    assert await svc.delete_host(db_session, host.id) is True
    assert await svc.get_host(db_session, host.id) is None


async def test_delete_host_rejects_attached_devices(db_session: AsyncSession) -> None:
    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    host = await svc.create_host(
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
        await svc.delete_host(db_session, host.id)


async def test_register_host_does_not_resurrect_offline_host(db_session: AsyncSession) -> None:
    host = Host(
        hostname="re-register",
        ip="10.0.0.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add(host)
    await db_session.commit()

    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    registered, is_new = await svc.register_host(
        db_session,
        HostRegister(
            hostname="re-register",
            ip="10.0.0.99",
            os_type=OSType.macos,
            agent_port=None,
            agent_version="2.0.0",
            capabilities={**CAPS_V5, "missing_prerequisites": ["adb", 5]},
        ),
    )

    assert is_new is False
    assert registered.ip == "10.0.0.99"
    assert registered.os_type == OSType.macos
    # only a status push flips a host online
    assert registered.status == HostStatus.offline
    assert registered.capabilities == {**CAPS_V5, "missing_prerequisites": ["adb"]}


async def test_register_host_creates_pending_or_online_host_based_on_setting(
    db_session: AsyncSession,
) -> None:
    svc_pending = HostCrudService(
        publisher=event_bus,
        settings=FakeSettingsReader({"agent.auto_accept_hosts": False, "agent.default_port": 5151}),
    )
    host, is_new = await svc_pending.register_host(
        db_session,
        HostRegister(
            hostname="pending-host",
            ip="10.0.0.20",
            os_type=OSType.linux,
            agent_port=None,
            capabilities=CAPS_V5,
        ),
    )

    assert is_new is True
    assert host.status == HostStatus.pending
    assert host.agent_port == 5151

    svc_online = HostCrudService(
        publisher=event_bus,
        settings=FakeSettingsReader({"agent.auto_accept_hosts": True, "agent.default_port": 5200}),
    )
    online_host, _ = await svc_online.register_host(
        db_session,
        HostRegister(
            hostname="online-host",
            ip="10.0.0.21",
            os_type=OSType.linux,
            agent_port=None,
            capabilities=CAPS_V5,
        ),
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

    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    approved = await svc.approve_host(db_session, pending.id)
    assert approved is not None
    assert approved.status == HostStatus.online
    assert await svc.approve_host(db_session, online.id) is None

    assert await svc.reject_host(db_session, reject_me.id) is True
    assert db_session.in_transaction() is False
    assert await svc.reject_host(db_session, online.id) is False


async def test_list_hosts_and_missing_host_paths(db_session: AsyncSession) -> None:
    host = Host(hostname="aaa", ip="10.0.0.40", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.commit()

    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    hosts = await svc.list_hosts(db_session)
    assert [item.hostname for item in hosts] == ["aaa"]
    assert await svc.delete_host(db_session, uuid4()) is False


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

    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    updated, is_new = await svc.register_host(
        db_session,
        HostRegister(
            hostname="report-host",
            ip="10.0.0.51",
            os_type=OSType.linux,
            agent_port=5200,
            capabilities=CAPS_V5,
        ),
    )

    assert is_new is False
    assert updated.agent_port == 5200


async def test_register_host_rejects_unsupported_agent_contract(db_session: AsyncSession) -> None:
    svc = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    unsupported_values = (
        None,
        {},
        {"orchestration_contract_version": 1},
        {"orchestration_contract_version": 4},  # pre-5 agents are now rejected
        {"orchestration_contract_version": "bad"},
    )
    for capabilities in unsupported_values:
        with pytest.raises(ValueError, match="orchestration contract"):
            await svc.register_host(
                db_session,
                HostRegister(
                    hostname=f"unsupported-{capabilities}",
                    ip="10.0.0.60",
                    os_type=OSType.linux,
                    agent_port=5100,
                    capabilities=capabilities,
                ),
            )
