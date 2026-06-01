from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidTransitionError
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_types import DeviceStateModel, TransitionEvent
from app.hosts.models import Host
from tests.helpers import test_event_bus as event_bus

pytestmark = [pytest.mark.db]


async def _seed_device(
    db_session: AsyncSession,
    db_host: Host,
    *,
    operational: DeviceOperationalState,
    name_suffix: str,
) -> Device:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"sm-{name_suffix}",
            connection_target=f"sm-{name_suffix}",
            name=f"SM Device {name_suffix}",
            os_version="14",
            host_id=db_host.id,
            operational_state=operational,
            device_type="real_device",
            connection_type="usb",
        )
    db_session.add(device)
    await db_session.flush()
    return device


class TestValidTransitions:
    async def test_available_to_busy_on_session_started(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.available, name_suffix="t1")
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=Mock())
        assert changed
        assert device.operational_state == DeviceOperationalState.busy

    async def test_busy_to_available_on_session_ended(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.busy, name_suffix="t2")
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_ENDED, publisher=Mock())
        assert changed
        assert device.operational_state == DeviceOperationalState.available

    async def test_available_to_offline_on_connectivity_lost(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.available, name_suffix="t3")
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.CONNECTIVITY_LOST, publisher=Mock())
        assert device.operational_state == DeviceOperationalState.offline

    async def test_offline_to_available_on_connectivity_restored(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.offline, name_suffix="t4")
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.CONNECTIVITY_RESTORED, publisher=Mock())
        assert device.operational_state == DeviceOperationalState.available

    async def test_offline_to_busy_on_session_started(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.offline, name_suffix="t7")
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=Mock())
        assert changed
        assert device.operational_state == DeviceOperationalState.busy

    async def test_verification_started_from_offline_transitions_to_verifying(
        self, db_session: AsyncSession, db_host: Host
    ) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.offline,
            name_suffix="verify-start",
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.VERIFICATION_STARTED, publisher=Mock())
        assert changed is True
        assert device.operational_state is DeviceOperationalState.verifying

    async def test_verification_failed_returns_to_offline(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.verifying,
            name_suffix="verify-fail",
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.VERIFICATION_FAILED, publisher=Mock())
        assert changed is True
        assert device.operational_state is DeviceOperationalState.offline

    async def test_verification_passed_returns_to_available_baseline(
        self, db_session: AsyncSession, db_host: Host
    ) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.verifying,
            name_suffix="verify-pass",
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.VERIFICATION_PASSED, publisher=Mock())
        assert changed is True
        assert device.operational_state is DeviceOperationalState.available


class TestIdempotency:
    async def test_session_started_from_busy_is_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.busy, name_suffix="i2")
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=event_bus)
        assert changed is False

    async def test_auto_stop_executed_from_offline_is_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.offline, name_suffix="i4")
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.AUTO_STOP_EXECUTED, publisher=event_bus)
        assert changed is False
        assert device.operational_state == DeviceOperationalState.offline


class TestInvalidTransitions:
    async def test_maintenance_exited_is_invalid_on_operational_axis(
        self, db_session: AsyncSession, db_host: Host
    ) -> None:
        # Maintenance is derived onto operational_state by the reconciler; the
        # state machine no longer handles the maintenance transition events.
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.available, name_suffix="x2")
        machine = DeviceStateMachine()
        with pytest.raises(InvalidTransitionError):
            await machine.transition(device, TransitionEvent.MAINTENANCE_EXITED, publisher=event_bus)

    async def test_connectivity_restored_from_busy_raises(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(db_session, db_host, operational=DeviceOperationalState.busy, name_suffix="x3")
        machine = DeviceStateMachine()
        with pytest.raises(InvalidTransitionError):
            await machine.transition(device, TransitionEvent.CONNECTIVITY_RESTORED, publisher=event_bus)


class TestStateModel:
    async def test_label_format(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.busy,
            name_suffix="m1",
        )
        snapshot = DeviceStateModel.from_device(device)
        assert snapshot.label() == "busy"
        snapshot_offline = DeviceStateModel(operational=DeviceOperationalState.offline)
        assert snapshot_offline.label() == "offline"


class TestSkipHooks:
    async def test_skip_hooks_true_suppresses_hook_invocation(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.available, name_suffix="sk1"
        )
        calls: list[tuple[str, str]] = []

        class _RecordingHook:
            async def on_transition(
                self,
                device: Device,
                event: TransitionEvent,
                before: DeviceStateModel,
                after: DeviceStateModel,
            ) -> None:
                calls.append((event.value, after.label()))

        machine = DeviceStateMachine(hooks=[_RecordingHook()])
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, skip_hooks=True, publisher=Mock())
        assert changed is True
        assert device.operational_state == DeviceOperationalState.busy
        assert calls == []
