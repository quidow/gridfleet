from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from adapter.telemetry import collect_telemetry


class _Ctx:
    device_identity_value = "ABC123"
    connection_target = "ABC123"


DUMPSYS_OUTPUT = """Current Battery Service state:
  AC powered: false
  USB powered: true
  present: true
  level: 85
  temperature: 305
  status: 2
"""

NO_BATTERY_OUTPUT = """Current Battery Service state:
  AC powered: true
  USB powered: false
  present: false
  level: 100
  temperature: 25
  status: 2
"""


@pytest.mark.asyncio
@patch("adapter.telemetry.run_cmd", new_callable=AsyncMock, return_value=DUMPSYS_OUTPUT)
async def test_collect_telemetry(mock_cmd: AsyncMock) -> None:
    result = await collect_telemetry(_Ctx())
    assert result.supported is True
    assert result.battery_level_percent == 85
    assert result.battery_temperature_c == 30.5
    assert result.charging_state == "charging"


@pytest.mark.asyncio
@patch("adapter.telemetry.run_cmd", new_callable=AsyncMock, return_value=NO_BATTERY_OUTPUT)
async def test_collect_telemetry_uses_temperature_when_battery_absent(mock_cmd: AsyncMock) -> None:
    result = await collect_telemetry(_Ctx())
    assert result.supported is True
    assert result.battery_level_percent is None
    assert result.battery_temperature_c == 25
    assert result.charging_state is None


@pytest.mark.asyncio
@patch("adapter.telemetry.run_cmd", new_callable=AsyncMock, return_value="present: false\n")
async def test_collect_telemetry_unsupported_when_no_battery_or_temperature(mock_cmd: AsyncMock) -> None:
    result = await collect_telemetry(_Ctx())
    assert result.supported is False


@pytest.mark.asyncio
@patch("adapter.telemetry.run_cmd", new_callable=AsyncMock, return_value="")
async def test_collect_telemetry_no_output(mock_cmd: AsyncMock) -> None:
    result = await collect_telemetry(_Ctx())
    assert result.supported is False
