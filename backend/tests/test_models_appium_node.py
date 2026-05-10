"""Defaults for Appium desired-state columns on the ORM model."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.appium_node import AppiumNode, NodeState
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_appium_node_desired_state_defaults_to_stopped(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="default-desired", verified=True)
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444")
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_state == NodeState.stopped
    assert node.desired_port is None
    assert node.transition_token is None
    assert node.transition_deadline is None
    assert node.last_observed_at is None
