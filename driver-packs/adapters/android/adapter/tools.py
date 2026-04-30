"""Android platform tool helpers."""

from __future__ import annotations

import os

from agent_app.pack.adapter_utils import find_tool, run_cmd

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
        pass
    return config


async def get_running_emulator_avd_name(adb: str, serial: str) -> str:
    output = await run_cmd([adb, "-s", serial, "emu", "avd", "name"])
    lines = [line.strip() for line in output.splitlines() if line.strip() and line.strip().upper() != "OK"]
    return lines[0] if lines else ""


async def get_android_properties(adb: str, udid: str) -> dict[str, str]:
    prop_keys = {
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
    }
    values = [await run_cmd([adb, "-s", udid, "shell", "getprop", prop]) for prop in prop_keys.values()]
    return {key: value for key, value in zip(prop_keys.keys(), values, strict=True) if value}
