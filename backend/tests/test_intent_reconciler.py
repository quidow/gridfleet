from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_intent import DeviceIntent
from app.models.device_intent_dirty import DeviceIntentDirty
from app.models.device_reservation import DeviceReservation
from app.services.intent_reconciler import _reconcile_device, _reconcile_dirty_devices, _reconcile_expired_intents
from app.services.intent_service import IntentService
from app.services.intent_types import GRID_ROUTING, NODE_PROCESS, RECOVERY, RESERVATION, IntentRegistration
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


async def _seed_node(db_session: AsyncSession, device_id: object, *, generation: int = 0) -> AppiumNode:
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.stopped,
        generation=generation,
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def test_baseline_eligible_device_derives_running(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="baseline")
    await _seed_node(db_session, device.id)

    await _reconcile_device(db_session, device.id)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is True
    assert node.generation == 1


async def test_cooldown_intents_derive_metadata_reservation_and_recovery(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="cooldown")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="cooldown run", devices=[device])
    service = IntentService(db_session)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await service.register_intents(
        device_id=device.id,
        reason="cooldown",
        intents=[
            IntentRegistration(
                source=f"cooldown:node:{run.id}",
                axis=NODE_PROCESS,
                run_id=run.id,
                expires_at=expires_at,
                payload={"action": "stop", "stop_mode": "defer", "priority": 70},
            ),
            IntentRegistration(
                source=f"cooldown:grid:{run.id}",
                axis=GRID_ROUTING,
                run_id=run.id,
                expires_at=expires_at,
                payload={"accepting_new_sessions": False, "priority": 70},
            ),
            IntentRegistration(
                source=f"cooldown:reservation:{run.id}",
                axis=RESERVATION,
                run_id=run.id,
                expires_at=expires_at,
                payload={
                    "excluded": True,
                    "priority": 70,
                    "exclusion_reason": "Device in cooldown",
                    "cooldown_count": 2,
                },
            ),
            IntentRegistration(
                source=f"cooldown:recovery:{run.id}",
                axis=RECOVERY,
                run_id=run.id,
                expires_at=expires_at,
                payload={"allowed": False, "priority": 70, "reason": "Device in cooldown"},
            ),
        ],
    )
    await db_session.commit()

    await _reconcile_device(db_session, device.id)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    await db_session.refresh(device)
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True
    assert node.desired_grid_run_id == run.id
    assert reservation.excluded is True
    assert reservation.exclusion_reason == "Device in cooldown"
    assert reservation.cooldown_count == 2
    assert device.recovery_allowed is False
    assert device.recovery_blocked_reason == "Device in cooldown"


async def test_expired_intents_are_deleted_and_reconciled(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired")
    await _seed_node(db_session, device.id)
    service = IntentService(db_session)
    await service.register_intent(
        device_id=device.id,
        source="expired",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": False, "priority": 90},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        reason="expired",
    )
    await db_session.commit()

    await _reconcile_expired_intents(db_session)

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert intents == []
    assert node.accepting_new_sessions is True


async def test_metadata_only_running_change_stages_outbox(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="metadata")
    node = await _seed_node(db_session, device.id, generation=7)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intent(
        device_id=device.id,
        source="grid:block",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": False, "priority": 80},
        reason="block",
    )
    await db_session.commit()

    await _reconcile_device(db_session, device.id)
    await db_session.commit()

    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert outbox.reconciled_generation == 8
    assert outbox.accepting_new_sessions is False


async def test_dirty_generation_not_deleted_when_incremented_during_reconcile(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dirty")
    await _seed_node(db_session, device.id)
    dirty = DeviceIntentDirty(device_id=device.id, generation=1, reason="initial")
    db_session.add(dirty)
    await db_session.commit()

    async def fake_reconcile(db: AsyncSession, device_id: object) -> None:
        row = await db.get(DeviceIntentDirty, device_id)
        assert row is not None
        row.generation += 1
        await db.flush()

    monkeypatch.setattr("app.services.intent_reconciler._reconcile_device", fake_reconcile)

    await _reconcile_dirty_devices(db_session, limit=10)
    await db_session.commit()

    assert await db_session.get(DeviceIntentDirty, device.id) is not None
