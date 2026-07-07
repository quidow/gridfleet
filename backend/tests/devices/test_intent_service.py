from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumNode
from app.devices.models import Device, DeviceIntent, DeviceIntentDirty
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS, IntentRegistration
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def test_appium_node_has_orchestration_columns() -> None:
    columns = AppiumNode.__table__.columns

    assert "accepting_new_sessions" in columns
    assert "stop_pending" in columns
    assert "generation" in columns


def test_device_has_recovery_decision_columns() -> None:
    columns = Device.__table__.columns

    assert "recovery_allowed" in columns
    assert "recovery_blocked_reason" in columns


async def test_register_intents_batches_dirty_mark(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-batch")
    service = IntentService(db_session)

    registered = await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="batch:node",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": 70},
            ),
            IntentRegistration(
                source="batch:grid",
                axis=GRID_ROUTING,
                payload={"accepting_new_sessions": False, "priority": 70},
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
    dirty = await db_session.get(DeviceIntentDirty, device.id)
    assert [intent.source for intent in registered] == ["batch:node", "batch:grid"]
    assert [intent.source for intent in intents] == ["batch:grid", "batch:node"]
    assert dirty is not None
    assert dirty.generation == 1


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
                    source="batch:node",
                    axis=NODE_PROCESS,
                    payload={"action": "stop", "priority": 70},
                ),
                IntentRegistration(
                    source="batch:node",
                    axis=NODE_PROCESS,
                    payload={"action": "start", "priority": 70},
                ),
            ],
        )

    dirty = await db_session.get(DeviceIntentDirty, device.id)
    assert dirty is None


async def test_revoke_intent_deletes_and_marks_dirty(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-revoke")
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="connectivity",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": 50},
            ),
        ],
    )
    await db_session.commit()

    revoked = await service.revoke_intent(device_id=device.id, source="connectivity")
    missing = await service.revoke_intent(device_id=device.id, source="connectivity")
    await db_session.commit()

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    dirty = await db_session.get(DeviceIntentDirty, device.id)
    assert revoked is True
    assert missing is False
    assert intents == []
    assert dirty is not None
    assert dirty.generation == 2


async def test_mark_dirty_returns_written_generation(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-dirty")
    service = IntentService(db_session)

    first = await service.mark_dirty(device.id)
    second = await service.mark_dirty(device.id)
    await db_session.commit()

    dirty = await db_session.get(DeviceIntentDirty, device.id)
    assert first == 1
    assert second == 2
    assert dirty is not None
    assert dirty.generation == 2


async def test_register_intents_empty_batch_is_noop(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-empty")
    service = IntentService(db_session)

    assert await service.register_intents(device_id=device.id, intents=[]) == []
    assert await db_session.get(DeviceIntentDirty, device.id) is None


async def test_mark_dirty_and_reconcile_flushes_before_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """mark_dirty_and_reconcile must flush before locking; the other two reconcile
    helpers must not. Pins the one asymmetry the dedup helper has to preserve."""
    device_id = uuid.uuid4()
    call_log: list[str] = []

    fake_db = AsyncMock()
    fake_db.flush.side_effect = lambda: call_log.append("flush")

    async def fake_lock_device(db: object, did: object) -> None:
        call_log.append("lock")

    async def fake_reconcile(db: object, did: object, **kwargs: object) -> None:
        pass

    async def fake_mark_dirty(did: object) -> int:
        return 1

    monkeypatch.setattr("app.devices.services.intent.device_locking.lock_device", fake_lock_device)
    monkeypatch.setattr("app.devices.services.intent.reconcile_device", fake_reconcile)

    service = IntentService(fake_db)
    monkeypatch.setattr(service, "mark_dirty", fake_mark_dirty)

    publisher = AsyncMock()

    # mark_dirty_and_reconcile: flush must precede lock
    call_log.clear()
    await service.mark_dirty_and_reconcile(device_id, publisher=publisher)
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
