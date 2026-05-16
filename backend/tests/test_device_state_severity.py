"""Tests that device state writers forward the correct severity to the event bus."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.devices.services.state import set_hold, set_operational_state

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_severity_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Return a list that accumulates {type, severity} dicts for each publish call."""
    captured: list[dict[str, Any]] = []

    async def _fake_publish(name: str, payload: dict[str, Any], *, severity: str | None = None) -> None:
        captured.append({"type": name, "severity": severity})

    monkeypatch.setattr("app.events.event_bus.publish", _fake_publish)
    return captured


def _events_of_type(captured: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [e for e in captured if e["type"] == event_type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def device(db_session: AsyncSession, db_host: Host) -> Device:
    """Create a minimal offline device for severity tests."""
    from tests.helpers import create_device

    return await create_device(
        db_session,
        host_id=db_host.id,
        name="severity-test-device",
        operational_state=DeviceOperationalState.offline,
    )


# ---------------------------------------------------------------------------
# state.py writer tests
# ---------------------------------------------------------------------------


async def test_set_operational_state_forwards_explicit_severity(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_operational_state passes severity kwarg through to queue_event_for_session."""
    captured = _make_severity_capture(monkeypatch)

    await set_operational_state(
        device,
        DeviceOperationalState.available,
        reason="Health checks recovered",
        severity="success",
    )
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "success"


async def test_set_operational_state_none_severity_passes_none(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no severity kwarg is passed, None is forwarded (bus uses catalog default)."""
    captured = _make_severity_capture(monkeypatch)

    await set_operational_state(
        device,
        DeviceOperationalState.available,
        reason="no severity kwarg",
    )
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    # None means the bus resolves the catalog default ("info")
    assert events[0]["severity"] is None


async def test_set_hold_forwards_explicit_severity(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _make_severity_capture(monkeypatch)

    await set_hold(
        device,
        DeviceHold.maintenance,
        reason="Operator entered maintenance",
        severity="info",
    )
    await db_session.commit()

    events = _events_of_type(captured, "device.hold_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "info"


async def test_set_hold_none_severity_passes_none(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _make_severity_capture(monkeypatch)

    await set_hold(device, DeviceHold.maintenance, reason="no severity kwarg")
    await db_session.commit()

    events = _events_of_type(captured, "device.hold_changed")
    assert len(events) == 1
    assert events[0]["severity"] is None


# ---------------------------------------------------------------------------
# State machine: connectivity events
# ---------------------------------------------------------------------------


async def test_state_machine_connectivity_lost_emits_warning(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECTIVITY_LOST → severity='warning' (available→offline)."""
    captured = _make_severity_capture(monkeypatch)

    # Put device in available state first
    device.operational_state = DeviceOperationalState.available
    await db_session.flush()

    changed = await DeviceStateMachine().transition(device, TransitionEvent.CONNECTIVITY_LOST, reason="ADB lost")
    assert changed is True
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "warning"


async def test_state_machine_connectivity_restored_emits_success(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECTIVITY_RESTORED → severity='success' (offline→available)."""
    captured = _make_severity_capture(monkeypatch)

    changed = await DeviceStateMachine().transition(
        device, TransitionEvent.CONNECTIVITY_RESTORED, reason="ADB restored"
    )
    assert changed is True
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "success"


async def test_state_machine_session_started_emits_info(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SESSION_STARTED → severity='info' (available→busy)."""
    captured = _make_severity_capture(monkeypatch)

    device.operational_state = DeviceOperationalState.available
    await db_session.flush()

    changed = await DeviceStateMachine().transition(device, TransitionEvent.SESSION_STARTED, reason="session started")
    assert changed is True
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "info"


async def test_state_machine_verification_failed_emits_warning(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VERIFICATION_FAILED → severity='warning' (verifying→offline)."""
    captured = _make_severity_capture(monkeypatch)

    device.operational_state = DeviceOperationalState.verifying
    await db_session.flush()

    changed = await DeviceStateMachine().transition(
        device, TransitionEvent.VERIFICATION_FAILED, reason="verification failed"
    )
    assert changed is True
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "warning"


async def test_state_machine_verification_passed_emits_success(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VERIFICATION_PASSED → severity='success' (verifying→available)."""
    captured = _make_severity_capture(monkeypatch)

    device.operational_state = DeviceOperationalState.verifying
    await db_session.flush()

    changed = await DeviceStateMachine().transition(
        device, TransitionEvent.VERIFICATION_PASSED, reason="verification passed"
    )
    assert changed is True
    await db_session.commit()

    events = _events_of_type(captured, "device.operational_state_changed")
    assert len(events) == 1
    assert events[0]["severity"] == "success"


async def test_state_machine_maintenance_entered_emits_info_on_hold(
    db_session: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAINTENANCE_ENTERED changes hold → severity='info' on hold_changed event."""
    captured = _make_severity_capture(monkeypatch)

    device.operational_state = DeviceOperationalState.available
    await db_session.flush()

    changed = await DeviceStateMachine().transition(device, TransitionEvent.MAINTENANCE_ENTERED, reason="operator")
    assert changed is True
    await db_session.commit()

    hold_events = _events_of_type(captured, "device.hold_changed")
    assert len(hold_events) == 1
    assert hold_events[0]["severity"] == "info"
