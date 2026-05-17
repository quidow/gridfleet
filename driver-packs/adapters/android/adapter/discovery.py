"""ADB device discovery for the Android adapter."""

from __future__ import annotations

import asyncio
import logging
import re

from adapter.device_info import model_name, model_number, software_versions
from adapter.tools import find_adb, get_android_properties, get_running_emulator_avd_name
from agent_app.pack.adapter_types import DiscoveryCandidate, DiscoveryContext
from agent_app.pack.adapter_utils import run_cmd

logger = logging.getLogger(__name__)

_IP_PORT_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+:\d+$")

# Per-device property fetch + AVD-name lookup runs concurrently; cap the bulk
# `adb devices -l` call so a hung adb server cannot eat the full hook budget.
_ADB_DEVICES_TIMEOUT_SECONDS: float = 5.0


def _platform_from_props(props: dict[str, str]) -> tuple[str, str]:
    fireos_version = props.get("fireos_version", "")
    android_version = props.get("android_version", "unknown")
    if fireos_version:
        return "firetv", fireos_version.split(".", 1)[0]
    characteristics = props.get("characteristics", "").lower()
    oem_key = props.get("oem_key", "")
    if "tv" in characteristics or "mbx" in characteristics or oem_key.upper().startswith("ATV"):
        return "android_tv", android_version
    return "android_mobile", android_version


async def _candidate_for_device(adb: str, serial: str, state: str) -> DiscoveryCandidate:
    runnable = state == "device"
    props = await get_android_properties(adb, serial) if runnable else {}
    platform, os_version = _platform_from_props(props)
    hardware = props.get("hardware", "")
    is_emulator = serial.startswith("emulator-") or hardware in {"goldfish", "ranchu"}
    device_type = "emulator" if is_emulator else "real_device"
    connection_type = "virtual" if is_emulator else "network" if _IP_PORT_RE.match(serial) else "usb"
    ip_address = serial.split(":", 1)[0] if connection_type == "network" else ""
    avd_name = await get_running_emulator_avd_name(adb, serial) if is_emulator and runnable else ""
    identity_value = props.get("serial_number") or props.get("boot_serial") or serial
    if is_emulator and avd_name:
        identity_value = f"avd:{avd_name}" if not avd_name.startswith("avd:") else avd_name
    discovered_model_name = model_name(props)
    detected = {
        "os_version": os_version,
        "manufacturer": props.get("manufacturer", ""),
        "model": discovered_model_name,
        "model_number": model_number(props),
        "serial": props.get("serial_number") or props.get("boot_serial") or serial,
        "connection_target": serial,
        "device_type": device_type,
        "connection_type": connection_type,
        "ip_address": ip_address,
        "platform": platform,
        "software_versions": software_versions(props),
    }
    if props.get("fireos_version"):
        marketing = props.get("fireos_marketing_version", "")
        os_version_display = marketing.removeprefix("Fire OS ").strip() or None
        if os_version_display:
            detected["os_version_display"] = os_version_display
    for key in ("fireos_version", "characteristics", "hardware"):
        if props.get(key):
            detected[key] = props[key]
    if avd_name:
        detected["avd_name"] = avd_name
        detected["active_adb_serial"] = serial
    return DiscoveryCandidate(
        identity_scheme="android_serial",
        identity_value=identity_value,
        suggested_name=discovered_model_name or identity_value or serial,
        detected_properties=detected,
        runnable=runnable,
        missing_requirements=[] if runnable else [f"adb_state:{state}"],
        field_errors=[],
        feature_status=[],
    )


async def discover_adb_devices(ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
    adb = find_adb()
    raw = await run_cmd([adb, "devices", "-l"], timeout=_ADB_DEVICES_TIMEOUT_SECONDS)
    if not raw:
        return []

    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        entries.append((parts[0], parts[1]))

    if not entries:
        return []

    results = await asyncio.gather(
        *(_candidate_for_device(adb, serial, state) for serial, state in entries),
        return_exceptions=True,
    )

    candidates: list[DiscoveryCandidate] = []
    for (serial, _state), result in zip(entries, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Discovery failed for device %s: %s", serial, result)
            continue
        candidates.append(result)
    return candidates
