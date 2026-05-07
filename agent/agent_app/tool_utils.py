from __future__ import annotations

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
