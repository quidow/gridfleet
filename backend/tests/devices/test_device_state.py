"""Tests for the read-time operational-state projection and edge detector."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state as device_state
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _persisted_device(db: AsyncSession, host_id: str) -> Device:
    # Seed the pack so a verified device derives `available` (is_ready_for_use
    # consults the pack manifest — an unseeded pack yields setup_required/offline).
    await seed_test_packs(db)
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
async def test_emit_operational_state_transition_queues_event(
    db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await _persisted_device(db_session, default_host_id)
    captured: list[tuple[str, dict[str, object], object]] = []

    def fake_queue(
        self: object, session: object, name: str, payload: dict[str, object], *, severity: object = None
    ) -> None:
        captured.append((name, payload, severity))

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", fake_queue)

    changed = await device_state.emit_operational_state_transition(
        db_session, device, now=datetime.now(UTC), publisher=event_bus
    )
    assert changed is True
    assert device.operational_state_last_emitted is DeviceOperationalState.available
    assert any(name == "device.operational_state_changed" for name, _, _ in captured)
    payload = captured[0][1]
    assert payload["new_operational_state"] == DeviceOperationalState.available.value
    assert "reason" not in payload
    assert captured[0][2] == "success"


@pytest.mark.db
@pytest.mark.asyncio
async def test_emit_operational_state_transition_noop_when_unchanged(
    db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _persisted_device(db_session, default_host_id)
    changed = await device_state.emit_operational_state_transition(
        db_session, device, now=datetime.now(UTC), publisher=event_bus
    )
    assert changed is True
    changed = await device_state.emit_operational_state_transition(
        db_session, device, now=datetime.now(UTC), publisher=event_bus
    )
    assert changed is False


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
    node = AppiumNode(
        device_id=device.id,
        port=4723,
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

    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        stop_pending=False,
    )
    assert device_state.appium_node_stop_in_flight(device) is False

    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        stop_pending=True,
    )
    assert device_state.appium_node_stop_in_flight(device) is True

    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.stopped,
        stop_pending=False,
    )
    assert device_state.appium_node_stop_in_flight(device) is True
