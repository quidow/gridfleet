from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from agent_app.host.telemetry import get_host_telemetry


async def test_get_host_telemetry_happy_path() -> None:
    with (
        patch("agent_app.host.telemetry.psutil.cpu_percent", return_value=71.2),
        patch(
            "agent_app.host.telemetry.psutil.virtual_memory",
            return_value=SimpleNamespace(used=24 * 1024 * 1024, total=32 * 1024 * 1024),
        ),
        patch(
            "agent_app.host.telemetry.psutil.disk_usage",
            return_value=SimpleNamespace(
                used=100 * 1024**3,
                total=250 * 1024**3,
                percent=40.0,
            ),
        ),
    ):
        payload = await get_host_telemetry()

    assert payload["cpu_percent"] == 71.2
    assert payload["memory_used_mb"] == 24
    assert payload["memory_total_mb"] == 32
    assert payload["disk_used_gb"] == 100.0
    assert payload["disk_total_gb"] == 250.0
    assert payload["disk_percent"] == 40.0


async def test_get_host_telemetry_returns_partial_nulls_when_disk_sampling_fails() -> None:
    with (
        patch("agent_app.host.telemetry.psutil.cpu_percent", return_value=11.5),
        patch(
            "agent_app.host.telemetry.psutil.virtual_memory",
            return_value=SimpleNamespace(used=8 * 1024 * 1024, total=16 * 1024 * 1024),
        ),
        patch("agent_app.host.telemetry.psutil.disk_usage", side_effect=RuntimeError("disk unavailable")),
    ):
        payload = await get_host_telemetry()

    assert payload["cpu_percent"] == 11.5
    assert payload["memory_used_mb"] == 8
    assert payload["memory_total_mb"] == 16
    assert payload["disk_used_gb"] is None
    assert payload["disk_total_gb"] is None
    assert payload["disk_percent"] is None


async def test_get_host_telemetry_uses_iso8601_utc_timestamp() -> None:
    payload = await get_host_telemetry()
    recorded_at = datetime.fromisoformat(payload["recorded_at"])

    assert recorded_at.tzinfo is not None
    assert payload["recorded_at"].endswith("+00:00")
