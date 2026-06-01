from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceEvent, DeviceEventType, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_hooks import EventLogHook
from app.devices.services.lifecycle_state_machine_types import DeviceStateModel, TransitionEvent
from app.hosts.models import Host
from tests.helpers import test_event_bus as event_bus

pytestmark = [pytest.mark.db]


async def _seed(db_session: AsyncSession, db_host: Host, suffix: str) -> Device:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"hook-{suffix}",
            connection_target=f"hook-{suffix}",
            name=f"Hook Device {suffix}",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            hold=None,
            device_type="real_device",
            connection_type="usb",
        )
    db_session.add(device)
    await db_session.flush()
    return device


class _RecordingHook:
    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        self._log.append(f"{self._name}:{event.value}:{before.label()}->{after.label()}")


class TestHookOrdering:
    async def test_hooks_execute_in_registration_order(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed(db_session, db_host, "ord1")
        log: list[str] = []
        machine = DeviceStateMachine(hooks=[_RecordingHook("A", log), _RecordingHook("B", log)])
        await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=Mock())
        assert log == [
            "A:session_started:available->busy",
            "B:session_started:available->busy",
        ]

    async def test_hooks_skipped_on_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed(db_session, db_host, "ord2")
        with state_write_guard.bypass():
            device.operational_state = DeviceOperationalState.busy
        await db_session.flush()
        log: list[str] = []
        machine = DeviceStateMachine(hooks=[_RecordingHook("A", log)])
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=event_bus)
        assert changed is False
        assert log == []


class TestEventLogHook:
    async def test_session_started_records_event(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed(db_session, db_host, "evt1")
        machine = DeviceStateMachine(hooks=[EventLogHook()])
        await machine.transition(device, TransitionEvent.SESSION_STARTED, reason="run start", publisher=Mock())
        await db_session.flush()

        rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
        assert any(row.event_type == DeviceEventType.session_started for row in rows)
        session_row = next(row for row in rows if row.event_type == DeviceEventType.session_started)
        assert session_row.details == {"from": "available", "to": "busy"}

    async def test_idempotent_transition_writes_no_event(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed(db_session, db_host, "evt2")
        with state_write_guard.bypass():
            device.operational_state = DeviceOperationalState.busy
        await db_session.flush()
        machine = DeviceStateMachine(hooks=[EventLogHook()])
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=event_bus)
        assert changed is False
        await db_session.flush()
        rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
        assert all(row.event_type != DeviceEventType.session_started for row in rows)

    async def test_unmapped_event_writes_nothing(self, db_session: AsyncSession, db_host: Host) -> None:
        # VERIFICATION_PASSED is not in _EVENT_TYPE_MAP, so no DeviceEvent row is written.
        device = await _seed(db_session, db_host, "evt3")
        with state_write_guard.bypass():
            device.operational_state = DeviceOperationalState.verifying
        await db_session.flush()
        machine = DeviceStateMachine(hooks=[EventLogHook()])
        await machine.transition(device, TransitionEvent.VERIFICATION_PASSED, publisher=event_bus)
        await db_session.flush()
        rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
        assert rows == []
