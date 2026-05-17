"""Android platform tool helpers."""

from __future__ import annotations

import logging
import os
import re

from agent_app.pack.adapter_utils import find_tool, run_cmd

logger = logging.getLogger(__name__)

# Per-call timeout for adb invocations during discovery. Kept short so a
# single unresponsive device cannot eat the 30s adapter-hook budget.
_ADB_DISCOVERY_TIMEOUT_SECONDS: float = 5.0

_GETPROP_LINE_RE = re.compile(r"^\[([^\]]+)\]:\s*\[(.*)\]$")

_PROPERTY_KEYS: dict[str, str] = {
    "android_version": "ro.build.version.release",
    "fireos_version": "ro.build.version.fireos",
    "fireos_marketing_version": "ro.build.mktg.fireos",
    "fireos_version_name": "ro.build.version.name",
    "model": "ro.product.marketname",
    "product_model": "ro.product.model",
    "product_name": "ro.product.name",
    "vendor_model": "ro.product.vendor.model",
    "odm_model": "ro.product.odm.model",
    "manufacturer": "ro.product.manufacturer",
    "build_id": "ro.build.display.id",
    "build_number": "ro.build.lab126.build",
    "product_type": "ro.build.product",
    "product_device": "ro.product.device",
    "netflix_model_group": "ro.nrdp.modelgroup",
    "sdk_version": "ro.build.version.sdk",
    "characteristics": "ro.build.characteristics",
    "hardware": "ro.hardware",
    "serial_number": "ro.serialno",
    "boot_serial": "ro.boot.serialno",
    "oem_key": "ro.oem.key1",
    "brand": "ro.product.brand",
}

_ANDROID_SDK_PATHS = [
    os.path.expanduser("~/Library/Android/sdk"),
    os.path.expanduser("~/Android/Sdk"),
    "/opt/android-sdk",
    "/usr/local/android-sdk",
]
_ADB_SEARCH_PATHS = [
    os.path.expanduser("~/Library/Android/sdk/platform-tools"),
    os.path.expanduser("~/Android/Sdk/platform-tools"),
    "/opt/android-sdk/platform-tools",
    "/usr/local/android-sdk/platform-tools",
]
_EMULATOR_SEARCH_PATHS = [
    os.path.expanduser("~/Library/Android/sdk/emulator"),
    os.path.expanduser("~/Android/Sdk/emulator"),
    "/opt/android-sdk/emulator",
    "/usr/local/android-sdk/emulator",
]


def find_android_home() -> str | None:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(var)
        if val and os.path.isdir(val):
            return val
    for sdk_path in _ANDROID_SDK_PATHS:
        if os.path.isdir(sdk_path) and os.path.isdir(os.path.join(sdk_path, "platform-tools")):
            return sdk_path
    return None


def find_adb() -> str:
    return find_tool("adb", [os.path.join(path, "adb") for path in _ADB_SEARCH_PATHS])


def find_emulator() -> str | None:
    emulator = find_tool("emulator", [os.path.join(path, "emulator") for path in _EMULATOR_SEARCH_PATHS])
    return emulator if emulator != "emulator" else None


def read_avd_config(avd_name: str) -> dict[str, str]:
    config_path = os.path.expanduser(f"~/.android/avd/{avd_name}.avd/config.ini")
    config: dict[str, str] = {}
    try:
        with open(config_path) as handle:
            for line in handle:
                stripped = line.strip()
                if "=" in stripped and not stripped.startswith("#"):
                    key, _, value = stripped.partition("=")
                    config[key.strip()] = value.strip()
    except OSError:
        logger.debug("Failed to read AVD config at %s", config_path, exc_info=True)
    return config


async def get_running_emulator_avd_name(adb: str, serial: str) -> str:
    output = await run_cmd(
        [adb, "-s", serial, "emu", "avd", "name"],
        timeout=_ADB_DISCOVERY_TIMEOUT_SECONDS,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip() and line.strip().upper() != "OK"]
    return lines[0] if lines else ""


async def get_android_properties(adb: str, udid: str) -> dict[str, str]:
    # Single bulk `getprop` invocation. Previously we issued one subprocess per
    # property (~20 sequential adb shell calls per device), which serialised on
    # a single unresponsive device and routinely tripped the 30s adapter-hook
    # timeout for hosts with multiple devices.
    raw = await run_cmd(
        [adb, "-s", udid, "shell", "getprop"],
        timeout=_ADB_DISCOVERY_TIMEOUT_SECONDS,
    )
    if not raw:
        return {}
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        match = _GETPROP_LINE_RE.match(line.strip())
        if match:
            parsed[match.group(1)] = match.group(2)
    return {friendly: parsed[raw_key] for friendly, raw_key in _PROPERTY_KEYS.items() if parsed.get(raw_key)}
