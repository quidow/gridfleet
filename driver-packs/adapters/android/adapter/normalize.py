"""Android device identity normalization."""

from __future__ import annotations

import re

from agent_app.pack.adapter_types import FieldError, NormalizedDevice, NormalizeDeviceContext
from agent_app.pack.adapter_utils import run_cmd

from adapter.device_info import model_name, model_number, software_versions
from adapter.tools import find_adb, get_android_properties, get_running_emulator_avd_name

_IP_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_IP_PORT_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+:\d+$")


def _network_adb_target(value: str) -> str:
    if _IP_PORT_RE.match(value):
        return value
    if _IP_RE.match(value):
        return f"{value}:5555"
    return value


def _is_network_target(value: str) -> bool:
    return bool(_IP_RE.match(value) or _IP_PORT_RE.match(value))


async def _ensure_network_target_connected(adb: str, target: str) -> str | None:
    result = await run_cmd([adb, "connect", target])
    normalized = result.lower()
    if "connected" in normalized or "already connected" in normalized:
        return None
    return result or f"ADB connect failed for {target}"


async def normalize_device(ctx: NormalizeDeviceContext) -> NormalizedDevice:
    raw = ctx.raw_input
    target = str(raw.get("connection_target") or raw.get("identity_value") or raw.get("ip_address") or "")
    errors: list[FieldError] = []
    if not target:
        errors.append(
            FieldError(field_id="connection_target", message="Connection target required for Android devices")
        )
    adb = find_adb()
    requested_connection_type = str(raw.get("connection_type") or "")
    is_network = requested_connection_type == "network" or _is_network_target(target)
    if is_network and target:
        target = _network_adb_target(target)
        connect_error = await _ensure_network_target_connected(adb, target)
        if connect_error:
            errors.append(FieldError(field_id="connection_target", message=connect_error))
    props = await get_android_properties(adb, target) if target else {}
    hardware = props.get("hardware", "")
    is_emulator = target.startswith("emulator-") or hardware in {"goldfish", "ranchu"}
    avd_name = await get_running_emulator_avd_name(adb, target) if is_emulator and target else ""
    identity_value = props.get("serial_number") or props.get("boot_serial") or target
    connection_target = target
    if is_emulator and avd_name:
        identity_value = f"avd:{avd_name}" if not avd_name.startswith("avd:") else avd_name
        connection_target = avd_name
    connection_type = "virtual" if is_emulator else "network" if _IP_PORT_RE.match(target) else "usb"
    os_version = props.get("fireos_version") or props.get("android_version") or str(raw.get("os_version") or "")
    return NormalizedDevice(
        identity_scheme="android_serial",
        identity_scope="host" if is_emulator else "global",
        identity_value=identity_value,
        connection_target=connection_target,
        ip_address=target.split(":", 1)[0] if connection_type == "network" else "",
        device_type="emulator" if is_emulator else "real_device",
        connection_type=connection_type,
        os_version=os_version,
        field_errors=errors,
        manufacturer=props.get("manufacturer", ""),
        model=model_name(props),
        model_number=model_number(props),
        software_versions=software_versions(props),
    )
