"""Bug 3: Session viability probe re-checks reservation state after the FOR UPDATE.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-3``.

``run_session_viability_probe`` reads ``device.operational_state`` and the
device's reservation state *without* a row lock, then re-locks and re-checks
both before proceeding. A concurrent reservation created between the unlocked
check and the FOR UPDATE must not be silently ignored — the post-lock re-check
raises ``ValueError`` rather than probing a now-reserved device.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.capability import DeviceCapabilityService
from app.sessions.service_viability import SessionViabilityService
from app.sessions.viability_types import SessionViabilityCheckedBy
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_host, create_reservation
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_viability_probe_runs_on_maintenance_held_device(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    seeded_driver_packs: None,
) -> None:
    _ = seeded_driver_packs
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="viability-toctou",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            pid=12345,
            active_connection_target=device.connection_target,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            health_state="up",
            health_running=True,
        )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    # Eager-load appium_node so the probe's ``device.appium_node`` read
    # at viability.py:383 does not trigger lazy IO in the async context.
    device = await device_locking.lock_device(db_session, device.id)

    original_lock = device_locking.lock_device

    async def _reserve_then_lock(db: object, did: uuid.UUID, **kwargs: object) -> Device:
        # Race: a concurrent allocation commits an active reservation after the
        # unlocked pre-flight gate but before the FOR UPDATE acquired here.
        if did == device_id:
            async with db_session_maker() as side:
                await create_reservation(side, device_id=did)
                await side.commit()
        locked: Device = await original_lock(db, did, **kwargs)  # type: ignore[arg-type]
        return locked

    probe_mock = AsyncMock(return_value=(True, None))
    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )

    # After Task 10: the probe no longer fires SESSION_STARTED via _MACHINE.
    # It only re-checks reservation state under the lock and raises ValueError if changed.
    with (
        patch.object(device_locking, "lock_device", side_effect=_reserve_then_lock),
        patch.object(svc, "probe_session_direct", probe_mock),
        pytest.raises(ValueError, match="state changed concurrently"),
    ):
        await svc.run_session_viability_probe(
            db_session,
            device,
            checked_by=SessionViabilityCheckedBy.manual,
        )
    # Fixed behavior: the probe re-checks reservation state under the lock and
    # raises ValueError before any state transition. No SESSION_STARTED is fired
    # because Task 10 removed the _MACHINE.transition call entirely.
