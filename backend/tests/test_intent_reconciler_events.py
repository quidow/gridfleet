from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.appium_node import AppiumDesiredState
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.intent_reconciler import _reconcile_device
from app.services.intent_service import IntentService
from app.services.intent_types import GRID_ROUTING, RECOVERY
from tests.helpers import create_device
from tests.test_intent_reconciler import _seed_node

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


async def test_reconciler_records_metadata_events(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="events")
    node = await _seed_node(db_session, device.id)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intent(
        device_id=device.id,
        source="grid:block",
        axis=GRID_ROUTING,
        payload={"accepting_new_sessions": False, "priority": 80},
        reason="block sessions",
    )
    await service.register_intent(
        device_id=device.id,
        source="recovery:block",
        axis=RECOVERY,
        payload={"allowed": False, "priority": 80, "reason": "blocked by test"},
        reason="block recovery",
    )
    await db_session.commit()

    await _reconcile_device(db_session, device.id)
    await db_session.commit()

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.desired_state_changed,
                )
            )
        )
        .scalars()
        .all()
    )
    details = [event.details for event in events]
    assert {
        "field": "accepting_new_sessions",
        "old_value": True,
        "new_value": False,
        "caller": "intent_reconciler",
        "reason": "grid:block intent (priority 80)",
    } in details
    assert {
        "field": "recovery_allowed",
        "old_value": True,
        "new_value": False,
        "caller": "intent_reconciler",
        "reason": "blocked by test",
    } in details
