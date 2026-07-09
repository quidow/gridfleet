from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode, AppiumNodeResourceClaim
from app.appium_nodes.routers.agent_state import _get_desired
from app.appium_nodes.services.reconciler_agent import build_node_launch_payload
from app.devices.models import DeviceOperationalState
from app.hosts.models import Host, HostStatus, OSType
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, test_event_bus

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
    )
    stopped_node = AppiumNode(
        device_id=stopped_device.id,
        port=4724,
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
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
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/agent/appium-nodes/desired", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    payload = response.json()
    assert {node["device_id"] for node in payload["nodes"]} == {
        str(running_device.id),
        str(stopped_device.id),
    }
    by_device = {node["device_id"]: node for node in payload["nodes"]}
    assert by_device[str(running_device.id)]["restart_requested_at"] is None
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
    grid_run_id = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4730,
        desired_state=AppiumDesiredState.running,
        desired_port=4730,
        accepting_new_sessions=False,
        stop_pending=True,
        desired_grid_run_id=grid_run_id,
    )
    db_session.add(node)
    await db_session.commit()
    device.appium_node = node
    settings = FakeSettingsReader()

    pushed = await build_node_launch_payload(
        db_session,
        device,
        port=4730,
        allocated_caps=None,
        settings=settings,
    )
    db_session.expire(device, ["appium_node"])
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


async def test_desired_launch_projects_reserved_parallel_resources(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-claims",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        live_capabilities={"appium:derivedDataPath": "/tmp/gridfleet/derived-data/test"},
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add_all(
        [
            AppiumNodeResourceClaim(host_id=db_host.id, capability_key="appium:systemPort", port=8207, node_id=node.id),
            AppiumNodeResourceClaim(
                host_id=db_host.id, capability_key="appium:mjpegServerPort", port=9203, node_id=node.id
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/agent/appium-nodes/desired", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    launch = response.json()["nodes"][0]["launch"]
    assert launch["allocated_caps"] == {
        "appium:systemPort": 8207,
        "appium:mjpegServerPort": 9203,
        "appium:derivedDataPath": "/tmp/gridfleet/derived-data/test",
    }
    assert launch["extra_caps"]["appium:systemPort"] == 8207
    assert launch["extra_caps"]["appium:derivedDataPath"] == "/tmp/gridfleet/derived-data/test"


async def test_two_nodes_started_via_pull_get_distinct_parallel_ports(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Spec acceptance: two same-driver nodes on one host get distinct systemPort via the pull path."""
    dev_a = await create_device(db_session, host_id=db_host.id, name="pull-par-a", verified=True)
    dev_b = await create_device(db_session, host_id=db_host.id, name="pull-par-b", verified=True)
    operator = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=test_event_bus
    )
    await db_session.refresh(dev_a, attribute_names=["appium_node"])
    node_a = await operator.request_start(db_session, dev_a, caller="operator_route", reason="test")
    await db_session.refresh(dev_b, attribute_names=["appium_node"])
    node_b = await operator.request_start(db_session, dev_b, caller="operator_route", reason="test")
    assert node_a.desired_state == AppiumDesiredState.running
    assert node_b.desired_state == AppiumDesiredState.running
    await db_session.commit()

    desired = await _get_desired(db_session, db_host.id, settings=FakeSettingsReader())

    by_device = {spec.device_id: spec for spec in desired.nodes}
    launch_a = by_device[dev_a.id].launch
    launch_b = by_device[dev_b.id].launch
    assert launch_a is not None and launch_b is not None
    caps_a = launch_a["allocated_caps"]
    caps_b = launch_b["allocated_caps"]
    assert caps_a is not None and caps_b is not None
    assert caps_a["appium:systemPort"] != caps_b["appium:systemPort"]
    assert launch_a["extra_caps"]["appium:systemPort"] != launch_b["extra_caps"]["appium:systemPort"]
