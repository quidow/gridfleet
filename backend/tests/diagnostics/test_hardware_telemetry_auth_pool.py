"""HardwareTelemetryService must forward the agent BasicAuth pool to the agent call.

Without it the per-device telemetry fetch is unauthenticated and the agent rejects
it when the auth gate is enabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from tests.fakes import FakeSettingsReader

pytestmark = pytest.mark.asyncio

POOL = Mock()


async def test_device_telemetry_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = AsyncMock(return_value={})
    monkeypatch.setattr("app.hosts.service_hardware_telemetry.fetch_pack_device_telemetry", mock)
    svc = HardwareTelemetryService(publisher=Mock(), settings=FakeSettingsReader({}), circuit_breaker=Mock(), pool=POOL)
    device = SimpleNamespace(
        host=SimpleNamespace(ip="127.0.0.1", agent_port=5100),
        connection_target="udid-1",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
        connection_type=SimpleNamespace(value="network"),
        ip_address="10.0.0.1",
    )

    await svc._get_device_telemetry(device)

    assert mock.await_args.kwargs["pool"] is POOL
