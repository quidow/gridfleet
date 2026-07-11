from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState
from app.devices.models import DeviceEvent, DeviceEventType, DeviceReservation, ExclusionKind
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_device
from tests.appium_nodes.test_intent_reconciler import _seed_node
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_reconciler_records_metadata_events(db_session: AsyncSession, db_host: Host) -> None:
    """A cooldown fact flips accepting_new_sessions; the reconciler emits a
    desired_state_changed event with the decider reason. Recovery is no longer a
    reconciler-owned field, so no recovery_allowed event is emitted (the badge is
    projected at read time)."""
    device = await create_device(db_session, host_id=db_host.id, name="events")
    node = await _seed_node(db_session, device.id)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    await create_reserved_run(db_session, name="events run", devices=[device])
    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    reservation.excluded = True
    reservation.exclusion_kind = ExclusionKind.cooldown
    reservation.exclusion_reason = "blocked by test"
    reservation.excluded_at = datetime.now(UTC)
    reservation.excluded_until = datetime.now(UTC) + timedelta(minutes=5)
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
        "reason": "reservation cooldown",
    } in details
    # No recovery_allowed field event: the recovery axis left the reconciler.
    assert not any(detail.get("field") == "recovery_allowed" for detail in details)


async def test_operator_stop_records_no_auto_stopped_device_event(db_session: AsyncSession, db_host: Host) -> None:
    """Operator stops no longer write an auto_stopped DeviceEvent — the stop is already
    visible as desired_state_changed rows from the reconciler (plan 4b behavior change #2)."""
    from app.devices.models import DeviceOperationalState
    from app.lifecycle.services.operator_node import operator_stop_intents

    device = await create_device(db_session, host_id=db_host.id, name="op-stop-events")
    device.operational_state_last_emitted = DeviceOperationalState.available
    await _seed_node(db_session, device.id)
    await db_session.commit()

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=operator_stop_intents(device.id),
        publisher=event_bus,
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
    assert events == []
