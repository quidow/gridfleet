"""Unit tests for the state-write guardrail listener.

These tests use lightweight in-memory ``Device`` instances constructed without
a session; the SQLAlchemy ``set`` event fires on attribute assignment regardless
of attach state. No DB marker required.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest

from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard


def _device() -> Device:
    # Pattern for fixture builders: wrap construction in bypass() to seed
    # state directly. SQLAlchemy fires the ``set`` event for each constructor
    # kwarg, so unguarded construction would trip the guard.
    with state_write_guard.bypass():
        return Device(
            id=uuid.uuid4(),
            operational_state=DeviceOperationalState.available,
        )


@pytest.fixture(autouse=True)
def _register_guard() -> None:
    state_write_guard.register()


def test_direct_assignment_from_unsanctioned_module_raises() -> None:
    device = _device()
    with pytest.raises(state_write_guard.StateWriteOutsideSanctionedWriterError) as exc:
        device.operational_state = DeviceOperationalState.offline
    assert "devices.operational_state" in str(exc.value)
    assert "tests.test_state_write_guard" in str(exc.value)


def test_assignment_from_allowlisted_module_is_permitted() -> None:
    """A write that originates from app.devices.services.state is accepted by the guard.

    ``set_operational_state`` calls ``_persistent_session`` before the assignment,
    which asserts the device is attached to a session. We monkeypatch that helper
    to return a dummy session object so the writer proceeds to the actual
    ``device.operational_state = new_state`` assignment on a detached device.
    The guard inspects the call-stack module name, so the write still originates
    from ``app.devices.services.state`` and must be accepted.
    """
    from app.devices.services import state as state_writer

    device = _device()
    _dummy_session = MagicMock()

    import app.devices.services.state as _state_mod

    original_persistent_session = _state_mod._persistent_session
    _state_mod._persistent_session = lambda _dev: _dummy_session  # type: ignore[assignment]
    try:
        asyncio.run(
            state_writer.set_operational_state(
                device,
                DeviceOperationalState.offline,
                reason="test",
                publish_event=False,
            )
        )
    finally:
        _state_mod._persistent_session = original_persistent_session

    assert device.operational_state == DeviceOperationalState.offline


def test_bypass_context_manager_suppresses_listener() -> None:
    device = _device()
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.offline
    assert device.operational_state == DeviceOperationalState.offline
    with pytest.raises(state_write_guard.StateWriteOutsideSanctionedWriterError):
        device.operational_state = DeviceOperationalState.available


@pytest.mark.parametrize(
    ("table", "column"),
    sorted(state_write_guard.ALLOWLIST.keys()),
)
def test_allowlist_pins_every_protected_column(table: str, column: str) -> None:
    """If a row is added or removed from ALLOWLIST, this test fails so the change
    is reviewed against ``CLAUDE.md`` and the design doc.
    """
    expected_columns = {
        ("devices", "operational_state"),
        ("devices", "hold"),
        ("devices", "lifecycle_policy_state"),
        ("appium_nodes", "desired_state"),
        ("appium_nodes", "desired_port"),
        ("appium_nodes", "transition_token"),
        ("appium_nodes", "transition_deadline"),
        ("appium_nodes", "pid"),
        ("appium_nodes", "port"),
        ("appium_nodes", "active_connection_target"),
        ("appium_nodes", "health_running"),
        ("appium_nodes", "health_state"),
        ("appium_nodes", "last_health_checked_at"),
        ("appium_nodes", "last_observed_at"),
    }
    assert (table, column) in expected_columns
