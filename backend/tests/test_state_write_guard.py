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

from app.appium_nodes.models import AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from tests.helpers import test_event_bus as event_bus

# Columns the attribute-event guard structurally CANNOT enforce: they are
# written only by SQLAlchemy Core bulk UPDATEs, which fire no ORM ``set`` event.
# Documented carve-out (WI-2, option B). See the test below + the module
# docstring in state_write_guard.py.
_CORE_WRITE_ONLY: frozenset[tuple[str, str]] = frozenset({("appium_nodes", "last_observed_at")})


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
                publisher=event_bus,
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


def _node() -> AppiumNode:
    with state_write_guard.bypass():
        return AppiumNode(id=uuid.uuid4())


@pytest.mark.parametrize(
    ("table", "column"),
    sorted(set(state_write_guard.ALLOWLIST) - _CORE_WRITE_ONLY),
)
def test_unsanctioned_write_actually_raises_for_every_orm_enforceable_column(table: str, column: str) -> None:
    """Enforcement (not just key-presence): a write from an unsanctioned module
    (this test) must actually raise for every ORM-enforceable protected column.

    The guard fires on the SQLAlchemy ``set`` event before any type coercion, so
    a type-agnostic sentinel value is sufficient to trip it. ``last_observed_at``
    is excluded as the documented Core-update carve-out (see the dedicated test).
    """
    target: Device | AppiumNode = _device() if table == "devices" else _node()
    with pytest.raises(state_write_guard.StateWriteOutsideSanctionedWriterError) as exc:
        setattr(target, column, "guard-enforcement-probe")
    assert f"{table}.{column}" in str(exc.value)


def test_last_observed_at_is_a_documented_core_write_carve_out() -> None:
    """``last_observed_at`` is written only via a Core bulk UPDATE
    (``reconciler._touch_last_observed``), which the attribute-event guard cannot
    intercept. Its ALLOWLIST entry must name the real writer (``reconciler``,
    documentary only) rather than the stale ``heartbeat`` — and it is excluded
    from the enforcement parametrization above so the gap is explicit, not silent.
    """
    assert ("appium_nodes", "last_observed_at") in _CORE_WRITE_ONLY
    assert state_write_guard.ALLOWLIST[("appium_nodes", "last_observed_at")] == frozenset(
        {"app.appium_nodes.services.reconciler"}
    )


def test_register_is_called_during_app_lifespan() -> None:
    """``register()`` is idempotent; verify the call site exists in lifespan source."""
    import inspect as _inspect

    from app import main as app_main

    lifespan_src = _inspect.getsource(app_main.lifespan)
    assert "state_write_guard.register()" in lifespan_src
