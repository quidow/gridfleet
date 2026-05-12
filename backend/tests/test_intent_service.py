from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.appium_node import AppiumNode
from app.models.device import Device
from app.models.device_intent import DeviceIntent
from app.models.device_intent_dirty import DeviceIntentDirty
from app.models.test_run import TestRun
from app.services.intent_service import IntentService
from app.services.intent_types import GRID_ROUTING, NODE_PROCESS, IntentRegistration
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


def test_appium_node_has_orchestration_columns() -> None:
    columns = AppiumNode.__table__.columns

    assert "accepting_new_sessions" in columns
    assert "stop_pending" in columns
    assert "generation" in columns


def test_device_has_recovery_decision_columns() -> None:
    columns = Device.__table__.columns

    assert "recovery_allowed" in columns
    assert "recovery_blocked_reason" in columns


async def test_register_intent_upserts_by_device_and_source(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-upsert")
    service = IntentService(db_session)

    first = await service.register_intent(
        device_id=device.id,
        source="operator:stop",
        axis=NODE_PROCESS,
        payload={"action": "stop", "priority": 100},
        reason="first",
    )
    second = await service.register_intent(
        device_id=device.id,
        source="operator:stop",
        axis=NODE_PROCESS,
        payload={"action": "start", "priority": 100},
        reason="second",
    )
    await db_session.commit()

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    dirty = await db_session.get(DeviceIntentDirty, device.id)

    assert second.id == first.id
    assert len(intents) == 1
    assert intents[0].payload == {"action": "start", "priority": 100}
    assert dirty is not None
    assert dirty.generation == 2
    assert dirty.reason == "second"


async def test_register_intent_stores_run_id_as_column(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-run-id")
    run = TestRun(name="intent run", requirements=[{"platform_id": device.platform_id, "count": 1}])
    db_session.add(run)
    await db_session.flush()
    service = IntentService(db_session)

    intent = await service.register_intent(
        device_id=device.id,
        source=f"run:{run.id}",
        axis=GRID_ROUTING,
        run_id=run.id,
        payload={"accepting_new_sessions": True, "priority": 40},
        reason="run route",
    )
    await db_session.commit()

    stored = await db_session.get(DeviceIntent, intent.id)
    assert stored is not None
    assert stored.run_id == run.id
    assert "run_id" not in stored.payload


async def test_register_intents_batches_dirty_mark(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-batch")
    service = IntentService(db_session)

    registered = await service.register_intents(
        device_id=device.id,
        reason="batch",
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
    assert dirty.reason == "batch"


async def test_revoke_intent_deletes_and_marks_dirty(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-revoke")
    service = IntentService(db_session)
    await service.register_intent(
        device_id=device.id,
        source="connectivity",
        axis=NODE_PROCESS,
        payload={"action": "stop", "priority": 50},
        reason="lost",
    )
    await db_session.commit()

    revoked = await service.revoke_intent(device_id=device.id, source="connectivity", reason="restored")
    missing = await service.revoke_intent(device_id=device.id, source="connectivity", reason="restored again")
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
    assert dirty.reason == "restored"


async def test_mark_dirty_returns_written_generation(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-dirty")
    service = IntentService(db_session)

    first = await service.mark_dirty(device.id, reason="first")
    second = await service.mark_dirty(device.id, reason="second")
    await db_session.commit()

    dirty = await db_session.get(DeviceIntentDirty, device.id)
    assert first == 1
    assert second == 2
    assert dirty is not None
    assert dirty.generation == 2
    assert dirty.reason == "second"


async def test_get_intents_by_axis_filters_device_and_axis(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-filter")
    other_device = await create_device(db_session, host_id=db_host.id, name="intent-filter-other")
    service = IntentService(db_session)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)

    expected = await service.register_intent(
        device_id=device.id,
        source="run:one",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": True, "priority": 40},
        expires_at=expires_at,
        reason="run",
    )
    await service.register_intent(
        device_id=device.id,
        source="operator:stop",
        axis=NODE_PROCESS,
        payload={"action": "stop", "priority": 100},
        reason="stop",
    )
    await service.register_intent(
        device_id=other_device.id,
        source="run:other",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": True, "priority": 40},
        reason="other run",
    )
    await db_session.commit()

    intents = await service.get_intents_by_axis(device.id, GRID_ROUTING)
    assert [intent.id for intent in intents] == [expected.id]
