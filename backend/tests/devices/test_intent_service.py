from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumNode
from app.devices.models import Device, DeviceIntent, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def test_appium_node_has_orchestration_columns() -> None:
    columns = AppiumNode.__table__.columns

    assert "accepting_new_sessions" in columns
    assert "stop_pending" in columns
    assert "restart_requested_at" in columns


async def test_register_intents_batches(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-batch")
    service = IntentService(db_session)

    registered = await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:node:{device.id}",
                kind=CommandKind.operator_stop,
                payload={"action": "stop", "priority": 70},
            ),
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "priority": 70},
            ),
        ],
    )
    await db_session.commit()

    intents = (
        (
            await db_session.execute(
                select(DeviceIntent).where(DeviceIntent.device_id == device.id).order_by(DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    assert [intent.source for intent in registered] == [
        f"operator:stop:node:{device.id}",
        f"operator:stop:recovery:{device.id}",
    ]
    assert [intent.source for intent in intents] == [
        f"operator:stop:node:{device.id}",
        f"operator:stop:recovery:{device.id}",
    ]


async def test_register_intents_rejects_duplicate_sources_before_upsert(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-duplicate-source")
    service = IntentService(db_session)

    with pytest.raises(ValueError, match="Duplicate intent source"):
        await service.register_intents(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=f"operator:stop:node:{device.id}",
                    kind=CommandKind.operator_stop,
                    payload={"action": "stop", "priority": 70},
                ),
                IntentRegistration(
                    source=f"operator:stop:node:{device.id}",
                    kind=CommandKind.operator_stop,
                    payload={"action": "start", "priority": 70},
                ),
            ],
        )

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert intents == []


async def test_revoke_intent_deletes(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-revoke")
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                kind=CommandKind.operator_start,
                payload={"action": "stop", "priority": 50},
            ),
        ],
    )
    await db_session.commit()

    source = f"operator:start:{device.id}"
    revoked = await service.revoke_intent(device_id=device.id, source=source)
    missing = await service.revoke_intent(device_id=device.id, source=source)
    await db_session.commit()

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert revoked is True
    assert missing is False
    assert intents == []


async def test_reconcile_now_derives_state_inline(db_session: AsyncSession, db_host: Host) -> None:
    """reconcile_now = lock + flush + inline reconcile; read-your-writes for
    operator/observation paths, no queue row involved."""
    device = await create_device(db_session, host_id=db_host.id, name="reconcile-now")
    device.device_checks_healthy = False  # fact write pending in the session
    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)
    await db_session.commit()
    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None
    assert refreshed.operational_state_last_emitted == DeviceOperationalState.offline


async def test_register_intents_empty_batch_is_noop(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-empty")
    service = IntentService(db_session)

    assert await service.register_intents(device_id=device.id, intents=[]) == []
    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert intents == []


async def test_reconcile_now_flushes_before_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """reconcile_now must flush before locking; the other two reconcile helpers
    must not. Pins the one asymmetry the shared helper has to preserve."""
    device_id = uuid.uuid4()
    call_log: list[str] = []

    fake_db = AsyncMock()
    fake_db.flush.side_effect = lambda: call_log.append("flush")

    async def fake_lock_device(db: object, did: object) -> None:
        call_log.append("lock")

    async def fake_reconcile(db: object, did: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr("app.devices.services.intent.device_locking.lock_device", fake_lock_device)
    monkeypatch.setattr("app.devices.services.intent.reconcile_device", fake_reconcile)

    service = IntentService(fake_db)

    publisher = AsyncMock()

    # reconcile_now: flush must precede lock
    call_log.clear()
    await service.reconcile_now(device_id, publisher=publisher)
    assert call_log == ["flush", "lock"], f"expected flush then lock, got {call_log}"

    # register_intents_and_reconcile: no flush at all
    call_log.clear()
    monkeypatch.setattr(service, "register_intents", AsyncMock(return_value=[]))
    await service.register_intents_and_reconcile(device_id=device_id, intents=[], publisher=publisher)
    assert "flush" not in call_log, f"unexpected flush in register path: {call_log}"
    assert "lock" in call_log

    # revoke_intents_and_reconcile: no flush at all
    call_log.clear()
    monkeypatch.setattr(service, "revoke_intents", AsyncMock(return_value=0))
    await service.revoke_intents_and_reconcile(device_id=device_id, sources=[], publisher=publisher)
    assert "flush" not in call_log, f"unexpected flush in revoke path: {call_log}"
    assert "lock" in call_log
