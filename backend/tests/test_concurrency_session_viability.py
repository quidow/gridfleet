# backend/tests/test_concurrency_session_viability.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.sessions import service_viability as session_viability
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

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
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    appium_node = AppiumNode(
        device_id=device.id,
        port=9999,
        grid_url="http://node-grid:4444/wd/hub",
        desired_state=AppiumDesiredState.running,
        desired_port=9999,
        pid=0,
        active_connection_target="",
    )
    db_session.add(appium_node)
    await db_session.commit()
    device_id = device.id

    probe_started = asyncio.Event()
    external_done = asyncio.Event()
    observed_grid_url: str | None = None

    async def fake_probe(
        capabilities: dict[str, Any],
        timeout_sec: int,
        *,
        grid_url: str | None = None,
    ) -> tuple[bool, str | None]:
        nonlocal observed_grid_url
        observed_grid_url = grid_url
        probe_started.set()
        await external_done.wait()
        return True, None

    async def always_ready(*_a: object, **_kw: object) -> bool:
        return True

    async def fake_get_caps(*_a: object, **_kw: object) -> dict[str, Any]:
        return {"platformName": "Android"}

    monkeypatch.setattr(session_viability, "probe_session_via_grid", fake_probe)
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
            locked.hold = DeviceHold.reserved
        external_done.set()

    await asyncio.gather(run_probe(), reserve_externally())
    assert observed_grid_url == "http://node-grid:4444/wd/hub"

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    assert device_row.hold == DeviceHold.reserved, (
        f"Probe restored device to {device_row.operational_state} despite external reservation"
    )
