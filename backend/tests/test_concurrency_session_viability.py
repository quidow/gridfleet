# backend/tests/test_concurrency_session_viability.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.services import device_locking, session_viability
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.host import Host

pytestmark = pytest.mark.asyncio


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_session_viability_restore_handles_external_reservation(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a session-viability probe is running (device marked busy), an external
    transaction reserves the device. The probe finishes and must NOT restore the
    device back to available — the reservation must be honored.

    NOTE: This test pins the post-fix invariant (external reservation is not clobbered)
    rather than demonstrating a red→green transition. The existing db.refresh() + guard
    in the pre-fix code happens to pass this particular scenario because db.refresh()
    picks up the external commit before the restore decision. The fix (re-lock via
    FOR UPDATE) makes this guarantee deterministic and extends protection to the
    except clause as well.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="probe-target",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    appium_node = AppiumNode(
        device_id=device.id,
        port=9999,
        grid_url="http://hub:4444",
        state=NodeState.running,
    )
    db_session.add(appium_node)
    await db_session.commit()
    device_id = device.id

    probe_started = asyncio.Event()
    external_done = asyncio.Event()

    async def fake_probe(
        db: AsyncSession,
        device_arg: Device,
        capabilities: dict[str, Any],
        timeout_sec: int,
    ) -> tuple[bool, str | None]:
        probe_started.set()
        await external_done.wait()
        return True, None

    async def always_ready(*_a: object, **_kw: object) -> bool:
        return True

    async def fake_get_caps(*_a: object, **_kw: object) -> dict[str, Any]:
        return {"platformName": "Android"}

    monkeypatch.setattr(session_viability, "probe_session_via_agent_node", fake_probe)
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", always_ready)
    monkeypatch.setattr(session_viability.capability_service, "get_device_capabilities", fake_get_caps)

    async def run_probe() -> None:
        async with db_session_maker() as session:
            stmt = (
                select(Device)
                .where(Device.id == device_id)
                .options(selectinload(Device.appium_node), selectinload(Device.host))
            )
            device_obj = (await session.execute(stmt)).scalar_one()
            await session_viability.run_session_viability_probe(session, device_obj, checked_by="manual")

    async def reserve_externally() -> None:
        await probe_started.wait()
        async with db_session_maker() as session, session.begin():
            locked = await device_locking.lock_device(session, device_id)
            locked.availability_status = DeviceAvailabilityStatus.reserved
        external_done.set()

    await asyncio.gather(run_probe(), reserve_externally())

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    assert device_row.availability_status == DeviceAvailabilityStatus.reserved, (
        f"Probe restored device to {device_row.availability_status} despite external reservation"
    )
