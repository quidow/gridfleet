from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

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


def find_android_home() -> str | None:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(var)
        if val and os.path.isdir(val):
            return val
    for sdk_path in _ANDROID_SDK_PATHS:
        if os.path.isdir(sdk_path) and os.path.isdir(os.path.join(sdk_path, "platform-tools")):
            return sdk_path
    return None


def _find_adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    for search_dir in _ADB_SEARCH_PATHS:
        candidate = os.path.join(search_dir, "adb")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "adb"


async def _run_cmd(cmd: list[str]) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()
    except FileNotFoundError:
        logger.warning("Command not found: %s", cmd[0])
        return ""


def _read_avd_config(avd_name: str) -> dict[str, str]:
    config_path = os.path.expanduser(f"~/.android/avd/{avd_name}.avd/config.ini")
    config: dict[str, str] = {}
    try:
        with open(config_path) as handle:
            for line in handle:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    config[key.strip()] = value.strip()
    except OSError:
        logger.debug("Failed to read AVD config at %s", config_path, exc_info=True)
    return config


async def _get_running_emulator_avd_name(adb: str, serial: str) -> str:
    output = await _run_cmd([adb, "-s", serial, "emu", "avd", "name"])
    if output:
        lines = [line.strip() for line in output.splitlines() if line.strip() and line.strip().upper() != "OK"]
        if lines:
            return lines[0]
    return ""


async def _get_android_properties(adb: str, udid: str) -> dict[str, str]:
    prop_keys = {
        "android_version": "ro.build.version.release",
        "fireos_version": "ro.build.version.fireos",
        "model": "ro.product.model",
        "manufacturer": "ro.product.manufacturer",
        "build_id": "ro.build.display.id",
        "product_type": "ro.build.product",
        "sdk_version": "ro.build.version.sdk",
        "characteristics": "ro.build.characteristics",
        "hardware": "ro.hardware",
        "serial_number": "ro.serialno",
        "boot_serial": "ro.boot.serialno",
    }
    results = await asyncio.gather(
        *[_run_cmd([adb, "-s", udid, "shell", "getprop", prop]) for prop in prop_keys.values()]
    )
    return {key: val for key, val in zip(prop_keys.keys(), results, strict=True) if val}
