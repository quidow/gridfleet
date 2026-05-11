"""Dispatcher regression: desired_state alone must not make a device claimable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.appium_node import AppiumDesiredState, AppiumNode
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
            pid=None,
            active_connection_target=None,
            desired_state=AppiumDesiredState.running,
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


async def test_dispatcher_picks_device_when_pid_and_active_target_set_without_state_column(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="disp-pid", verified=True)
    device.operational_state = DeviceOperationalState.available
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=12345,
            active_connection_target=device.identity_value,
        )
    )
    await db_session.commit()

    from app.services import run_service

    candidates = await run_service._find_matching_devices(
        db_session,
        DeviceRequirement(pack_id=device.pack_id, platform_id=device.platform_id),
    )
    assert device.id in {candidate.id for candidate in candidates}


async def test_dispatcher_does_not_pick_device_when_pid_null(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="disp-no-pid", verified=True)
    device.operational_state = DeviceOperationalState.available
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=None,
            active_connection_target=None,
        )
    )
    await db_session.commit()

    from app.services import run_service

    candidates = await run_service._find_matching_devices(
        db_session,
        DeviceRequirement(pack_id=device.pack_id, platform_id=device.platform_id),
    )
    assert device.id not in {candidate.id for candidate in candidates}
