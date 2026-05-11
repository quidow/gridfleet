"""Phase 3 bulk + device-group desired-state caller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.models.appium_node import AppiumDesiredState, AppiumNode
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device
    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_bulk_start_nodes_tags_desired_state_as_bulk(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="bk-start", verified=True)
    await db_session.commit()

    captured: list[str] = []

    async def fake_start(_db: AsyncSession, dev: Device, caller: str) -> AppiumNode:
        captured.append(caller)
        return AppiumNode(
            device_id=dev.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=0,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )

    from app.services import bulk_service

    monkeypatch.setattr(bulk_service, "_bulk_start_one", fake_start)
    monkeypatch.setattr(bulk_service.event_bus, "publish", AsyncMock())
    await bulk_service.bulk_start_nodes(db_session, [device.id])

    assert captured == ["bulk"]


async def test_bulk_start_nodes_accepts_group_caller(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="grp-start", verified=True)
    await db_session.commit()

    captured: list[str] = []

    async def fake_start(_db: AsyncSession, dev: Device, caller: str) -> AppiumNode:
        captured.append(caller)
        return AppiumNode(
            device_id=dev.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=0,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )

    from app.services import bulk_service

    monkeypatch.setattr(bulk_service, "_bulk_start_one", fake_start)
    monkeypatch.setattr(bulk_service.event_bus, "publish", AsyncMock())
    await bulk_service.bulk_start_nodes(db_session, [device.id], caller="group")

    assert captured == ["group"]
