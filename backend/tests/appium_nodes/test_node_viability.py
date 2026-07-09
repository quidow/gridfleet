from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from app.appium_nodes.services.node_viability import device_node_is_viable

if TYPE_CHECKING:
    from app.devices.models import Device

NOW = datetime(2026, 7, 9, tzinfo=UTC)


def _device(*, started_at: datetime | None, restart_requested_at: datetime | None) -> Device:
    node = SimpleNamespace(
        pid=123,
        active_connection_target="localhost:4723",
        started_at=started_at,
        restart_requested_at=restart_requested_at,
    )
    return cast("Device", SimpleNamespace(appium_node=node))


def test_pending_watermark_without_spawn_time_is_not_viable() -> None:
    # started_at None + a recent watermark: the SQL arm reads NULL >= x as
    # unknown -> false, so the Python re-check must agree (not viable) rather
    # than raising TypeError on None >= datetime.
    device = _device(started_at=None, restart_requested_at=NOW - timedelta(seconds=5))
    assert device_node_is_viable(device, now=NOW, restart_window_sec=120) is False


def test_expired_watermark_without_spawn_time_is_viable() -> None:
    device = _device(started_at=None, restart_requested_at=NOW - timedelta(seconds=600))
    assert device_node_is_viable(device, now=NOW, restart_window_sec=120) is True
