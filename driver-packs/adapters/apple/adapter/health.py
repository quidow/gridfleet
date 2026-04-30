"""Apple health checks."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_app.pack.adapter_types import HealthCheckResult, HealthContext
from agent_app.pack.adapter_utils import run_cmd


async def health_check(ctx: HealthContext) -> list[HealthCheckResult]:
    state = await _simulator_state(ctx.device_identity_value)
    if state is not None:
        return [HealthCheckResult(check_id="simulator_state", ok=state == "Booted", detail=state)]
    details = await _devicectl_device_details(ctx.device_identity_value)
    if not details:
        return [
            HealthCheckResult(
                check_id="devicectl_visible",
                ok=False,
                detail="Device is not visible to xcrun devicectl",
            )
        ]
    return _real_device_health_results(details, platform_id=getattr(ctx, "platform_id", None))


def _real_device_health_results(details: dict[str, object], *, platform_id: str | None) -> list[HealthCheckResult]:
    result = details.get("result")
    if not isinstance(result, dict):
        return [
            HealthCheckResult(
                check_id="devicectl_visible",
                ok=False,
                detail="devicectl did not return device details",
            )
        ]

    connection = _dict_value(result, "connectionProperties")
    properties = _dict_value(result, "deviceProperties")
    hardware = _dict_value(result, "hardwareProperties")

    pairing_state = str(connection.get("pairingState") or "")
    tunnel_state = str(connection.get("tunnelState") or "")
    boot_state = str(properties.get("bootState") or "")
    booted_from_snapshot = properties.get("bootedFromSnapshot")
    developer_mode = str(properties.get("developerModeStatus") or "")
    ddi_available = properties.get("ddiServicesAvailable")
    is_tvos = platform_id == "tvos"
    booted = boot_state.lower() == "booted" or (is_tvos and booted_from_snapshot is True)

    checks = [
        HealthCheckResult(
            check_id="devicectl_visible",
            ok=True,
            detail=_device_label(properties, hardware, result),
        ),
        HealthCheckResult(
            check_id="devicectl_paired",
            ok=pairing_state.lower() == "paired",
            detail=pairing_state or "unknown pairing state",
        ),
        HealthCheckResult(
            check_id="ios_booted",
            ok=booted,
            detail=boot_state or ("booted from snapshot" if booted_from_snapshot is True else "unknown boot state"),
        ),
        HealthCheckResult(
            check_id="developer_mode",
            ok=developer_mode.lower() == "enabled",
            detail=developer_mode or "unknown developer mode state",
        ),
    ]
    if not is_tvos:
        checks.insert(
            2,
            HealthCheckResult(
                check_id="devicectl_tunnel",
                ok=tunnel_state.lower() == "connected",
                detail=tunnel_state or "unknown tunnel state",
            ),
        )
        checks.append(
            HealthCheckResult(
                check_id="ddi_services",
                ok=ddi_available is True,
                detail="available" if ddi_available is True else "unavailable",
            )
        )
    return checks


def _dict_value(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _device_label(
    properties: dict[str, object],
    hardware: dict[str, object],
    result: dict[str, object],
) -> str:
    name = properties.get("name")
    product_type = hardware.get("productType")
    identifier = hardware.get("udid") or result.get("identifier")
    return " ".join(str(part) for part in (name, product_type, identifier) if part)


async def _devicectl_device_details(udid: str) -> dict[str, object] | None:
    with tempfile.NamedTemporaryFile(prefix="gridfleet-devicectl-details-", suffix=".json") as output:
        stdout = await run_cmd(
            [
                "xcrun",
                "devicectl",
                "list",
                "devices",
                "--timeout",
                "20",
                "--json-output",
                output.name,
            ],
            timeout=25,
        )
        raw = _read_output_json(Path(output.name), stdout)
        if not raw:
            return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    info = _dict_value(data, "info")
    if info.get("outcome") != "success":
        return None
    for device in _devices_from_devicectl_list(data):
        hardware = _dict_value(device, "hardwareProperties")
        identifiers = {str(value) for value in (hardware.get("udid"), device.get("identifier")) if value}
        if udid in identifiers:
            return {"info": info, "result": device}
    return None


def _devices_from_devicectl_list(data: dict[str, object]) -> list[dict[str, object]]:
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    devices = result.get("devices")
    if not isinstance(devices, list):
        return []
    return [device for device in devices if isinstance(device, dict)]


def _read_output_json(path: Path, fallback: str) -> str:
    if path.exists():
        content = path.read_text().strip()
        if content:
            return content
    return fallback


async def _simulator_state(udid: str) -> str | None:
    raw = await run_cmd(["xcrun", "simctl", "list", "devices", "-j"], timeout=10)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for devices in data.get("devices", {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return str(device.get("state") or "")
    return None
