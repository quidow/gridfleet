"""Verify restart_node_via_agent ORM mutations are flushed by the caller's commit."""

from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import node_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_restart_mutations_visible_after_caller_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """restart_node_via_agent writes node fields and leaves commit to the caller."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="commit-prop",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.error,
        pid=None,
        active_connection_target=None,
    )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    async def stub_stop(*_a: object, **_kw: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={},
            request=httpx.Request("POST", "http://example/stop"),
        )

    async def stub_start(*_a: object, **_kw: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"pid": 9999, "connection_target": "udid-commit-test"},
            request=httpx.Request("POST", "http://example/start"),
        )

    async def stub_wait(*_a: object, **_kw: object) -> None:
        return None

    async with db_session_maker() as session:
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
            patch("app.services.node_service.appium_start", stub_start),
            patch("app.services.node_service._wait_for_remote_appium_ready", stub_wait),
            patch("app.services.node_service.assert_runnable", return_value=None),
            patch("app.services.node_service.build_agent_start_payload", return_value={}),
            patch("app.services.node_service._merge_appium_default_pack_caps", return_value=None),
            patch("app.services.node_service.build_pack_start_payload", return_value=None),
            patch("app.services.node_service.render_stereotype", return_value={}),
            patch(
                "app.services.node_service.resolve_pack_platform",
                return_value=type("ResolvedPlatform", (), {"appium_platform_name": "Android"})(),
            ),
            patch("app.services.node_service._build_session_aligned_start_caps", return_value={}),
            patch("app.services.node_service.appium_resource_allocator.get_owner_capabilities", return_value={}),
            patch(
                "app.services.node_service.resolve_pack_for_device",
                return_value=("appium-uiautomator2", "android_mobile"),
            ),
        ):
            result = await node_service.restart_node_via_agent(
                session,
                target,
                target_node,
                http_client_factory=httpx.AsyncClient,
            )
        assert result is True

        assert target_node.state == NodeState.running
        assert target_node.pid == 9999

        await session.commit()

    async with db_session_maker() as verify:
        row = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert row.state == NodeState.running, f"Expected running, got {row.state.value}"
    assert row.pid == 9999, f"Expected pid 9999, got {row.pid}"
    assert row.active_connection_target == "udid-commit-test"
