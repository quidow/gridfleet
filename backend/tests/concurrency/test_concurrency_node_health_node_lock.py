import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import node_health
from app.devices.models import DeviceOperationalState
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_node_health_failure_path_locks_appium_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When ``_process_node_health`` writes node health error fields, the AppiumNode row must be locked."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="nh-lock",
        operational_state=DeviceOperationalState.busy,
        verified=True,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id
    node_id = node.id

    stomper_can_go = asyncio.Event()
    original_record_event = node_health.record_event

    async def racing_record_event(*args: object, **kwargs: object) -> None:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return await original_record_event(*args, **kwargs)

    async def health_runner() -> None:
        async with db_session_maker() as session:
            from app.devices import locking as device_locking

            locked_device = await device_locking.lock_device_handle(session, device_id)
            with patch("app.appium_nodes.services.node_health.record_event", racing_record_event):
                from app.appium_nodes.services.node_health import NodeHealthService, _NodeObservation

                assert locked_device.device.appium_node is not None
                await NodeHealthService(
                    publisher=event_bus,
                    settings=FakeSettingsReader({}),
                    recovery_control=AsyncMock(),
                    health=AsyncMock(),
                    incidents=AsyncMock(),
                )._process_node_health(
                    session,
                    locked_device.device.appium_node,
                    locked_device,
                    object(),  # type: ignore[arg-type]
                    observation=_NodeObservation(ProbeResult(status="refused")),
                )
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            # Use a core UPDATE to guarantee a real SQL statement is always
            # issued, bypassing ORM dirty-tracking (which would skip the UPDATE
            # when the value matches the in-memory snapshot).
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.id == node_id)
                .values(pid=12345, active_connection_target="stomper-target", health_running=None, health_state=None)
            )
            await session.commit()

    await asyncio.gather(health_runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.observed_running, (
        f"Expected observed_running=True but got observed_running={verify_node.observed_running} — "
        "node_health overwrote the concurrent running write (missing AppiumNode lock)"
    )
    assert verify_node.pid == 12345
    assert verify_node.active_connection_target == "stomper-target"


async def test_node_health_lock_order(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.appium_nodes.services import locking as appium_node_locking
    from app.appium_nodes.services.node_health import NodeHealthService
    from app.devices import locking as device_locking
    from tests.fakes import FakeSettingsReader

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="nh-lock-order",
        operational_state=DeviceOperationalState.busy,
        verified=True,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    lock_order = []

    original_lock_device = device_locking.lock_device
    original_lock_device_handle = getattr(device_locking, "lock_device_handle", None)
    original_lock_node = appium_node_locking.lock_appium_node_for_device

    async def track_lock_device(*args: object, **kwargs: object) -> object:
        lock_order.append("device")
        return await original_lock_device(*args, **kwargs)

    async def track_lock_device_handle(*args: object, **kwargs: object) -> object:
        lock_order.append("device")
        if original_lock_device_handle:
            return await original_lock_device_handle(*args, **kwargs)
        raise RuntimeError("No lock_device_handle")

    async def track_lock_node(*args: object, **kwargs: object) -> object:
        lock_order.append("node")
        return await original_lock_node(*args, **kwargs)

    monkeypatch.setattr(device_locking, "lock_device", track_lock_device)
    if hasattr(device_locking, "lock_device_handle"):
        monkeypatch.setattr(device_locking, "lock_device_handle", track_lock_device_handle)
    monkeypatch.setattr(appium_node_locking, "lock_appium_node_for_device", track_lock_node)

    from app.devices.services.health import DeviceHealthService

    svc = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.node_fail_window_sec": 60, "appium_reconciler.restart_window_sec": 300}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    section = {
        "reported_at": "2026-07-22T00:00:00Z",
        "nodes": [{"port": 4723, "pid": 1, "connection_target": "target", "running": True}],
    }
    await svc.fold_host_nodes(db_session, db_host.id, section)

    assert lock_order == ["device", "node"]
