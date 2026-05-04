from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.appium_node import AppiumNode, NodeState
from app.services import appium_resource_allocator, device_service
from app.services.node_service import restart_node, restart_node_via_agent, start_temporary_node, stop_temporary_node
from app.services.node_service_types import TemporaryNodeHandle
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


_ANDROID_PORTS: dict[str, int] = {
    "appium:systemPort": 8200,
    "appium:chromedriverPort": 9515,
    "appium:mjpegServerPort": 9200,
}
_XCUITEST_PORTS: dict[str, int] = {
    "appium:wdaLocalPort": 8100,
    "appium:mjpegServerPort": 9100,
}


async def test_android_allocations_reuse_release_and_reclaim(db_session: AsyncSession, db_host: Host) -> None:
    first = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key="device:first",
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )
    second = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key="device:second",
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )

    assert first == {
        "appium:systemPort": 8200,
        "appium:chromedriverPort": 9515,
        "appium:mjpegServerPort": 9200,
    }
    assert second == {
        "appium:systemPort": 8201,
        "appium:chromedriverPort": 9516,
        "appium:mjpegServerPort": 9201,
    }

    await appium_resource_allocator.release_owner(db_session, "device:first")
    await db_session.commit()

    third = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key="device:third",
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )
    assert third == first


async def test_transfer_owner_preserves_xcuitest_allocations(db_session: AsyncSession, db_host: Host) -> None:
    source_owner = f"temp:{db_host.id}:device-001"
    target_owner = "device:managed-001"

    original = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=source_owner,
        host_id=db_host.id,
        resource_ports=_XCUITEST_PORTS,
        needs_derived_data_path=True,
    )
    transferred = await appium_resource_allocator.transfer_owner(
        db_session,
        source_owner_key=source_owner,
        target_owner_key=target_owner,
    )
    await db_session.commit()

    assert transferred is not None
    assert transferred["capabilities"] == original
    assert await appium_resource_allocator.get_owner_capabilities(db_session, source_owner) is None
    assert await appium_resource_allocator.get_owner_capabilities(db_session, target_owner) == original
    assert isinstance(original["appium:derivedDataPath"], str)
    assert original["appium:derivedDataPath"].startswith("/tmp/gridfleet/derived-data/")


async def test_get_live_device_capabilities_only_for_running_nodes(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="allocator-live-001",
        connection_target="allocator-live-001",
        name="Allocator Live Device",
    )
    owner_key = appium_resource_allocator.managed_owner_key(device.id)
    expected = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=owner_key,
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running))
    await db_session.commit()

    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None
    assert loaded.appium_node is not None
    assert await appium_resource_allocator.get_live_device_capabilities(db_session, loaded) == expected

    loaded.appium_node.state = NodeState.stopped
    await db_session.commit()
    assert await appium_resource_allocator.get_live_device_capabilities(db_session, loaded) == {}


async def test_remote_node_manager_restart_reuses_existing_allocations(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="restart-managed-001",
        connection_target="restart-managed-001",
        name="Restart Managed Device",
        availability_status="available",
    )
    owner_key = appium_resource_allocator.managed_owner_key(device.id)
    existing_caps = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=owner_key,
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=123,
            state=NodeState.running,
        )
    )
    await db_session.commit()

    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    with (
        patch("app.services.node_service.start_remote_temporary_node", new_callable=AsyncMock) as start_mock,
        patch("app.services.node_service.stop_remote_temporary_node", new_callable=AsyncMock),
    ):
        start_mock.return_value = TemporaryNodeHandle(port=4723, pid=456)
        restarted = await restart_node(db_session, loaded)

    assert restarted.state == NodeState.running
    assert start_mock.await_args is not None
    assert start_mock.await_args.kwargs["allocated_caps"] == existing_caps


async def test_remote_node_manager_temporary_start_reuses_existing_managed_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="temp-managed-001",
        connection_target="temp-managed-001",
        name="Temp Managed Device",
        availability_status="available",
    )
    owner_key = appium_resource_allocator.managed_owner_key(device.id)
    existing_caps = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=owner_key,
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4726,
            grid_url="http://hub:4444",
            pid=321,
            active_connection_target="emulator-5554",
            state=NodeState.running,
        )
    )
    await db_session.commit()

    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    with patch("app.services.node_service.start_remote_temporary_node", new_callable=AsyncMock) as start_mock:
        handle = await start_temporary_node(db_session, loaded, owner_key=owner_key)

    assert handle.reused_existing is True
    assert handle.port == 4726
    assert handle.active_connection_target == "emulator-5554"
    assert handle.allocated_caps == existing_caps
    start_mock.assert_not_awaited()


async def test_remote_node_manager_reused_temporary_handle_skips_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="temp-managed-stop-001",
        connection_target="temp-managed-stop-001",
        name="Temp Managed Stop Device",
        availability_status="available",
    )
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    handle = TemporaryNodeHandle(port=4727, pid=654, reused_existing=True, owner_key="device:managed")
    with (
        patch("app.services.node_service.stop_remote_temporary_node", new_callable=AsyncMock) as stop_mock,
        patch(
            "app.services.node_service.appium_resource_allocator.release_owner",
            new_callable=AsyncMock,
        ) as release_mock,
    ):
        await stop_temporary_node(db_session, loaded, handle)

    stop_mock.assert_not_awaited()
    release_mock.assert_not_awaited()


async def test_restart_node_via_agent_reuses_existing_allocations(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="restart-agent-001",
        connection_target="restart-agent-001",
        name="Restart Agent Device",
    )
    owner_key = appium_resource_allocator.managed_owner_key(device.id)
    existing_caps = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=owner_key,
        host_id=db_host.id,
        resource_ports=_ANDROID_PORTS,
    )
    node = AppiumNode(device_id=device.id, port=4728, grid_url="http://hub:4444", pid=789, state=NodeState.running)
    db_session.add(node)
    await db_session.commit()
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None
    assert loaded.appium_node is not None

    stop_response = MagicMock()
    stop_response.raise_for_status.return_value = None
    start_response = MagicMock()
    start_response.raise_for_status.return_value = None
    start_response.json.return_value = {"pid": 999}

    with (
        patch("app.services.node_service.assert_runnable", new_callable=AsyncMock),
        patch("app.services.node_service.appium_stop", new_callable=AsyncMock, return_value=stop_response),
        patch(
            "app.services.node_service.appium_start",
            new_callable=AsyncMock,
            return_value=start_response,
        ) as start_mock,
        patch("app.services.node_service.appium_status", new_callable=AsyncMock, return_value={"running": True}),
        patch("app.services.node_service.asyncio.sleep", new_callable=AsyncMock),
    ):
        ok = await restart_node_via_agent(
            db_session,
            loaded,
            loaded.appium_node,
            http_client_factory=AsyncMock(),
        )

    assert ok is True
    assert start_mock.await_args is not None
    assert start_mock.await_args.kwargs["payload"]["allocated_caps"] == existing_caps
