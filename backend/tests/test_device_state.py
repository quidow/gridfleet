"""Tests for the new operational_state + hold writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services import state as device_state
from app.devices.services import state_write_guard
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _persisted_device(db: AsyncSession, host_id: str) -> Device:
    device = await create_device_record(
        db,
        host_id=host_id,
        identity_value=f"state-{id(db)}",
        connection_target=f"state-{id(db)}",
        name="State Test",
    )
    await db.refresh(device, attribute_names=["appium_node"])
    return device


@pytest.mark.db
@pytest.mark.asyncio
async def test_set_operational_state_writes_and_queues_event(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)
    captured: list[tuple[str, dict[str, object]]] = []

    def fake_queue(
        session: object, name: str, payload: dict[str, object], *, severity: object = None, publisher: object = None
    ) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.devices.services.state.queue_event_for_session", fake_queue)

    changed = await device_state.set_operational_state(
        device, DeviceOperationalState.available, reason="test", publisher=event_bus
    )
    assert changed is True
    assert device.operational_state == DeviceOperationalState.available
    assert any(name == "device.operational_state_changed" for name, _ in captured)


@pytest.mark.db
@pytest.mark.asyncio
async def test_set_operational_state_noop_when_unchanged(db_session: AsyncSession, default_host_id: str) -> None:
    device = await _persisted_device(db_session, default_host_id)
    changed = await device_state.set_operational_state(
        device, DeviceOperationalState.offline, publish_event=False, publisher=event_bus
    )
    assert changed is False


@pytest.mark.db
@pytest.mark.asyncio
async def test_set_hold_writes_and_queues_event(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)
    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "app.devices.services.state.queue_event_for_session",
        lambda s, n, p, *, severity=None, publisher=None: captured.append((n, p)),
    )

    changed = await device_state.set_hold(device, DeviceHold.reserved, reason="run-1", publisher=event_bus)
    assert changed is True
    assert device.hold == DeviceHold.reserved
    assert any(name == "device.hold_changed" for name, _ in captured)


@pytest.mark.db
@pytest.mark.asyncio
async def test_set_hold_to_none_clears(db_session: AsyncSession, default_host_id: str) -> None:
    device = await _persisted_device(db_session, default_host_id)
    await device_state.set_hold(device, DeviceHold.maintenance, publish_event=False, publisher=event_bus)
    changed = await device_state.set_hold(device, None, publish_event=False, publisher=event_bus)
    assert changed is True
    assert device.hold is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_available_when_ready(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.available


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_preserves_verifying(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.verifying

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.verifying


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_offline_when_not_ready(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return False

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_offline_when_health_failed(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale device_checks_healthy=False must block the available projection.

    Regression: callers like node_service `Node started` flipped a device to
    available via ready_operational_state without consulting health, leaving
    UI showing available + unhealthy until the next probe cleared the signal.
    """
    device = await _persisted_device(db_session, default_host_id)
    device.device_checks_healthy = False
    device.device_checks_summary = "Disconnected"
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_offline_when_session_viability_failed(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)
    device.session_viability_status = "failed"
    device.session_viability_error = "probe failed"
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_available_when_signals_unset(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No probes yet (None signals) must not block the available projection."""
    device = await _persisted_device(db_session, default_host_id)
    assert device.device_checks_healthy is None
    assert device.session_viability_status is None

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.available


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_offline_when_node_stop_pending(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intent-driven stop signal on the Appium node row must not surface as
    ``available``.

    Without this gate, the allocator could pick the device between the
    reconciler writing ``stop_pending=True`` and the agent actually
    deregistering the relay — the resulting session would be removed as
    soon as the relay disconnects from the hub.
    """
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode

    device = await _persisted_device(db_session, default_host_id)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=42,
            active_connection_target=device.connection_target,
            stop_pending=True,
            accepting_new_sessions=False,
        )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_ready_operational_state_returns_offline_when_desired_state_stopped(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode

    device = await _persisted_device(db_session, default_host_id)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.stopped,
            pid=42,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return True

    monkeypatch.setattr("app.devices.services.state.is_ready_for_use_async", fake_ready)
    assert await device_state.ready_operational_state(db_session, device) == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_appium_node_stop_in_flight_returns_false_when_unloaded(
    db_session: AsyncSession, default_host_id: str
) -> None:
    """Lazy-load guard: an unloaded ``appium_node`` relationship must not
    trigger a sync IO inside the AsyncSession context. The predicate must
    return False without touching the attribute.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.appium_nodes.models import AppiumDesiredState, AppiumNode

    device = await _persisted_device(db_session, default_host_id)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.stopped,
            stop_pending=True,
        )
    db_session.add(node)
    await db_session.commit()
    db_session.expire(device, ["appium_node"])
    assert "appium_node" in sa_inspect(device).unloaded

    assert device_state.appium_node_stop_in_flight(device) is False
    # Predicate must not have lazy-loaded the relationship.
    assert "appium_node" in sa_inspect(device).unloaded


def test_appium_node_stop_in_flight_predicate() -> None:
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="predicate",
        connection_target="predicate",
        name="Predicate",
        host_id=None,  # type: ignore[arg-type]
    )

    device.appium_node = None
    assert device_state.appium_node_stop_in_flight(device) is False

    with state_write_guard.bypass():
        device.appium_node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            stop_pending=False,
        )
    assert device_state.appium_node_stop_in_flight(device) is False

    with state_write_guard.bypass():
        device.appium_node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            stop_pending=True,
        )
    assert device_state.appium_node_stop_in_flight(device) is True

    with state_write_guard.bypass():
        device.appium_node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.stopped,
            stop_pending=False,
        )
    assert device_state.appium_node_stop_in_flight(device) is True


def test_operational_state_and_hold_value_sets_are_disjoint() -> None:
    op_values = {v.value for v in DeviceOperationalState}
    # DeviceHold.maintenance overlaps intentionally during the hold-collapse migration
    # (Phase 1 adds maintenance to operational_state; Phase 5 removes DeviceHold.maintenance).
    hold_values = {v.value for v in DeviceHold} - {"maintenance"}
    assert op_values.isdisjoint(hold_values), (
        "operational_state and hold value sets must not overlap; the chip "
        "projection `hold or operational_state` becomes ambiguous otherwise."
    )
