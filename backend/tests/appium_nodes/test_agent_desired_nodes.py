from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.routers.agent_state import _get_desired
from app.appium_nodes.services.reconciler_agent import build_node_launch_payload
from app.devices.models import DeviceOperationalState
from app.hosts.models import Host, HostStatus, OSType
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_desired_nodes_are_host_scoped_and_include_launch_only_for_running_nodes(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    other_host = Host(
        hostname="other-node-pull-host",
        ip="10.0.0.251",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(other_host)
    await db_session.flush()

    running_device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-running",
        operational_state=DeviceOperationalState.available,
    )
    stopped_device = await create_device(db_session, host_id=db_host.id, name="pull-stopped")
    other_device = await create_device(db_session, host_id=other_host.id, name="pull-other")
    running_node = AppiumNode(
        device_id=running_device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=7,
    )
    stopped_node = AppiumNode(
        device_id=stopped_device.id,
        port=4724,
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
        generation=8,
    )
    db_session.add_all(
        [
            running_node,
            stopped_node,
            AppiumNode(
                device_id=other_device.id,
                port=4725,
                desired_state=AppiumDesiredState.running,
                desired_port=4725,
                generation=99,
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/agent/appium-nodes/desired", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["generation_hint"] == 8
    assert {node["device_id"] for node in payload["nodes"]} == {
        str(running_device.id),
        str(stopped_device.id),
    }
    by_device = {node["device_id"]: node for node in payload["nodes"]}
    assert by_device[str(running_device.id)]["generation"] == 7
    assert by_device[str(running_device.id)]["launch"]["port"] == 4723
    assert by_device[str(running_device.id)]["launch"]["connection_target"] == running_device.connection_target
    assert by_device[str(stopped_device.id)]["launch"] is None
    assert by_device[str(stopped_device.id)]["unrunnable_reason"] is None


async def test_desired_launch_payload_matches_push_payload(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-channel-equality",
        operational_state=DeviceOperationalState.available,
    )
    device.host = db_host
    node = AppiumNode(
        device_id=device.id,
        port=4730,
        desired_state=AppiumDesiredState.running,
        desired_port=4730,
        generation=12,
    )
    db_session.add(node)
    await db_session.commit()
    settings = FakeSettingsReader()

    pushed = await build_node_launch_payload(
        db_session,
        device,
        port=4730,
        allocated_caps=None,
        settings=settings,
    )
    desired = await _get_desired(db_session, db_host.id, settings=settings)
    spec = next(item for item in desired.nodes if item.device_id == device.id)

    assert spec.launch == pushed


async def test_unrunnable_running_node_degrades_to_reason(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="pull-unrunnable")
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(
        "app.appium_nodes.routers.agent_state.build_node_launch_payload",
        AsyncMock(side_effect=NodeManagerError("pack is blocked")),
    )

    response = await client.get("/agent/appium-nodes/desired", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    assert response.json()["nodes"][0]["launch"] is None
    assert response.json()["nodes"][0]["unrunnable_reason"] == "pack is blocked"
