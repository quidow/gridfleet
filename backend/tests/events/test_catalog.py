from __future__ import annotations

from app.events import PUBLIC_EVENT_NAME_SET


def test_device_health_changed_is_registered() -> None:
    assert "device.health_changed" in PUBLIC_EVENT_NAME_SET


def test_device_crashed_is_registered() -> None:
    assert "device.crashed" in PUBLIC_EVENT_NAME_SET
