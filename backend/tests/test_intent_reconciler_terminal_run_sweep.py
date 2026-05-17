from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import _reconcile_terminal_run_intents
from app.devices.services.intent_types import RESERVATION, IntentRegistration
from app.runs.models import RunState
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_node(db_session: AsyncSession, device_id: object) -> AppiumNode:
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def _register_run_scoped_reservation_intent(
    db_session: AsyncSession, *, device_id: object, run_id: object
) -> None:
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device_id,
        reason="seed",
        intents=[
            IntentRegistration(
                source=f"health_failure:reservation:{device_id}",
                axis=RESERVATION,
                run_id=run_id,
                payload={
                    "excluded": True,
                    "priority": 60,
                    "exclusion_reason": "irrelevant",
                },
            )
        ],
    )
    await db_session.commit()


async def test_sweep_deletes_intent_bound_to_terminal_run(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="sweep-terminal")
    await _seed_node(db_session, device.id)
    terminal_run = await create_reserved_run(
        db_session,
        name="terminal-run",
        devices=[device],
        state=RunState.expired,
        mark_released=True,
    )
    await _register_run_scoped_reservation_intent(db_session, device_id=device.id, run_id=terminal_run.id)

    await _reconcile_terminal_run_intents(db_session)
    await db_session.commit()

    remaining = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"health_failure:reservation:{device.id}",
            )
        )
    ).scalar_one_or_none()
    assert remaining is None


async def test_sweep_keeps_intent_bound_to_active_run(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="sweep-active")
    await _seed_node(db_session, device.id)
    active_run = await create_reserved_run(
        db_session,
        name="active-run",
        devices=[device],
        state=RunState.active,
    )
    await _register_run_scoped_reservation_intent(db_session, device_id=device.id, run_id=active_run.id)

    await _reconcile_terminal_run_intents(db_session)
    await db_session.commit()

    remaining = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"health_failure:reservation:{device.id}",
            )
        )
    ).scalar_one_or_none()
    assert remaining is not None


async def test_sweep_keeps_intent_with_null_run_id(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="sweep-null")
    await _seed_node(db_session, device.id)
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        reason="seed unbound",
        intents=[
            IntentRegistration(
                source=f"active_session:{device.id}",
                axis=RESERVATION,
                run_id=None,
                payload={"excluded": False, "priority": 30},
            )
        ],
    )
    await db_session.commit()

    await _reconcile_terminal_run_intents(db_session)
    await db_session.commit()

    remaining = (
        await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))
    ).scalar_one_or_none()
    assert remaining is not None
