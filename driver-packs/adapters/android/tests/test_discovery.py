from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from adapter.discovery import discover_adb_devices


class _Ctx:
    host_id = "test-host"
    platform_id = "android_mobile"


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock)
@patch("adapter.discovery.get_android_properties", new_callable=AsyncMock)
async def test_discover_usb_real_device(mock_props: AsyncMock, mock_cmd: AsyncMock) -> None:
    mock_cmd.return_value = "List of devices attached\nABC123\tdevice usb:1-1"
    mock_props.return_value = {
        "android_version": "14",
        "model": "Pixel 8",
        "model_number": "GKWS6",
        "manufacturer": "Google",
        "serial_number": "ABC123",
        "sdk_version": "34",
        "build_id": "AP1A.240405.002",
        "characteristics": "default",
        "hardware": "oriole",
    }
    candidates = await discover_adb_devices(_Ctx())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.identity_scheme == "android_serial"
    assert candidate.identity_value == "ABC123"
    assert candidate.detected_properties["device_type"] == "real_device"
    assert candidate.detected_properties["connection_type"] == "usb"
    assert candidate.detected_properties["manufacturer"] == "Google"
    assert candidate.detected_properties["model"] == "Pixel 8"
    assert candidate.detected_properties["model_number"] == "GKWS6"
    assert candidate.detected_properties["software_versions"] == {
        "android": "14",
        "sdk": "34",
        "build": "AP1A.240405.002",
    }


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock)
@patch("adapter.discovery.get_android_properties", new_callable=AsyncMock)
async def test_discover_firetv_keeps_model_code_as_model_number(mock_props: AsyncMock, mock_cmd: AsyncMock) -> None:
    mock_cmd.return_value = "List of devices attached\n192.168.1.254:5555\tdevice"
    mock_props.return_value = {
        "android_version": "7.1.2",
        "fireos_version": "6.0",
        "fireos_marketing_version": "Fire OS 6.7.1.1",
        "serial_number": "G070VM2011740KW1",
        "manufacturer": "Amazon",
        "product_model": "AFTMM",
        "product_device": "mantis",
        "product_name": "mantis",
        "netflix_model_group": "FIRETVSTICK2018",
        "sdk_version": "25",
        "build_id": "NS6711",
        "build_number": "5908",
        "characteristics": "tv",
        "hardware": "mt8695",
    }

    candidates = await discover_adb_devices(_Ctx())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.suggested_name == "Fire TV Stick 4K (1st Gen)"
    assert candidate.detected_properties["os_version"] == "6.0"
    assert candidate.detected_properties["model"] == "Fire TV Stick 4K (1st Gen)"
    assert candidate.detected_properties["model_number"] == "AFTMM"
    assert candidate.detected_properties["software_versions"] == {
        "fire_os": "Fire OS 6.7.1.1",
        "fire_os_compat": "6.0",
        "android": "7.1.2",
        "sdk": "25",
        "build": "NS6711",
        "build_number": "5908",
    }


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock)
@patch("adapter.discovery.get_android_properties", new_callable=AsyncMock)
@patch("adapter.discovery.get_running_emulator_avd_name", new_callable=AsyncMock)
async def test_discover_emulator(mock_avd: AsyncMock, mock_props: AsyncMock, mock_cmd: AsyncMock) -> None:
    mock_cmd.return_value = "List of devices attached\nemulator-5554\tdevice"
    mock_props.return_value = {
        "android_version": "14",
        "model": "sdk_gphone64_arm64",
        "manufacturer": "Google",
        "hardware": "ranchu",
        "characteristics": "default",
    }
    mock_avd.return_value = "Pixel_8_API_34"
    candidates = await discover_adb_devices(_Ctx())
    assert len(candidates) == 1
    assert candidates[0].identity_value == "avd:Pixel_8_API_34"
    assert candidates[0].detected_properties["device_type"] == "emulator"


@pytest.mark.asyncio
@patch("adapter.discovery.run_cmd", new_callable=AsyncMock, return_value="")
async def test_discover_no_adb(mock_cmd: AsyncMock) -> None:
    candidates = await discover_adb_devices(_Ctx())
    assert candidates == []
