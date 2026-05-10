import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import InvalidTransitionError
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.services.lifecycle_state_machine import DeviceStateMachine
from app.services.lifecycle_state_machine_types import DeviceStateModel, TransitionEvent

pytestmark = [pytest.mark.db]


async def _seed_device(
    db_session: AsyncSession,
    db_host: Host,
    *,
    operational: DeviceOperationalState,
    hold: DeviceHold | None,
    name_suffix: str,
) -> Device:
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
        hold=hold,
        device_type="real_device",
        connection_type="usb",
    )
    db_session.add(device)
    await db_session.flush()
    return device


class TestValidTransitions:
    async def test_available_to_busy_on_session_started(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.available, hold=None, name_suffix="t1"
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert changed
        assert device.operational_state == DeviceOperationalState.busy
        assert device.hold is None

    async def test_busy_to_available_on_session_ended(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.busy, hold=None, name_suffix="t2"
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_ENDED)
        assert changed
        assert device.operational_state == DeviceOperationalState.available

    async def test_available_to_offline_on_connectivity_lost(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.available, hold=None, name_suffix="t3"
        )
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.CONNECTIVITY_LOST)
        assert device.operational_state == DeviceOperationalState.offline

    async def test_offline_to_available_on_connectivity_restored(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.offline, hold=None, name_suffix="t4"
        )
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.CONNECTIVITY_RESTORED)
        assert device.operational_state == DeviceOperationalState.available

    async def test_maintenance_entered_sets_offline_and_hold(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.available, hold=None, name_suffix="t5"
        )
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.MAINTENANCE_ENTERED)
        assert device.operational_state == DeviceOperationalState.offline
        assert device.hold == DeviceHold.maintenance

    async def test_maintenance_exited_clears_hold(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.offline,
            hold=DeviceHold.maintenance,
            name_suffix="t6",
        )
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.MAINTENANCE_EXITED)
        assert device.operational_state == DeviceOperationalState.offline
        assert device.hold is None

    async def test_offline_to_busy_on_session_started(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.offline, hold=None, name_suffix="t7"
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert changed
        assert device.operational_state == DeviceOperationalState.busy
        assert device.hold is None

    async def test_offline_reserved_to_busy_on_session_started(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.offline,
            hold=DeviceHold.reserved,
            name_suffix="t8",
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert changed
        assert device.operational_state == DeviceOperationalState.busy
        assert device.hold == DeviceHold.reserved


class TestReservedHoldTransparent:
    async def test_session_started_works_while_reserved(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.available,
            hold=DeviceHold.reserved,
            name_suffix="r1",
        )
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert device.operational_state == DeviceOperationalState.busy
        assert device.hold == DeviceHold.reserved

    async def test_connectivity_lost_works_while_reserved(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.busy,
            hold=DeviceHold.reserved,
            name_suffix="r2",
        )
        machine = DeviceStateMachine()
        await machine.transition(device, TransitionEvent.CONNECTIVITY_LOST)
        assert device.operational_state == DeviceOperationalState.offline
        assert device.hold == DeviceHold.reserved


class TestIdempotency:
    async def test_maintenance_entered_from_maintenance_is_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.offline,
            hold=DeviceHold.maintenance,
            name_suffix="i1",
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.MAINTENANCE_ENTERED)
        assert changed is False
        assert device.operational_state == DeviceOperationalState.offline
        assert device.hold == DeviceHold.maintenance

    async def test_session_started_from_busy_is_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.busy, hold=None, name_suffix="i2"
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED)
        assert changed is False

    async def test_connectivity_lost_from_maintenance_is_noop(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.offline,
            hold=DeviceHold.maintenance,
            name_suffix="i3",
        )
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.CONNECTIVITY_LOST)
        assert changed is False
        assert device.operational_state == DeviceOperationalState.offline
        assert device.hold == DeviceHold.maintenance


class TestInvalidTransitions:
    async def test_maintenance_exited_without_maintenance_hold_raises(
        self, db_session: AsyncSession, db_host: Host
    ) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.available, hold=None, name_suffix="x2"
        )
        machine = DeviceStateMachine()
        with pytest.raises(InvalidTransitionError):
            await machine.transition(device, TransitionEvent.MAINTENANCE_EXITED)

    async def test_connectivity_restored_from_busy_raises(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.busy, hold=None, name_suffix="x3"
        )
        machine = DeviceStateMachine()
        with pytest.raises(InvalidTransitionError):
            await machine.transition(device, TransitionEvent.CONNECTIVITY_RESTORED)


class TestStateModel:
    async def test_label_format(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session,
            db_host,
            operational=DeviceOperationalState.busy,
            hold=DeviceHold.reserved,
            name_suffix="m1",
        )
        snapshot = DeviceStateModel.from_device(device)
        assert snapshot.label() == "busy/reserved"
        snapshot_none = DeviceStateModel(operational=DeviceOperationalState.offline, hold=None)
        assert snapshot_none.label() == "offline/None"


class TestSkipHooks:
    async def test_skip_hooks_true_suppresses_hook_invocation(self, db_session: AsyncSession, db_host: Host) -> None:
        device = await _seed_device(
            db_session, db_host, operational=DeviceOperationalState.available, hold=None, name_suffix="sk1"
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
        changed = await machine.transition(device, TransitionEvent.SESSION_STARTED, skip_hooks=True)
        assert changed is True
        assert device.operational_state == DeviceOperationalState.busy
        assert calls == []


class TestPassThroughEvents:
    @pytest.mark.parametrize(
        "operational,hold",
        [
            (DeviceOperationalState.available, None),
            (DeviceOperationalState.busy, None),
            (DeviceOperationalState.offline, None),
        ],
    )
    async def test_device_discovered_is_passthrough(
        self, db_session: AsyncSession, db_host: Host, operational: DeviceOperationalState, hold: DeviceHold | None
    ) -> None:
        suffix = f"pt-disc-{operational.value}"
        device = await _seed_device(db_session, db_host, operational=operational, hold=hold, name_suffix=suffix)
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.DEVICE_DISCOVERED)
        assert changed is False
        assert device.operational_state == operational
        assert device.hold == hold

    @pytest.mark.parametrize(
        "operational",
        [DeviceOperationalState.available, DeviceOperationalState.busy],
    )
    async def test_auto_stop_deferred_is_passthrough(
        self, db_session: AsyncSession, db_host: Host, operational: DeviceOperationalState
    ) -> None:
        suffix = f"pt-defer-{operational.value}"
        device = await _seed_device(db_session, db_host, operational=operational, hold=None, name_suffix=suffix)
        machine = DeviceStateMachine()
        changed = await machine.transition(device, TransitionEvent.AUTO_STOP_DEFERRED)
        assert changed is False
        assert device.operational_state == operational
        assert device.hold is None
