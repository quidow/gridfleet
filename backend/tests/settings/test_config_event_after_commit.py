"""Contract tests for host/config event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.hosts.schemas import HostRegister
from app.hosts.service import HostCrudService
from app.settings.service_config import SettingsConfigService
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CAPS_V2 = {"orchestration_contract_version": 2}


async def test_register_host_queues_host_registered(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    payload = HostRegister(
        hostname="contract-host", ip="10.0.0.42", os_type="linux", agent_port=5100, capabilities=CAPS_V2
    )
    host, _is_new = await HostCrudService(publisher=event_bus, settings=FakeSettingsReader({})).register_host(
        db_session, payload
    )
    await settle_after_commit_tasks()

    registered = [p for n, p in event_bus_capture if n == "host.registered"]
    assert len(registered) == 1
    assert registered[0]["host_id"] == str(host.id)


async def test_approve_host_queues_status_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    payload = HostRegister(
        hostname="approve-host", ip="10.0.0.43", os_type="linux", agent_port=5100, capabilities=CAPS_V2
    )
    host, _ = await HostCrudService(
        publisher=event_bus, settings=FakeSettingsReader({"agent.auto_accept_hosts": False})
    ).register_host(db_session, payload)
    assert host.status.value == "pending"
    event_bus_capture.clear()

    approved = await HostCrudService(publisher=event_bus, settings=FakeSettingsReader({})).approve_host(
        db_session, host.id
    )
    assert approved is not None
    await settle_after_commit_tasks()

    changed = [p for n, p in event_bus_capture if n == "host.status_changed"]
    assert len(changed) == 1
    assert changed[0]["new_status"] == "online"


async def test_merge_device_config_queues_config_updated(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="config-merge-1")
    event_bus_capture.clear()

    await SettingsConfigService(publisher=event_bus).merge_device_config(
        db_session, device, {"wifi": {"ssid": "lab"}}, changed_by="tester"
    )
    await settle_after_commit_tasks()

    updated = [p for n, p in event_bus_capture if n == "config.updated"]
    assert len(updated) == 1
    assert updated[0]["device_id"] == str(device.id)
