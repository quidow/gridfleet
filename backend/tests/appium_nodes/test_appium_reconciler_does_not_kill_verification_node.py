from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.agent_comm.snapshot import parse_running_nodes
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler import _fetch_desired_rows
from app.appium_nodes.services.reconciler_convergence import ObservedEntry, reap_orphan_nodes
from app.devices.models import DeviceOperationalState
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def _observed_from_payload(payload: dict[str, object]) -> list[ObservedEntry]:
    appium_processes = payload["appium_processes"]
    assert isinstance(appium_processes, dict)
    return [
        ObservedEntry(port=entry.port, pid=entry.pid, connection_target=entry.connection_target)
        for entry in parse_running_nodes(appium_processes)
    ]


async def test_reconciler_does_not_stop_node_during_verification(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="verify-reconciler", verified=False)
    device.operational_state = DeviceOperationalState.verifying
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=12345,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    observed = await _observed_from_payload(
        {
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
    )
    stop_agent = AsyncMock()

    reaped = await reap_orphan_nodes(observed, await _fetch_desired_rows(db_session), stop_agent=stop_agent)

    assert reaped == []
    stop_agent.assert_not_awaited()


async def test_reconciler_does_not_stop_emulator_node_reporting_live_serial(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    # A virtual emulator registers with the stable AVD name as its
    # connection_target, but the running node reports the live ADB serial
    # (cached on AppiumNode.active_connection_target). The orphan reaper
    # must recognise the node by its live serial and leave it running.
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="verify-emulator",
        verified=False,
        connection_target="Television_1080p",
        device_type="emulator",
        connection_type="virtual",
    )
    device.operational_state = DeviceOperationalState.verifying
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    observed = await _observed_from_payload(
        {
            "appium_processes": {
                "running_nodes": [
                    {
                        "port": 4723,
                        "pid": 12345,
                        "connection_target": "emulator-5554",
                        "platform_id": device.platform_id,
                    }
                ]
            }
        }
    )
    stop_agent = AsyncMock()

    reaped = await reap_orphan_nodes(observed, await _fetch_desired_rows(db_session), stop_agent=stop_agent)

    assert reaped == []
    stop_agent.assert_not_awaited()
