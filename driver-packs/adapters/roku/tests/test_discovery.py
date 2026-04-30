from __future__ import annotations

import pytest
from adapter.discovery import _ssdp_search_sync, discover_roku_devices, parse_ssdp_response


class _Ctx:
    host_id = "h1"
    platform_id = "roku_network"


def test_parse_ssdp_response_reads_location_and_serial_case_insensitively() -> None:
    payload = (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=3600\r\n"
        "st: roku:ecp\r\n"
        "LOCATION: http://192.168.1.134:8060/\r\n"
        "USN: uuid:roku:ecp:P0A070000007\r\n"
        "\r\n"
    )

    response = parse_ssdp_response(payload.encode())

    assert response is not None
    assert response.ip_address == "192.168.1.134"
    assert response.serial_number == "P0A070000007"


def test_parse_ssdp_response_ignores_non_roku_responses() -> None:
    payload = (
        "HTTP/1.1 200 OK\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        "LOCATION: http://192.168.1.55:8060/\r\n"
        "\r\n"
    )

    assert parse_ssdp_response(payload.encode()) is None


def test_ssdp_search_returns_empty_when_multicast_send_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DeniedSocket:
        def __enter__(self) -> _DeniedSocket:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def setsockopt(self, *_args: object) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def sendto(self, _payload: bytes, _addr: tuple[str, int]) -> None:
            raise PermissionError("multicast denied")

    monkeypatch.setattr("adapter.discovery.socket.socket", lambda *_args: _DeniedSocket())

    assert _ssdp_search_sync(0.1) == []


@pytest.mark.asyncio
async def test_discover_roku_devices_uses_ssdp_and_ecp_device_info(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SsdpDevice:
        ip_address = "192.168.1.134"
        serial_number = "P0A070000007"

    async def fake_search() -> list[_SsdpDevice]:
        return [_SsdpDevice()]

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        assert ip_address == "192.168.1.134"
        return {
            "serial-number": "P0A070000007",
            "vendor-name": "Roku",
            "model-name": "Roku Ultra",
            "model-number": "4802RW",
            "software-version": "15.1.4",
            "software-build": "3321",
        }

    monkeypatch.setattr("adapter.discovery.ssdp_search", fake_search)
    monkeypatch.setattr("adapter.discovery.fetch_device_info", fake_device_info)

    candidates = await discover_roku_devices(_Ctx())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.identity_scheme == "roku_serial"
    assert candidate.identity_value == "P0A070000007"
    assert candidate.suggested_name == "Roku Ultra"
    assert candidate.runnable is True
    assert candidate.detected_properties == {
        "connection_target": "192.168.1.134",
        "ip_address": "192.168.1.134",
        "device_type": "real_device",
        "connection_type": "network",
        "manufacturer": "Roku",
        "model": "Roku Ultra",
        "model_number": "4802RW",
        "os_version": "15.1.4",
        "software_versions": {"roku_os": "15.1.4", "build": "3321"},
    }


@pytest.mark.asyncio
async def test_discover_roku_devices_keeps_ssdp_candidate_when_device_info_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SsdpDevice:
        ip_address = "192.168.1.134"
        serial_number = "P0A070000007"

    async def fake_search() -> list[_SsdpDevice]:
        return [_SsdpDevice()]

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        raise TimeoutError("timed out")

    monkeypatch.setattr("adapter.discovery.ssdp_search", fake_search)
    monkeypatch.setattr("adapter.discovery.fetch_device_info", fake_device_info)

    candidates = await discover_roku_devices(_Ctx())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.identity_value == "P0A070000007"
    assert candidate.suggested_name == "P0A070000007"
    assert candidate.runnable is False
    assert candidate.missing_requirements == ["ecp_device_info"]
    assert candidate.field_errors[0].field_id == "ip_address"
