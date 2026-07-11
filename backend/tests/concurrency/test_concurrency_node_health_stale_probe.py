"""Verify node-health folds skip stale pushed observations after node changes."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.node_health import NodeHealthService
from app.core.timeutil import now_utc
from app.devices.models import DeviceOperationalState
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_fold_skips_stale_unhealthy_observation_after_node_restart(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="stale-restarted",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1111,
        active_connection_target="old-target",
    )
    db_session.add(node)
    await db_session.commit()
    section = {
        "reported_at": now_utc().isoformat(),
        "nodes": [
            {
                "port": node.port,
                "pid": node.pid,
                "connection_target": node.active_connection_target,
                "running": False,
                "observed_at": now_utc().isoformat(),
            }
        ],
    }
    async with db_session_maker() as session:
        await session.execute(
            update(AppiumNode)
            .where(AppiumNode.id == node.id)
            .values(pid=2222, active_connection_target="new-target", health_running=None, health_state=None)
        )
        await session.commit()

    async with db_session_maker() as session:
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            recovery_control=AsyncMock(),
            health=AsyncMock(),
            incidents=AsyncMock(),
        ).fold_host_nodes(session, db_host.id, section)

    async with db_session_maker() as verify:
        verified = (await verify.execute(select(AppiumNode).where(AppiumNode.id == node.id))).scalar_one()
    assert verified.health_failing_since is None
    assert verified.pid == 2222
    assert verified.active_connection_target == "new-target"
