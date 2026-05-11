"""Verify restart_node_via_agent ORM mutations are flushed by the caller's commit."""

from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode
from app.models.device import Device, DeviceOperationalState
from app.models.host import Host
from app.services import appium_reconciler_agent as node_service
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
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        health_running=False,
        health_state="error",
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
            patch("app.services.appium_reconciler_agent.appium_stop", stub_stop),
            patch("app.services.appium_reconciler_agent.appium_start", stub_start),
            patch("app.services.appium_reconciler_agent.assert_runnable", return_value=None),
            patch("app.services.appium_reconciler_agent.build_agent_start_payload", return_value={}),
            patch("app.services.appium_reconciler_agent._merge_appium_default_pack_caps", return_value=None),
            patch("app.services.appium_reconciler_agent.build_pack_start_payload", return_value=None),
            patch("app.services.appium_reconciler_agent.render_stereotype", return_value={}),
            patch(
                "app.services.appium_reconciler_agent.resolve_pack_platform",
                return_value=type("ResolvedPlatform", (), {"appium_platform_name": "Android"})(),
            ),
            patch("app.services.appium_reconciler_agent._build_session_aligned_start_caps", return_value={}),
            patch("app.services.appium_node_resource_service.get_capabilities", return_value={}),
            patch(
                "app.services.appium_reconciler_agent.resolve_pack_for_device",
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

        assert target_node.observed_running
        assert target_node.pid == 9999

        await session.commit()

    async with db_session_maker() as verify:
        row = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert row.observed_running, f"Expected observed_running=True, got observed_running={row.observed_running}"
    assert row.pid == 9999, f"Expected pid 9999, got {row.pid}"
    assert row.active_connection_target == "udid-commit-test"
