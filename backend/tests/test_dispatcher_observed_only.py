"""Dispatcher regression: desired_state alone must not make a device claimable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import DeviceOperationalState
from app.schemas.run import DeviceRequirement
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_dispatcher_does_not_pick_device_with_only_desired_running(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="disp-fence", verified=True)
    device.operational_state = DeviceOperationalState.offline
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=0,
            grid_url="http://hub:4444",
            state=NodeState.stopped,
            desired_state=NodeState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    from app.services import run_service

    candidates = await run_service._find_matching_devices(
        db_session,
        DeviceRequirement(pack_id=device.pack_id, platform_id=device.platform_id),
    )
    assert device.id not in {candidate.id for candidate in candidates}
