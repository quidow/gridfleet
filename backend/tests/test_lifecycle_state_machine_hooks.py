import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceOperationalState
from app.models.host import Host
from app.services.lifecycle_state_machine import DeviceStateMachine
from app.services.lifecycle_state_machine_types import DeviceStateModel, TransitionEvent

pytestmark = [pytest.mark.db]


async def _seed(db_session: AsyncSession, db_host: Host, suffix: str) -> Device:
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
        await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert log == [
            "A:session_started:available/None->busy/None",
            "B:session_started:available/None->busy/None",
        ]

    async def test_hooks_skipped_on_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed(db_session, db_host, "ord2")
        device.operational_state = DeviceOperationalState.busy
        await db_session.flush()
        log: list[str] = []
        machine = DeviceStateMachine(hooks=[_RecordingHook("A", log)])
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert changed is False
        assert log == []
