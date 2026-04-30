from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from adapter.discovery import discover_apple_devices


class _Ctx:
    host_id = "h1"
    platform_id = "ios"


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock)
async def test_discover_real_device_from_devicectl_json_output_file(mock_cmd: AsyncMock) -> None:
    devicectl = {
        "result": {
            "devices": [
                {
                    "identifier": "COREDEVICE-ID",
                    "connectionProperties": {
                        "pairingState": "paired",
                        "transportType": "wired",
                        "tunnelState": "connected",
                    },
                    "deviceProperties": {
                        "developerModeStatus": "enabled",
                        "name": "Test User's iPhone",
                        "osVersionNumber": "26.4.2",
                    },
                    "hardwareProperties": {
                        "marketingName": "iPhone 17 Pro Max",
                        "platform": "iOS",
                        "productType": "iPhone18,2",
                        "udid": "00008101-000A1234ABCD5678",
                    },
                }
            ]
        }
    }
    simctl = {"devices": {}}

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd[:3] == ["xcrun", "devicectl", "list"]:
            assert "--json-output" in cmd
            output_path = Path(cmd[cmd.index("--json-output") + 1])
            output_path.write_text(json.dumps(devicectl))
            return ""
        assert cmd == ["xcrun", "simctl", "list", "devices", "available", "-j"]
        return json.dumps(simctl)

    mock_cmd.side_effect = fake_run_cmd

    candidates = await discover_apple_devices(_Ctx())

    assert len(candidates) == 1
    assert candidates[0].identity_value == "00008101-000A1234ABCD5678"
    assert candidates[0].suggested_name == "Test User's iPhone"
    assert candidates[0].detected_properties["device_type"] == "real_device"
    assert candidates[0].detected_properties["connection_type"] == "usb"
    assert candidates[0].detected_properties["model"] == "iPhone 17 Pro Max"
    assert candidates[0].detected_properties["model_number"] == "iPhone18,2"
    assert candidates[0].detected_properties["product_type"] == "iPhone18,2"
    assert candidates[0].detected_properties["os_version"] == "26.4.2"


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock)
async def test_discover_wireless_tvos_real_device_without_tunnel(mock_cmd: AsyncMock) -> None:
    devicectl = {
        "result": {
            "devices": [
                {
                    "identifier": "F1A2B3C4-D5E6-7890-ABCD-EF1234567890",
                    "connectionProperties": {
                        "pairingState": "paired",
                        "transportType": "localNetwork",
                        "tunnelState": "disconnected",
                    },
                    "deviceProperties": {
                        "developerModeStatus": "enabled",
                        "name": "Living Room",
                        "osVersionNumber": "26.4",
                    },
                    "hardwareProperties": {
                        "marketingName": "Apple TV 4K (2nd generation)",
                        "platform": "tvOS",
                        "productType": "AppleTV11,1",
                        "udid": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                    },
                }
            ]
        }
    }
    simctl = {"devices": {}}
    mock_cmd.side_effect = [json.dumps(devicectl), json.dumps(simctl)]

    candidates = await discover_apple_devices(_Ctx())

    assert len(candidates) == 1
    assert candidates[0].identity_value == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    assert candidates[0].suggested_name == "Living Room"
    assert candidates[0].detected_properties["platform"] == "tvos"
    assert candidates[0].detected_properties["connection_type"] == "network"
    assert "ip_address" not in candidates[0].detected_properties
    assert candidates[0].detected_properties["hardware_udid"] == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock)
async def test_discover_real_device_and_simulator(mock_cmd: AsyncMock) -> None:
    devicectl = {
        "result": {
            "devices": [
                {
                    "identifier": "UDID123",
                    "name": "Quinn iPhone",
                    "operatingSystemVersion": "17.2",
                    "hardwareProperties": {"platform": "iOS", "productType": "iPhone15,2", "udid": "UDID123"},
                }
            ]
        }
    }
    simctl = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-17-2": [
                {"udid": "SIM123", "name": "iPhone 15", "state": "Shutdown", "isAvailable": True}
            ]
        }
    }
    mock_cmd.side_effect = [json.dumps(devicectl), json.dumps(simctl)]
    candidates = await discover_apple_devices(_Ctx())
    assert len(candidates) == 2
    assert candidates[0].identity_value == "UDID123"
    assert candidates[0].detected_properties["device_type"] == "real_device"
    assert candidates[1].identity_value == "SIM123"
    assert candidates[1].detected_properties["device_type"] == "simulator"


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock, return_value="")
async def test_discover_no_tools(mock_cmd: AsyncMock) -> None:
    assert await discover_apple_devices(_Ctx()) == []
