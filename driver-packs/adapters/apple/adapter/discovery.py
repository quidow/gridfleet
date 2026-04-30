"""Apple device discovery via devicectl and simctl."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from agent_app.pack.adapter_types import DiscoveryCandidate, DiscoveryContext
from agent_app.pack.adapter_utils import run_cmd


async def discover_apple_devices(ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
    physical, simulators = await asyncio.gather(_discover_physical(), _discover_simulators())
    return physical + simulators


async def _discover_physical() -> list[DiscoveryCandidate]:
    raw = await _run_devicectl_devices()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    candidates: list[DiscoveryCandidate] = []
    for device in data.get("result", {}).get("devices", []):
        hardware = device.get("hardwareProperties", {})
        device_properties = device.get("deviceProperties", {})
        platform_name = _platform_id(str(hardware.get("platform") or device.get("platform") or "ios"))
        if platform_name not in {"ios", "tvos"}:
            continue
        udid = _identity_value(device)
        if not udid:
            continue
        if not _is_physical_device_connected(device, platform_name):
            continue
        connection = device.get("connectionProperties", {})
        transport_type = str(connection.get("transportType") or "") if isinstance(connection, dict) else ""
        product_type = str(hardware.get("productType") or "")
        marketing_name = str(hardware.get("marketingName") or "")
        os_version = str(
            device.get("operatingSystemVersion")
            or device_properties.get("osVersionNumber")
            or hardware.get("osVersion")
            or "unknown"
        )
        name = str(
            device.get("name")
            or device_properties.get("name")
            or hardware.get("name")
            or hardware.get("marketingName")
            or product_type
            or udid
        )
        candidates.append(
            DiscoveryCandidate(
                identity_scheme="apple_udid",
                identity_value=udid,
                suggested_name=name,
                detected_properties={
                    "platform": platform_name,
                    "device_type": "real_device",
                    "connection_type": "network" if transport_type == "localNetwork" else "usb",
                    "manufacturer": "Apple",
                    "model": marketing_name or product_type,
                    "model_number": product_type,
                    "os_version": os_version,
                    "connection_target": udid,
                    "hardware_udid": str(hardware.get("udid") or ""),
                    "product_type": product_type,
                },
                runnable=True,
                missing_requirements=[],
                field_errors=[],
                feature_status=[],
            )
        )
    return candidates


async def _run_devicectl_devices() -> str:
    with tempfile.NamedTemporaryFile(prefix="gridfleet-devicectl-", suffix=".json") as output:
        stdout = await run_cmd(
            ["xcrun", "devicectl", "list", "devices", "--timeout", "20", "--json-output", output.name]
        )
        path = Path(output.name)
        if path.exists():
            content = path.read_text().strip()
            if content:
                return content
        return stdout


def _identity_value(device: dict[str, object]) -> str:
    hardware = device.get("hardwareProperties", {})
    hardware_udid = str(hardware.get("udid") or "") if isinstance(hardware, dict) else ""
    identifier = str(device.get("identifier") or "")
    return hardware_udid or identifier


def _is_physical_device_connected(device: dict[str, object], platform_name: str) -> bool:
    connection = device.get("connectionProperties")
    if not isinstance(connection, dict):
        return True

    pairing_state = str(connection.get("pairingState") or "").lower()
    if pairing_state in {"unpaired", "notpaired"}:
        return False

    tunnel_state = str(connection.get("tunnelState") or "").lower()
    if platform_name == "tvos":
        return tunnel_state != "unavailable"
    return tunnel_state not in {"unavailable", "disconnected"}


async def _discover_simulators() -> list[DiscoveryCandidate]:
    raw = await run_cmd(["xcrun", "simctl", "list", "devices", "available", "-j"])
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    candidates: list[DiscoveryCandidate] = []
    for runtime_name, devices in data.get("devices", {}).items():
        platform_name = _platform_id(runtime_name)
        if platform_name not in {"ios", "tvos"}:
            continue
        version = _runtime_version(runtime_name)
        for device in devices:
            udid = str(device.get("udid") or "")
            if not udid or device.get("isAvailable") is False:
                continue
            name = str(device.get("name") or "Simulator")
            candidates.append(
                DiscoveryCandidate(
                    identity_scheme="apple_udid",
                    identity_value=udid,
                    suggested_name=name,
                    detected_properties={
                        "platform": platform_name,
                        "device_type": "simulator",
                        "connection_type": "virtual",
                        "manufacturer": "Apple",
                        "model": "Simulator",
                        "os_version": version,
                        "connection_target": udid,
                        "simulator_state": str(device.get("state") or ""),
                    },
                    runnable=True,
                    missing_requirements=[],
                    field_errors=[],
                    feature_status=[],
                )
            )
    return candidates


def _platform_id(raw: str) -> str:
    lower = raw.lower()
    if "tvos" in lower or "appletv" in lower:
        return "tvos"
    if "ios" in lower or "iphoneos" in lower:
        return "ios"
    return lower


def _runtime_version(raw: str) -> str:
    match = re.search(r"(\d+)[-.](\d+)(?:[-.](\d+))?", raw)
    if not match:
        return "unknown"
    parts = [match.group(1), match.group(2)]
    if match.group(3):
        parts.append(match.group(3))
    return ".".join(parts)
