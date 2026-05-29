"""Bug 9: ``try_complete_drain`` disables a pack with a fresh run created mid-drain.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-9``.

``transition_pack_state`` for ``enabled → disabled`` writes ``state =
draining`` and commits at ``lifecycle.py:101`` so the draining state
becomes globally visible. It then calls ``try_complete_drain`` at line
102, which counts active work and, if zero, flips ``state =
disabled``. The count and the flip both run *outside* any pack-level
lock — a concurrent ``create_run`` (or any path that adds a
``DeviceReservation`` for a device in the pack) that commits *after*
the count read but *before* the disable write leaves the system with
a disabled pack and an active reservation referencing it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.devices.models import DeviceOperationalState, DeviceReservation
from app.packs.models import DriverPack, PackState
from app.packs.services.lifecycle import PackLifecycleService
from app.runs.models import RunState, TestRun
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_drain_disables_pack_with_fresh_concurrent_reservation(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    seeded_driver_packs: None,
) -> None:
    _ = seeded_driver_packs
    pack_id = "appium-uiautomator2"

    pack = await db_session.get(DriverPack, pack_id)
    assert pack is not None
    assert pack.state == PackState.enabled

    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="drain-race",
        pack_id=pack_id,
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    device_id = device.id
    await db_session.commit()

    original_count = PackLifecycleService.count_active_work_for_pack
    triggered = False

    async def _count_then_concurrent_reserve(
        self: PackLifecycleService, session: AsyncSession, target_pack_id: str
    ) -> dict[str, int]:
        nonlocal triggered
        # 1) Read counts as they stand right now (zero active work).
        counts = await original_count(self, session, target_pack_id)
        # ``try_complete_drain`` calls ``count_active_work_for_pack`` twice:
        # the first read decides whether to attempt the disable; the second
        # is the defensive recount immediately before the state write. On
        # the recount (``triggered`` already set), pass through without
        # injecting another reservation so the recount sees the one this
        # patch just committed below.
        if triggered:
            return counts
        triggered = True
        # 2) Simulate a concurrent ``create_run`` that commits a fresh
        #    reservation for a device in this pack *after* try_complete_drain
        #    has already observed "no active work" but *before* it flips
        #    state to disabled. The fixed try_complete_drain re-counts
        #    immediately before the state write — it will see the new
        #    reservation and bail.
        async with db_session_maker() as side:
            run = TestRun(
                name=f"drain-race-{uuid.uuid4().hex[:6]}",
                state=RunState.preparing,
                requirements=[{"pack_id": target_pack_id, "count": 1}],
                ttl_minutes=60,
                heartbeat_timeout_sec=120,
            )
            side.add(run)
            await side.flush()
            side.add(
                DeviceReservation(
                    run_id=run.id,
                    device_id=device_id,
                    identity_value=device.identity_value,
                    connection_target=device.connection_target,
                    pack_id=target_pack_id,
                    platform_id=device.platform_id,
                    os_version=device.os_version,
                )
            )
            await side.commit()
        return counts

    PackLifecycleService.count_active_work_for_pack = _count_then_concurrent_reserve  # type: ignore[assignment]
    try:
        await PackLifecycleService().transition_pack_state(db_session, pack_id, PackState.disabled)
    finally:
        PackLifecycleService.count_active_work_for_pack = original_count

    async with db_session_maker() as side:
        refreshed_pack: Any = await side.get(DriverPack, pack_id)
        assert refreshed_pack is not None
        active_reservation = (
            await side.execute(
                select(DeviceReservation).where(
                    DeviceReservation.pack_id == pack_id,
                    DeviceReservation.released_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    # Fixed behavior: the defensive recount in ``try_complete_drain`` sees
    # the reservation committed mid-flight and bails on the state flip, so
    # the pack stays in ``draining`` (the stable state for an interrupted
    # drain completion). Pre-fix behavior: pack ends up ``disabled``
    # despite a live reservation referencing it.
    assert active_reservation is not None
    assert refreshed_pack.state == PackState.draining, (
        f"Pack should remain ``draining`` after a reservation committed between "
        f"the initial count and the recount; got state={refreshed_pack.state}"
    )
