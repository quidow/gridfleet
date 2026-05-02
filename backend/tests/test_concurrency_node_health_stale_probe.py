"""Verify node_health skips stale probe results after node changes."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import control_plane_state_store, node_health
from app.services.settings_service import settings_service
from tests.helpers import create_device

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
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
        auto_manage=False,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        pid=pid,
        active_connection_target=active_connection_target,
    )
    db_session.add(node)
    await db_session.commit()

    threshold = int(settings_service.get("general.node_max_failures"))
    for _ in range(threshold - 1):
        await control_plane_state_store.increment_counter(db_session, node_health.NODE_HEALTH_NAMESPACE, str(node.id))
    await db_session.commit()
    return device, node


async def _run_node_health_with_gate(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    probe_complete: asyncio.Event,
    allow_processing: asyncio.Event,
) -> None:
    async def unhealthy_probe(*_args: object, **_kwargs: object) -> bool:
        probe_complete.set()
        await asyncio.wait_for(allow_processing.wait(), timeout=2.0)
        return False

    with (
        patch(
            "app.services.node_health._build_probe_capabilities_for_node",
            new=AsyncMock(return_value=None),
        ),
        patch("app.services.node_health._check_node_health", side_effect=unhealthy_probe),
        patch("app.services.node_health.grid_service.get_grid_status", new=AsyncMock(return_value={})),
        patch("app.services.node_health.grid_service.available_node_device_ids", return_value=set()),
    ):
        async with db_session_maker() as session:
            await node_health._check_nodes(session)


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
                update(AppiumNode).where(AppiumNode.device_id == device_id).values(state=NodeState.stopped, pid=None)
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

    assert verified.state == NodeState.stopped, (
        f"Expected stopped but got {verified.state.value} - "
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
                    state=NodeState.running,
                    pid=2222,
                    active_connection_target="new-target",
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

    assert verified.state == NodeState.running
    assert verified.pid == 2222
    assert verified.active_connection_target == "new-target"
