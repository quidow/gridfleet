from __future__ import annotations

from typing import ClassVar

import pytest
from adapter.normalize import normalize_device


class _Ctx:
    host_id = "h1"
    platform_id = "roku_real"
    raw_input: ClassVar[dict[str, str]] = {"serial_number": "YJ1234567890", "ip_address": "192.168.1.50"}


@pytest.mark.asyncio
async def test_normalize_roku_device() -> None:
    result = await normalize_device(_Ctx())
    assert result.identity_scheme == "roku_serial"
    assert result.identity_value == "YJ1234567890"
    assert result.ip_address == "192.168.1.50"
    assert result.connection_type == "network"


@pytest.mark.asyncio
async def test_normalize_missing_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoSerial:
        host_id = "h1"
        platform_id = "roku_real"
        raw_input: ClassVar[dict[str, str]] = {"ip_address": "192.168.1.50"}

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        return {}

    monkeypatch.setattr("adapter.normalize.fetch_device_info", fake_device_info)

    result = await normalize_device(NoSerial())
    assert len(result.field_errors) == 1
    assert result.field_errors[0].field_id == "serial_number"


@pytest.mark.asyncio
async def test_normalize_resolves_serial_from_ecp(monkeypatch: pytest.MonkeyPatch) -> None:
    class IpOnly:
        host_id = "h1"
        platform_id = "roku_real"
        raw_input: ClassVar[dict[str, str]] = {"ip_address": "192.168.1.50"}

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        assert ip_address == "192.168.1.50"
        return {
            "serial-number": "YJ1234567890",
            "software-version": "12.5.0",
            "software-build": "4178",
            "model-name": "Roku Ultra",
            "model-number": "4802RW",
        }

    monkeypatch.setattr("adapter.normalize.fetch_device_info", fake_device_info)

    result = await normalize_device(IpOnly())

    assert result.identity_value == "YJ1234567890"
    assert result.ip_address == "192.168.1.50"
    assert result.os_version == "12.5.0"
    assert result.manufacturer == "Roku"
    assert result.model == "Roku Ultra"
    assert result.model_number == "4802RW"
    assert result.software_versions == {"roku_os": "12.5.0", "build": "4178"}
    assert result.field_errors == []


@pytest.mark.asyncio
async def test_normalize_replaces_unknown_os_version_from_ecp(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnknownVersion:
        host_id = "h1"
        platform_id = "roku_real"
        raw_input: ClassVar[dict[str, str]] = {"ip_address": "192.168.1.50", "os_version": "unknown"}

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        assert ip_address == "192.168.1.50"
        return {"serial-number": "YJ1234567890", "software-version": "12.5.0"}

    monkeypatch.setattr("adapter.normalize.fetch_device_info", fake_device_info)

    result = await normalize_device(UnknownVersion())

    assert result.os_version == "12.5.0"
