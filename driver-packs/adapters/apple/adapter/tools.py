"""Apple platform tool helpers."""

from __future__ import annotations

import platform

from agent_app.pack.adapter_utils import find_tool


def find_go_ios() -> str:
    ios = find_tool("ios", ["/usr/local/bin/ios"])
    return "" if ios == "ios" else ios


def host_supports_apple_devicectl() -> bool:
    return platform.system() == "Darwin" and find_tool("xcrun") != "xcrun"
