from __future__ import annotations

from typing import Any

import pytest
from adapter.normalize import normalize_device


class _Ctx:
    host_id = "host-1"
    platform_id = "firetv_real"

    def __init__(self, raw_input: dict[str, Any]) -> None:
        self.raw_input = raw_input


@pytest.mark.asyncio
async def test_normalize_network_ip_connects_adb_and_uses_stable_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str]) -> str:
        calls.append(cmd)
        return "connected to 192.168.1.99:5555"

    async def fake_get_android_properties(adb: str, target: str) -> dict[str, str]:
        assert adb == "adb"
        assert target == "192.168.1.99:5555"
        return {
            "android_version": "7.1.2",
            "fireos_version": "6.0",
            "serial_number": "G070VM1234567890",
            "manufacturer": "Amazon",
            "model": "Fire TV Stick 4K",
            "model_number": "AFTMM",
            "build_id": "NS6271/2495",
            "sdk_version": "25",
        }

    monkeypatch.setattr("adapter.normalize.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.normalize.run_cmd", fake_run_cmd, raising=False)
    monkeypatch.setattr("adapter.normalize.get_android_properties", fake_get_android_properties)

    result = await normalize_device(
        _Ctx(
            {
                "connection_target": "192.168.1.99",
                "ip_address": "192.168.1.99",
                "connection_type": "network",
            }
        )
    )

    assert result.field_errors == []
    assert result.identity_value == "G070VM1234567890"
    assert result.connection_target == "192.168.1.99:5555"
    assert result.ip_address == "192.168.1.99"
    assert result.connection_type == "network"
    assert result.os_version == "6.0"
    assert result.manufacturer == "Amazon"
    assert result.model == "Fire TV Stick 4K"
    assert result.model_number == "AFTMM"
    assert result.software_versions == {
        "fire_os": "6.0",
        "android": "7.1.2",
        "sdk": "25",
        "build": "NS6271/2495",
    }
    assert calls == [["adb", "connect", "192.168.1.99:5555"]]


@pytest.mark.asyncio
async def test_normalize_firetv_keeps_model_code_as_model_number(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str]) -> str:
        return "already connected to 192.168.1.254:5555"

    async def fake_get_android_properties(adb: str, target: str) -> dict[str, str]:
        assert adb == "adb"
        assert target == "192.168.1.254:5555"
        return {
            "android_version": "7.1.2",
            "fireos_version": "6.0",
            "fireos_marketing_version": "Fire OS 6.7.1.1",
            "fireos_version_name": "Fire OS 6.7.1.1 (NS6711/5908)",
            "serial_number": "G070VM2011740KW1",
            "manufacturer": "Amazon",
            "product_model": "AFTMM",
            "product_device": "mantis",
            "product_name": "mantis",
            "netflix_model_group": "FIRETVSTICK2018",
            "build_id": "NS6711",
            "build_number": "5908",
            "sdk_version": "25",
        }

    monkeypatch.setattr("adapter.normalize.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.normalize.run_cmd", fake_run_cmd, raising=False)
    monkeypatch.setattr("adapter.normalize.get_android_properties", fake_get_android_properties)

    result = await normalize_device(
        _Ctx(
            {
                "connection_target": "192.168.1.254",
                "ip_address": "192.168.1.254",
                "connection_type": "network",
            }
        )
    )

    assert result.os_version == "6.0"
    assert result.model == "Fire TV Stick 4K (1st Gen)"
    assert result.model_number == "AFTMM"
    assert result.software_versions == {
        "fire_os": "Fire OS 6.7.1.1",
        "fire_os_compat": "6.0",
        "android": "7.1.2",
        "sdk": "25",
        "build": "NS6711",
        "build_number": "5908",
    }
