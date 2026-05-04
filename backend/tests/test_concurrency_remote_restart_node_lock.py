import asyncio
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device
from app.models.host import Host
from app.services import node_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_restart_node_via_agent_locks_device_and_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``restart_node_via_agent`` writes ``node.state``, ``node.pid``, and
    ``node.active_connection_target`` after a successful remote start. Those
    writes must hold the AppiumNode lock.
    """
    device = await create_device(db_session, host_id=db_host.id, name="nmr-lock", verified=True)
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.error)
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()

    async def stub_stop(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={},
            request=httpx.Request("POST", "http://example/stop"),
        )

    async def stub_appium_start(*_args: object, **kwargs: object) -> httpx.Response:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return httpx.Response(
            200,
            json={"connection_target": "udid-stub"},
            request=httpx.Request("POST", "http://example/start"),
        )

    async def stub_wait_ready(*_args: object, **_kwargs: object) -> None:
        return None

    async def runner() -> None:
        async with db_session_maker() as session:
            # Eagerly load appium_node to avoid lazy-load outside greenlet.
            from sqlalchemy.orm import selectinload

            target = (
                await session.execute(
                    select(Device)
                    .where(Device.id == device_id)
                    .options(selectinload(Device.appium_node), selectinload(Device.host))
                )
            ).scalar_one()
            target_node = target.appium_node
            with (
                patch("app.services.node_service.appium_stop", stub_stop),
                patch("app.services.node_service.appium_start", stub_appium_start),
                patch(
                    "app.services.node_service._wait_for_remote_appium_ready",
                    stub_wait_ready,
                ),
                patch("app.services.node_service.assert_runnable", return_value=None),
                patch("app.services.node_service.build_agent_start_payload", return_value={}),
                patch(
                    "app.services.node_service._merge_appium_default_pack_caps",
                    return_value=None,
                ),
                patch(
                    "app.services.node_service.build_pack_start_payload",
                    return_value=None,
                ),
                patch(
                    "app.services.node_service.render_stereotype",
                    return_value={},
                ),
                patch(
                    "app.services.node_service.resolve_pack_platform_fn",
                    return_value=type("RP", (), {"appium_platform_name": "Android"})(),
                ),
                patch(
                    "app.services.node_service._build_session_aligned_start_caps",
                    return_value={},
                ),
                patch(
                    "app.services.node_service.appium_resource_allocator.get_owner_capabilities",
                    return_value={},
                ),
                patch(
                    "app.services.node_service.resolve_pack_for_device",
                    return_value=("appium-uiautomator2", "android_mobile"),
                ),
            ):
                await node_service.restart_node_via_agent(
                    session,
                    target,
                    target_node,
                    http_client_factory=httpx.AsyncClient,
                )
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode).where(AppiumNode.device_id == device_id).values(state=NodeState.stopped)
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.state == NodeState.stopped, (
        f"Expected stopped but got {verify_node.state.value} — "
        "restart_node_via_agent overwrote the concurrent stopped write "
        "(missing AppiumNode lock)"
    )
