"""Detect installed tools and supported platforms on this host."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from copy import deepcopy
from typing import Any

from agent_app.appium_process import _find_java
from agent_app.tool_paths import find_appium as _find_appium

logger = logging.getLogger(__name__)

_CAPABILITIES_REFRESH_INTERVAL_SEC = 600
_TOOL_CHECKS: list[tuple[str, str | None, list[str], str]] = [
    ("appium", None, ["--version"], r"(\d+\.\d+\.\d+)"),
    ("adb", None, ["--version"], r"Android Debug Bridge.*?(\d+\.\d+\.\d+)"),
    ("xcodebuild", "xcodebuild", ["-version"], r"Xcode\s+(\d+\.\d+(?:\.\d+)?)"),
    ("go_ios", "ios", ["--version"], r"v?(\d+\.\d+\.\d+)"),
    ("java", None, ["-version"], r'"(\d+[\d.]+)"'),
]
_DEFAULT_CAPABILITIES: dict[str, Any] = {"platforms": [], "tools": {}, "missing_prerequisites": []}
_capabilities_snapshot: dict[str, Any] | None = None
_capabilities_snapshot_at: float | None = None
_capabilities_lock = asyncio.Lock()


async def _run_cmd(*args: str) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode().strip() or stderr.decode().strip()
        return output if proc.returncode == 0 else None
    except (FileNotFoundError, TimeoutError, OSError):
        return None


async def _get_tool_version(cmd: str, args: list[str], pattern: str) -> str | None:
    """Run a tool command and extract version via regex pattern."""
    output = await _run_cmd(cmd, *args)
    if output is None:
        return None
    match = re.search(pattern, output)
    return match.group(1) if match else output.split("\n")[0].strip()


def _resolve_tool_command(name: str, configured_cmd: str | None) -> str:
    if configured_cmd:
        return configured_cmd
    if name == "appium":
        return _find_appium()
    if name == "java":
        return _find_java()
    return name


async def detect_capabilities() -> dict[str, Any]:
    """Detect installed tools and infer supported platforms."""
    tools: dict[str, str] = {}

    checks = [(_resolve_tool_command(name, cmd), args, pattern) for name, cmd, args, pattern in _TOOL_CHECKS]

    # Run all tool version checks in parallel
    results = await asyncio.gather(
        *(_get_tool_version(cmd, args, pattern) for cmd, args, pattern in checks),
        return_exceptions=True,
    )

    for (name, _, _, _), result in zip(_TOOL_CHECKS, results, strict=True):
        if isinstance(result, str):
            tools[name] = result

    platforms: list[str] = []

    required_tools = ["appium", "java"]
    missing_prerequisites = [name for name in required_tools if name not in tools]

    return {"platforms": platforms, "tools": tools, "missing_prerequisites": missing_prerequisites}


def get_capabilities_snapshot() -> dict[str, Any]:
    """Return the last detected capabilities without running probes."""
    return deepcopy(_capabilities_snapshot or _DEFAULT_CAPABILITIES)


def clear_capabilities_snapshot() -> None:
    """Clear the cached capabilities snapshot."""
    global _capabilities_snapshot, _capabilities_snapshot_at
    _capabilities_snapshot = None
    _capabilities_snapshot_at = None


def _snapshot_is_stale() -> bool:
    if _capabilities_snapshot_at is None:
        return True
    return time.monotonic() - _capabilities_snapshot_at >= _CAPABILITIES_REFRESH_INTERVAL_SEC


async def refresh_capabilities_snapshot() -> dict[str, Any]:
    """Refresh and return the cached capabilities snapshot."""
    global _capabilities_snapshot, _capabilities_snapshot_at
    async with _capabilities_lock:
        snapshot = await detect_capabilities()
        _capabilities_snapshot = deepcopy(snapshot)
        _capabilities_snapshot_at = time.monotonic()
        return deepcopy(snapshot)


async def get_or_refresh_capabilities_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Return cached capabilities, refreshing only when missing/stale or forced."""
    if force or _capabilities_snapshot is None or _snapshot_is_stale():
        return await refresh_capabilities_snapshot()
    return get_capabilities_snapshot()


async def capabilities_refresh_loop(
    interval_sec: int = _CAPABILITIES_REFRESH_INTERVAL_SEC,
    *,
    refresh_immediately: bool = True,
) -> None:
    """Periodically refresh the capability snapshot outside the health request path."""
    if not refresh_immediately:
        await asyncio.sleep(interval_sec)
    while True:
        try:
            await refresh_capabilities_snapshot()
        except Exception:
            logger.exception("Capability snapshot refresh failed")
        await asyncio.sleep(interval_sec)
