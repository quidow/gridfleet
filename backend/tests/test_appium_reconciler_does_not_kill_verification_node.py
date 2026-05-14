from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler import _fetch_node_rows, reconcile_host_orphans
from app.devices.models import DeviceOperationalState
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_reconciler_does_not_stop_node_during_verification(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="verify-reconciler", verified=False)
    device.operational_state = DeviceOperationalState.verifying
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=12345,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    agent_payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 12345,
                    "connection_target": device.connection_target,
                    "platform_id": device.platform_id,
                }
            ]
        }
    }
    appium_stop = AsyncMock()

    stopped = await reconcile_host_orphans(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        db_running_rows=await _fetch_node_rows(db_session),
        fetch_health=AsyncMock(return_value=agent_payload),
        appium_stop=appium_stop,
    )

    assert stopped == []
    appium_stop.assert_not_awaited()
