from __future__ import annotations

from app.models.appium_node import AppiumNode
from app.models.device import Device


def test_appium_node_has_orchestration_columns() -> None:
    columns = AppiumNode.__table__.columns

    assert "accepting_new_sessions" in columns
    assert "stop_pending" in columns
    assert "generation" in columns


def test_device_has_recovery_decision_columns() -> None:
    columns = Device.__table__.columns

    assert "recovery_allowed" in columns
    assert "recovery_blocked_reason" in columns
