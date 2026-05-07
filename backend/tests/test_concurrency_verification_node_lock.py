import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceOperationalState
from app.models.host import Host
from app.services import device_verification_execution
from app.services.device_verification_job_state import new_job
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_retain_verified_node_locks_appium_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``retain_verified_node`` writes ``node.state`` and related fields. The
    AppiumNode row must be locked across that write.

    Timeline of the correctly-locked path:

      Runner: lock_device() → lock_appium_node_for_device() (FOR UPDATE held)
      Runner: ready_operational_state() -> fires event, yields 0.15 s
      Stomper: wakes up, issues UPDATE … SET state=error
              → Postgres BLOCKS it (runner holds FOR UPDATE)
      Runner: set_operational_state(), commit -> releases lock
      Stomper: UPDATE unblocks, commits state=error
      Final:  state == error ✓

    Without the AppiumNode lock the stomper commits freely before the runner
    releases anything, the runner's commit then overwrites with "running", and
    the assertion fails.
    """
    device = await create_device(db_session, host_id=db_host.id, name="dve-lock", verified=True)
    db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.stopped))
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()
    original_ready = device_verification_execution.ready_operational_state

    async def racing_ready(
        db: AsyncSession,
        target_device: Device,
    ) -> DeviceOperationalState:
        stomper_can_go.set()
        # Yield to the event loop so the stomper can issue its UPDATE to
        # Postgres.  The UPDATE will block at the Postgres level on the FOR
        # UPDATE lock the runner holds, keeping the stomper's transaction open
        # until the runner commits.
        await asyncio.sleep(0.15)
        return await original_ready(db, target_device)

    handle = device_verification_execution.TemporaryNodeHandle(
        port=4724,
        pid=99999,
        active_connection_target="udid-x",
        owner_key=None,
    )

    job = new_job("test-job-id")

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await session.get(Device, device_id)
            with (
                patch("app.services.device_verification_execution.set_stage", new=_noop_set_stage),
                patch(
                    "app.services.device_verification_execution.ready_operational_state",
                    new=racing_ready,
                ),
            ):
                await device_verification_execution.retain_verified_node(job, session, target, handle)

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode).where(AppiumNode.device_id == device_id).values(state=NodeState.error)
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.state == NodeState.error, (
        f"Expected error but got {verify_node.state.value} — "
        "retain_verified_node overwrote the concurrent error write (missing AppiumNode lock)"
    )


async def _noop_set_stage(*args: object, **kwargs: object) -> None:
    pass
