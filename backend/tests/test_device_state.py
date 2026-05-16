"""Tests for the new operational_state + hold writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services import state as device_state
from tests.helpers import create_device_record

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

    def fake_queue(session: object, name: str, payload: dict[str, object], *, severity: object = None) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.devices.services.state.queue_event_for_session", fake_queue)

    changed = await device_state.set_operational_state(device, DeviceOperationalState.available, reason="test")
    assert changed is True
    assert device.operational_state == DeviceOperationalState.available
    assert any(name == "device.operational_state_changed" for name, _ in captured)


@pytest.mark.db
@pytest.mark.asyncio
async def test_set_operational_state_noop_when_unchanged(db_session: AsyncSession, default_host_id: str) -> None:
    device = await _persisted_device(db_session, default_host_id)
    changed = await device_state.set_operational_state(device, DeviceOperationalState.offline)
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
        lambda s, n, p, *, severity=None: captured.append((n, p)),
    )

    changed = await device_state.set_hold(device, DeviceHold.reserved, reason="run-1")
    assert changed is True
    assert device.hold == DeviceHold.reserved
    assert any(name == "device.hold_changed" for name, _ in captured)


@pytest.mark.db
@pytest.mark.asyncio
async def test_set_hold_to_none_clears(db_session: AsyncSession, default_host_id: str) -> None:
    device = await _persisted_device(db_session, default_host_id)
    await device_state.set_hold(device, DeviceHold.maintenance)
    changed = await device_state.set_hold(device, None)
    assert changed is True
    assert device.hold is None


def test_legacy_label_for_audit_returns_legacy_string() -> None:
    device = Device(name="x", identity_value="x", connection_target="x")
    device.operational_state = DeviceOperationalState.available
    device.hold = DeviceHold.reserved
    assert device_state.legacy_label_for_audit(device) == "reserved"

    device.hold = None
    assert device_state.legacy_label_for_audit(device) == "available"

    device.operational_state = DeviceOperationalState.busy
    device.hold = None
    assert device_state.legacy_label_for_audit(device) == "busy"


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


def test_operational_state_and_hold_value_sets_are_disjoint() -> None:
    op_values = {v.value for v in DeviceOperationalState}
    hold_values = {v.value for v in DeviceHold}
    assert op_values.isdisjoint(hold_values), (
        "operational_state and hold value sets must not overlap; the chip "
        "projection `hold or operational_state` becomes ambiguous otherwise."
    )
