from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from adapter.health import health_check


class _Ctx:
    device_identity_value = "UDID123"
    allow_boot = False
    platform_id = "ios"
    device_type = "real_device"
    connection_type = "usb"
    ip_address: str | None = None
    ip_ping_timeout_sec: float | None = None
    ip_ping_count: int | None = None


DEVICECTL_DETAILS = {
    "info": {"outcome": "success"},
    "result": {
        "connectionProperties": {
            "pairingState": "paired",
            "transportType": "wired",
            "tunnelState": "connected",
        },
        "deviceProperties": {
            "bootState": "booted",
            "ddiServicesAvailable": True,
            "developerModeStatus": "enabled",
            "name": "Quinn iPhone",
            "osVersionNumber": "26.4.2",
        },
        "hardwareProperties": {
            "platform": "iOS",
            "productType": "iPhone18,2",
            "udid": "UDID123",
        },
        "identifier": "COREDEVICE-ID",
    },
}


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value="Booted")
async def test_health_simulator_booted(_mock_state: AsyncMock) -> None:
    result = await health_check(_Ctx())
    assert result[0].ok is True


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=DEVICECTL_DETAILS)
async def test_health_real_device(_mock_details: AsyncMock, _mock_state: AsyncMock) -> None:
    result = await health_check(_Ctx())
    assert result[0].ok is True


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=DEVICECTL_DETAILS)
async def test_health_real_device_prefers_devicectl_details(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    result = await health_check(_Ctx())

    checks = {item.check_id: item for item in result}
    assert checks["devicectl_visible"].ok is True
    assert checks["devicectl_paired"].ok is True
    assert checks["devicectl_tunnel"].ok is True
    assert checks["ios_booted"].ok is True
    assert checks["developer_mode"].ok is True
    assert checks["ddi_services"].ok is True


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch(
    "adapter.health._devicectl_device_details",
    new_callable=AsyncMock,
    return_value={
        **DEVICECTL_DETAILS,
        "result": {
            **DEVICECTL_DETAILS["result"],
            "connectionProperties": {
                **DEVICECTL_DETAILS["result"]["connectionProperties"],
                "tunnelState": "disconnected",
            },
        },
    },
)
async def test_health_real_device_reports_devicectl_tunnel_failure(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    result = await health_check(_Ctx())

    checks = {item.check_id: item for item in result}
    assert checks["devicectl_tunnel"].ok is False
    assert "disconnected" in checks["devicectl_tunnel"].detail


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch(
    "adapter.health._devicectl_device_details",
    new_callable=AsyncMock,
    return_value={
        **DEVICECTL_DETAILS,
        "result": {
            **DEVICECTL_DETAILS["result"],
            "connectionProperties": {
                **DEVICECTL_DETAILS["result"]["connectionProperties"],
                "tunnelState": "disconnected",
            },
        },
    },
)
async def test_health_tvos_real_device_does_not_require_devicectl_tunnel(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    class _TvosCtx(_Ctx):
        platform_id = "tvos"

    result = await health_check(_TvosCtx())

    checks = {item.check_id: item for item in result}
    assert "devicectl_tunnel" not in checks
    assert "ddi_services" not in checks
    assert all(item.ok for item in result)


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch(
    "adapter.health._devicectl_device_details",
    new_callable=AsyncMock,
    return_value={
        **DEVICECTL_DETAILS,
        "result": {
            **DEVICECTL_DETAILS["result"],
            "connectionProperties": {
                **DEVICECTL_DETAILS["result"]["connectionProperties"],
                "transportType": "localNetwork",
                "tunnelState": "disconnected",
            },
            "deviceProperties": {
                "bootedFromSnapshot": True,
                "ddiServicesAvailable": False,
                "developerModeStatus": "enabled",
                "name": "Living Room",
                "osVersionNumber": "26.4",
            },
            "hardwareProperties": {
                "platform": "tvOS",
                "productType": "AppleTV11,1",
                "udid": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            },
            "identifier": "F1A2B3C4-D5E6-7890-ABCD-EF1234567890",
        },
    },
)
async def test_health_tvos_real_device_accepts_wireless_preinstalled_wda_shape(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    class _TvosCtx(_Ctx):
        device_identity_value = "F1A2B3C4-D5E6-7890-ABCD-EF1234567890"
        platform_id = "tvos"
        connection_type = "network"

    result = await health_check(_TvosCtx())

    checks = {item.check_id: item for item in result}
    assert checks["devicectl_visible"].ok is True
    assert checks["devicectl_paired"].ok is True
    assert checks["ios_booted"].ok is True
    assert checks["developer_mode"].ok is True
    assert "devicectl_tunnel" not in checks
    assert "ddi_services" not in checks


@pytest.mark.asyncio
@patch("adapter.health.run_cmd", new_callable=AsyncMock)
async def test_health_devicectl_lookup_matches_coredevice_identifier(mock_cmd: AsyncMock) -> None:
    output_path = ""

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        nonlocal output_path
        if cmd[:3] == ["xcrun", "simctl", "list"]:
            return '{"devices": {}}'
        output_path = cmd[cmd.index("--json-output") + 1]
        return ""

    mock_cmd.side_effect = fake_run_cmd

    with patch("adapter.health._read_output_json") as mock_read:
        mock_read.return_value = json.dumps(
            {
                "info": {"outcome": "success"},
                "result": {
                    "devices": [
                        {
                            "identifier": "F1A2B3C4-D5E6-7890-ABCD-EF1234567890",
                            "connectionProperties": {"pairingState": "paired", "tunnelState": "disconnected"},
                            "deviceProperties": {
                                "bootedFromSnapshot": True,
                                "developerModeStatus": "enabled",
                                "name": "Living Room",
                            },
                            "hardwareProperties": {
                                "platform": "tvOS",
                                "productType": "AppleTV11,1",
                                "udid": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                            },
                        }
                    ]
                },
            }
        )

        class _TvosCtx(_Ctx):
            device_identity_value = "F1A2B3C4-D5E6-7890-ABCD-EF1234567890"
            platform_id = "tvos"

        result = await health_check(_TvosCtx())

    assert output_path
    assert result[0].check_id == "devicectl_visible"
    assert result[0].ok is True


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=None)
async def test_health_real_device_reports_missing_devicectl_visibility(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    result = await health_check(_Ctx())

    assert len(result) == 1
    assert result[0].check_id == "devicectl_visible"
    assert result[0].ok is False


# ---------------------------------------------------------------------------
# ip_ping health check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=DEVICECTL_DETAILS)
@patch("adapter.health.icmp_reachable", new_callable=AsyncMock, return_value=True)
async def test_health_check_emits_ip_ping_when_ip_set(
    mock_icmp: AsyncMock,
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    ctx = _Ctx()
    ctx.connection_type = "usb"
    ctx.ip_address = "10.0.0.7"

    results = await health_check(ctx)

    ip_ping_results = [r for r in results if r.check_id == "ip_ping"]
    assert len(ip_ping_results) == 1
    assert ip_ping_results[0].ok is True
    assert ip_ping_results[0].detail == ""


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=DEVICECTL_DETAILS)
async def test_health_check_omits_ip_ping_when_ip_unset(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    icmp_called: list[str] = []

    async def recording_icmp_reachable(host: str, *, timeout: float = 2.0, count: int = 1) -> bool:
        icmp_called.append(host)
        return True

    monkeypatch.setattr("adapter.health.icmp_reachable", recording_icmp_reachable)

    ctx = _Ctx()
    ctx.connection_type = "usb"
    ctx.ip_address = None

    results = await health_check(ctx)

    assert not any(r.check_id == "ip_ping" for r in results)
    assert icmp_called == [], "icmp_reachable must not be called when ip_address is None"


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=DEVICECTL_DETAILS)
async def test_health_check_omits_ip_ping_when_connection_type_not_usb(
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    icmp_called: list[str] = []

    async def recording_icmp_reachable(host: str, *, timeout: float = 2.0, count: int = 1) -> bool:
        icmp_called.append(host)
        return True

    monkeypatch.setattr("adapter.health.icmp_reachable", recording_icmp_reachable)

    ctx = _Ctx()
    ctx.connection_type = "network"
    ctx.ip_address = "10.0.0.7"

    results = await health_check(ctx)

    assert not any(r.check_id == "ip_ping" for r in results)
    assert icmp_called == [], "icmp_reachable must not be called when connection_type != 'usb'"


@pytest.mark.asyncio
@patch("adapter.health._simulator_state", new_callable=AsyncMock, return_value=None)
@patch("adapter.health._devicectl_device_details", new_callable=AsyncMock, return_value=DEVICECTL_DETAILS)
@patch("adapter.health.icmp_reachable", new_callable=AsyncMock, return_value=False)
async def test_health_check_marks_ip_ping_failure(
    mock_icmp: AsyncMock,
    _mock_details: AsyncMock,
    _mock_state: AsyncMock,
) -> None:
    ctx = _Ctx()
    ctx.connection_type = "usb"
    ctx.ip_address = "10.0.0.7"

    results = await health_check(ctx)

    ip_ping_results = [r for r in results if r.check_id == "ip_ping"]
    assert len(ip_ping_results) == 1
    assert ip_ping_results[0].ok is False
    assert ip_ping_results[0].detail != ""
