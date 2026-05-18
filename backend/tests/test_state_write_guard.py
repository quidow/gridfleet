"""Unit tests for the state-write guardrail listener.

These tests use lightweight in-memory ``Device`` instances constructed without
a session; the SQLAlchemy ``set`` event fires on attribute assignment regardless
of attach state. No DB marker required.
"""

from __future__ import annotations

import uuid

import pytest

from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard


def _device() -> Device:
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
