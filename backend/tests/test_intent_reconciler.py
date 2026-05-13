from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.errors import AgentUnreachableError
from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_intent import DeviceIntent
from app.models.device_intent_dirty import DeviceIntentDirty
from app.models.device_reservation import DeviceReservation
from app.services.control_plane_leader import LeadershipLost
from app.services.intent_reconciler import (
    _reconcile_all_devices_once,
    _reconcile_device,
    _reconcile_dirty_devices,
    _reconcile_expired_intents,
    _stage_agent_reconfigure,
    run_device_intent_reconciler_once,
)
from app.services.intent_service import IntentService
from app.services.intent_types import GRID_ROUTING, NODE_PROCESS, RECOVERY, RESERVATION, IntentRegistration
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
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


async def test_expired_running_metadata_change_is_delivered(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired-delivery")
    node = await _seed_node(db_session, device.id, generation=4)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    node.accepting_new_sessions = False
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intent(
        device_id=device.id,
        source="expired:grid:block",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": False, "priority": 90},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        reason="expired block",
    )
    await db_session.commit()
    reconfigure = AsyncMock()
    monkeypatch.setattr("app.services.agent_operations.agent_appium_reconfigure", reconfigure)

    await _reconcile_expired_intents(db_session)

    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
    )
    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert outbox.delivered_at is not None


async def test_pending_reconfigure_from_expired_last_intent_is_retried(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired-retry")
    node = await _seed_node(db_session, device.id, generation=4)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    node.accepting_new_sessions = False
    await db_session.commit()
    service = IntentService(db_session)
    intent = await service.register_intent(
        device_id=device.id,
        source="expired:grid:block",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": False, "priority": 90},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        reason="expired block",
    )
    await db_session.commit()
    await _reconcile_dirty_devices(db_session, limit=10)
    intent.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    reconfigure = AsyncMock(side_effect=[AgentUnreachableError(db_host.ip, "offline"), {"port": 4723}])
    monkeypatch.setattr("app.services.agent_operations.agent_appium_reconfigure", reconfigure)
    monkeypatch.setattr("app.services.intent_reconciler.assert_current_leader", AsyncMock())

    await _reconcile_expired_intents(db_session)

    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    dirty_rows = (await db_session.execute(select(DeviceIntentDirty))).scalars().all()
    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert outbox.delivered_at is None
    assert outbox.delivery_attempts == 1
    assert dirty_rows == []
    assert intents == []

    await run_device_intent_reconciler_once(db_session, cycle=1)

    await db_session.refresh(outbox)
    assert outbox.delivered_at is not None
    assert outbox.delivery_attempts == 1
    assert reconfigure.await_count == 2


async def test_graceful_stop_stages_agent_drain_before_convergence_can_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="graceful")
    node = await _seed_node(db_session, device.id, generation=2)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intent(
        device_id=device.id,
        source="maintenance:node",
        axis=NODE_PROCESS,
        payload={"action": "stop", "stop_mode": "graceful", "priority": 80},
        reason="maintenance",
    )
    await db_session.commit()

    await _reconcile_device(db_session, device.id)
    await db_session.commit()

    await db_session.refresh(node)
    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False
    assert outbox.port == 4723
    assert outbox.stop_pending is True
    assert outbox.accepting_new_sessions is False


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


async def test_stage_agent_reconfigure_dedupes_identical_undelivered_generation(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="stage-dedupe")
    node = await _seed_node(db_session, device.id, generation=3)
    node.port = 4723
    node.accepting_new_sessions = False
    node.stop_pending = True
    await db_session.commit()

    await _stage_agent_reconfigure(db_session, node)
    await _stage_agent_reconfigure(db_session, node)
    await db_session.commit()

    rows = (await db_session.execute(select(AgentReconfigureOutbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reconciled_generation == 3
    assert rows[0].accepting_new_sessions is False
    assert rows[0].stop_pending is True


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


async def test_full_scan_reconciles_each_intent_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await create_device(db_session, host_id=db_host.id, name="full-scan-a")
    second = await create_device(db_session, host_id=db_host.id, name="full-scan-b")
    db_session.add_all(
        [
            DeviceIntent(device_id=first.id, source="test-a", axis=NODE_PROCESS, payload={}),
            DeviceIntent(device_id=second.id, source="test-b", axis=NODE_PROCESS, payload={}),
        ]
    )
    await db_session.commit()
    reconciled: list[object] = []
    deliver = AsyncMock()

    async def fake_reconcile(_db: AsyncSession, device_id: object) -> None:
        reconciled.append(device_id)

    monkeypatch.setattr("app.services.intent_reconciler._reconcile_device", fake_reconcile)
    monkeypatch.setattr("app.services.intent_reconciler.deliver_agent_reconfigures", deliver)

    await _reconcile_all_devices_once(db_session)

    assert set(reconciled) == {first.id, second.id}
    assert deliver.await_count == 2


async def test_reconciler_cycle_checks_leadership_before_writes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconcile_expired = AsyncMock()
    monkeypatch.setattr("app.services.intent_reconciler._reconcile_expired_intents", reconcile_expired)
    monkeypatch.setattr(
        "app.services.intent_reconciler.assert_current_leader",
        AsyncMock(side_effect=LeadershipLost("lost")),
    )

    with pytest.raises(LeadershipLost):
        await run_device_intent_reconciler_once(db_session, cycle=1)

    reconcile_expired.assert_not_awaited()
