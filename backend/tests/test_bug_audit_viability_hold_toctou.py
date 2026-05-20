"""Bug 3: Session viability probe does not re-check ``hold`` after the FOR UPDATE.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-3``.

``run_session_viability_probe`` reads ``device.operational_state`` and
``device.hold`` at ``service_viability.py:367-369`` *without* a row
lock, then re-locks at line 397 but only reads
``operational_state`` (line 398) before firing
``SESSION_STARTED``. A concurrent ``hold = maintenance`` flip between
the unlocked check and the FOR UPDATE is silently ignored — the probe
transitions the device to ``busy`` despite the maintenance hold.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.sessions import service_viability
from app.sessions.service_viability import run_session_viability_probe
from app.sessions.viability_types import SessionViabilityCheckedBy
from tests.helpers import create_device, create_host

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
        hold=None,
        verified=True,
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://localhost:4444",
            pid=12345,
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

    transitions_fired: list[TransitionEvent] = []
    original_transition = service_viability._MACHINE.transition

    async def _spy_transition(target_device: Device, event: TransitionEvent, **kwargs: object) -> bool:
        transitions_fired.append(event)
        result: bool = await original_transition(target_device, event, **kwargs)  # type: ignore[arg-type]
        return result

    original_lock = device_locking.lock_device

    async def _flip_hold_then_lock(db: object, did: uuid.UUID, **kwargs: object) -> Device:
        # Race: concurrent maintenance-enter commits hold=maintenance after the
        # unlocked pre-flight gate at viability.py:367 but before the FOR UPDATE
        # acquired here.
        if did == device_id:
            async with db_session_maker() as side:
                row = await side.get(Device, did)
                assert row is not None
                with state_write_guard.bypass():
                    row.hold = DeviceHold.maintenance
                await side.commit()
        locked: Device = await original_lock(db, did, **kwargs)  # type: ignore[arg-type]
        return locked

    probe_mock = AsyncMock(return_value=(True, None))

    with (
        patch.object(service_viability._MACHINE, "transition", side_effect=_spy_transition),
        patch.object(device_locking, "lock_device", side_effect=_flip_hold_then_lock),
        patch.object(service_viability, "probe_session_via_grid", probe_mock),
    ):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=SessionViabilityCheckedBy.manual,
        )

    # Fixed behavior: the probe re-checks hold under the lock and bails before
    # transitioning to busy. Current behavior (bug): SESSION_STARTED fires
    # despite the maintenance hold.
    assert TransitionEvent.SESSION_STARTED not in transitions_fired, (
        "Probe transitioned device to busy even though hold flipped to maintenance "
        "between the unlocked pre-flight check and the FOR UPDATE re-lock"
    )
