from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState
from app.devices.models import DeviceEvent, DeviceEventType
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.intent_types import GRID_ROUTING, RECOVERY, IntentRegistration
from tests.appium_nodes.test_intent_reconciler import _seed_node
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_reconciler_records_metadata_events(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="events")
    node = await _seed_node(db_session, device.id)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="grid:block",
                axis=GRID_ROUTING,
                payload={"accepting_new_sessions": False, "priority": 80},
            ),
        ],
    )
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="recovery:block",
                axis=RECOVERY,
                payload={"allowed": False, "priority": 80, "reason": "blocked by test"},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
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


def test_operator_stopped_maps_to_auto_stopped_event() -> None:
    from app.devices.models import DeviceOperationalState
    from app.devices.services.observation_reason import ObservationReason, map_transition_event

    event_type, severity = map_transition_event(DeviceOperationalState.offline, ObservationReason.operator_stopped)
    assert event_type is DeviceEventType.auto_stopped
    assert severity == "info"


async def test_operator_stop_records_auto_stopped_device_event(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.models import DeviceOperationalState
    from app.devices.services.observation_reason import ObservationReason
    from app.lifecycle.services.operator_node import operator_stop_intents

    device = await create_device(db_session, host_id=db_host.id, name="op-stop-events")
    device.operational_state = DeviceOperationalState.available
    await _seed_node(db_session, device.id)
    await db_session.commit()

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=operator_stop_intents(device.id),
        publisher=event_bus,
        observed_reason=ObservationReason.operator_stopped,
    )
    await db_session.commit()

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.auto_stopped,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].details["reason"] == "operator_stopped"
