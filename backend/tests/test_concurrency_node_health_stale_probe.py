"""Verify node_health skips stale probe results after node changes."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import node_health as node_health
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from app.hosts.models import Host
from tests.conftest import settings_service
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def _seed_running_node_at_failure_threshold(
    db_session: AsyncSession,
    db_host: Host,
    *,
    name: str,
    pid: int = 1111,
    active_connection_target: str = "old-target",
) -> tuple[Device, AppiumNode]:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name=name,
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=pid,
            active_connection_target=active_connection_target,
        )
    db_session.add(node)
    await db_session.commit()

    threshold = int(settings_service.get("general.node_max_failures"))
    node.consecutive_health_failures = threshold - 1
    await db_session.commit()
    return device, node


async def _run_node_health_with_gate(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    probe_complete: asyncio.Event,
    allow_processing: asyncio.Event,
) -> None:
    async def unhealthy_probe(*_args: object, **_kwargs: object) -> ProbeResult:
        probe_complete.set()
        await asyncio.wait_for(allow_processing.wait(), timeout=2.0)
        return ProbeResult(status="refused")

    fake_grid = AsyncMock()
    fake_grid.get_status = AsyncMock(return_value={})
    fake_grid.available_node_device_ids = Mock(return_value=set())

    with (
        patch.object(node_health.NodeHealthService, "_check_node_health", side_effect=unhealthy_probe),
        patch("app.appium_nodes.services.node_health.assert_current_leader"),
    ):
        async with db_session_maker() as session:
            await node_health.NodeHealthService(
                publisher=event_bus,
                settings=FakeSettingsReader({}),
                pool=Mock(),
                circuit_breaker=Mock(),
                grid=fake_grid,
            ).check_nodes(session)


async def test_stale_unhealthy_probe_skips_when_node_stopped_before_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A stopped node must not be marked error by a stale unhealthy probe."""
    device, _node = await _seed_running_node_at_failure_threshold(db_session, db_host, name="stale-stopped")
    device_id = device.id

    probe_complete = asyncio.Event()
    allow_processing = asyncio.Event()

    async def stopper() -> None:
        await asyncio.wait_for(probe_complete.wait(), timeout=2.0)
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.device_id == device_id)
                .values(pid=None, active_connection_target=None, health_running=None, health_state=None)
            )
            await session.commit()
        allow_processing.set()

    await asyncio.gather(
        _run_node_health_with_gate(
            db_session_maker,
            probe_complete=probe_complete,
            allow_processing=allow_processing,
        ),
        stopper(),
    )

    async with db_session_maker() as verify:
        verified = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert not verified.observed_running, (
        f"Expected observed_running=False but got observed_running={verified.observed_running} - "
        "node_health processed a stale unhealthy probe for a stopped node"
    )


async def test_stale_unhealthy_probe_skips_when_node_restarted_before_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A restarted running node must not inherit the previous probe result."""
    device, _node = await _seed_running_node_at_failure_threshold(db_session, db_host, name="stale-restarted")
    device_id = device.id

    probe_complete = asyncio.Event()
    allow_processing = asyncio.Event()

    async def restarter() -> None:
        await asyncio.wait_for(probe_complete.wait(), timeout=2.0)
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.device_id == device_id)
                .values(
                    pid=2222,
                    active_connection_target="new-target",
                    health_running=None,
                    health_state=None,
                )
            )
            await session.commit()
        allow_processing.set()

    await asyncio.gather(
        _run_node_health_with_gate(
            db_session_maker,
            probe_complete=probe_complete,
            allow_processing=allow_processing,
        ),
        restarter(),
    )

    async with db_session_maker() as verify:
        verified = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verified.observed_running
    assert verified.pid == 2222
    assert verified.active_connection_target == "new-target"
