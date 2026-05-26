"""Contract tests for host/config event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.hosts import service as host_service
from app.hosts.schemas import HostRegister
from app.settings import service_config as config_service
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

if TYPE_CHECKING:
    import pytest

from tests.helpers import test_event_bus as event_bus

CAPS_V2 = {"orchestration_contract_version": 2}


async def test_register_host_queues_host_registered(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    payload = HostRegister(
        hostname="contract-host", ip="10.0.0.42", os_type="linux", agent_port=5100, capabilities=CAPS_V2
    )
    host, _is_new = await host_service.register_host(db_session, payload, publisher=event_bus)
    await settle_after_commit_tasks()

    registered = [p for n, p in event_bus_capture if n == "host.registered"]
    assert len(registered) == 1
    assert registered[0]["host_id"] == str(host.id)


async def test_approve_host_queues_status_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.settings import settings_service as _ss

    real_settings_get = _ss.get
    monkeypatch.setattr(
        "app.settings.service.settings_service.get",
        lambda key: False if key == "agent.auto_accept_hosts" else real_settings_get(key),
    )

    payload = HostRegister(
        hostname="approve-host", ip="10.0.0.43", os_type="linux", agent_port=5100, capabilities=CAPS_V2
    )
    host, _ = await host_service.register_host(db_session, payload, publisher=event_bus)
    assert host.status.value == "pending"
    event_bus_capture.clear()

    approved = await host_service.approve_host(db_session, host.id, publisher=event_bus)
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

    await config_service.merge_device_config(
        db_session, device, {"wifi": {"ssid": "lab"}}, changed_by="tester", publisher=event_bus
    )
    await settle_after_commit_tasks()

    updated = [p for n, p in event_bus_capture if n == "config.updated"]
    assert len(updated) == 1
    assert updated[0]["device_id"] == str(device.id)
