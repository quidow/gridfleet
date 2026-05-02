from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from adapter import Adapter


class _Ctx:
    device_identity_value = "UDID123"
    connection_target = "UDID123"


@pytest.mark.asyncio
@patch("adapter.telemetry.find_go_ios", return_value="/usr/local/bin/ios")
@patch(
    "adapter.telemetry.run_cmd",
    new_callable=AsyncMock,
    return_value="""{"level":"warning","msg":"failed to get tunnel info"}
{
  "InstantAmperage": 218,
  "Temperature": 2650,
  "Voltage": 4495,
  "IsCharging": true,
  "CurrentCapacity": 87,
  "CycleCount": 48,
  "AtCriticalLevel": false,
  "AtWarnLevel": false
}
""",
)
async def test_telemetry_uses_go_ios_battery_registry(mock_cmd: AsyncMock, _mock_find: object) -> None:
    result = await Adapter().telemetry(_Ctx())

    assert result.supported is True
    assert result.battery_level_percent == 87
    assert result.battery_temperature_c == 26.5
    assert result.charging_state == "charging"
    mock_cmd.assert_awaited_once_with(
        ["/usr/local/bin/ios", "batteryregistry", "--udid=UDID123"],
        timeout=10,
    )


@pytest.mark.asyncio
@patch("adapter.telemetry.find_go_ios", return_value="/usr/local/bin/ios")
@patch(
    "adapter.telemetry.run_cmd",
    new_callable=AsyncMock,
    side_effect=[
        "",
        """{
  "BatteryCurrentCapacity": 42,
  "BatteryIsCharging": false,
  "ExternalConnected": false,
  "HasBattery": true
}
""",
    ],
)
async def test_telemetry_falls_back_to_go_ios_batterycheck(mock_cmd: AsyncMock, _mock_find: object) -> None:
    result = await Adapter().telemetry(_Ctx())

    assert result.supported is True
    assert result.battery_level_percent == 42
    assert result.battery_temperature_c is None
    assert result.charging_state == "discharging"


@pytest.mark.asyncio
@patch("adapter.telemetry.find_go_ios", return_value="")
async def test_telemetry_is_unsupported_without_go_ios(_mock_find: object) -> None:
    result = await Adapter().telemetry(_Ctx())

    assert result.supported is False
