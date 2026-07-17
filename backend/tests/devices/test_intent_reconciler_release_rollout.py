from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceIntent
from app.devices.services import intent_reconciler
from app.devices.services.decision import DecisionFacts
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration, release_rollout_intent_source
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _rollout_intent(device_id: uuid.UUID, *, target_release: str) -> IntentRegistration:
    return IntentRegistration(
        source=release_rollout_intent_source(device_id),
        kind=CommandKind.release_rollout,
        payload={"target_release": target_release},
        expires_at=now_utc() + timedelta(minutes=15),
    )


async def _running_device(db: AsyncSession, host: Host, *, name: str) -> tuple[Device, AppiumNode]:
    device = await create_device(db, host_id=host.id, name=name)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db.add(node)
    await db.commit()
    return device, node


async def test_rollout_drains_while_session_live(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_device(db_session, db_host, name="rollout-live")
    db_session.add(Session(session_id="rollout-live", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )

    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.restart_requested_at is None
    assert row.payload.get("restart_requested_at") is None


async def test_rollout_stamps_watermark_once_when_idle(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_device(db_session, db_host, name="rollout-idle")

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )

    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    first = node.restart_requested_at
    assert node.accepting_new_sessions is False
    assert first is not None
    assert row.payload["restart_requested_at"] == first.isoformat()

    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)

    assert node.restart_requested_at == first
    assert row.payload["restart_requested_at"] == first.isoformat()


async def test_rollout_stamp_sequence_uses_live_session_state_once(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        desired_grid_run_id=None,
        accepting_new_sessions=False,
        stop_pending=False,
        restart_requested_at=None,
    )
    device = Device(id=device_id)
    device.appium_node = node
    row = DeviceIntent(
        device_id=device_id,
        source=release_rollout_intent_source(device_id),
        kind=CommandKind.release_rollout.value,
        payload={"target_release": "B"},
        expires_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [row]
    db = AsyncMock()
    db.execute.return_value = result
    times = iter(
        (
            datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 12, 1, tzinfo=UTC),
            datetime(2026, 7, 17, 12, 2, tzinfo=UTC),
        )
    )
    session_check = AsyncMock(side_effect=(True, False))
    facts = DecisionFacts(
        in_maintenance=False,
        device_checks_unhealthy=False,
        in_service=True,
        reservation_run_id=None,
        cooldown_active=False,
        cooldown_reason=None,
    )
    monkeypatch.setattr(intent_reconciler, "now_utc", lambda: next(times))
    monkeypatch.setattr(intent_reconciler, "device_has_live_session", session_check)
    monkeypatch.setattr(intent_reconciler, "gather_decision_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(intent_reconciler, "emit_operational_state_transition", AsyncMock())
    monkeypatch.setattr("app.appium_nodes.services.desired_state_writer.record_event", AsyncMock())

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())
    assert row.payload.get("restart_requested_at") is None
    assert node.restart_requested_at is None

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())
    stamp = datetime(2026, 7, 17, 12, 1, tzinfo=UTC)
    assert row.payload["restart_requested_at"] == stamp.isoformat()
    assert node.restart_requested_at == stamp

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())
    assert row.payload["restart_requested_at"] == stamp.isoformat()
    assert node.restart_requested_at == stamp
    assert session_check.await_count == 2
