"""Android lifecycle actions via ADB and emulator tools."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_app.pack.adapter_types import LifecycleActionResult, LifecycleContext
from agent_app.pack.adapter_utils import run_cmd

from adapter.health import _adb_shell_echo
from adapter.tools import find_adb, find_emulator, get_running_emulator_avd_name


async def lifecycle_action(
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> LifecycleActionResult:
    if action_id == "reconnect":
        return await _reconnect(args)
    if action_id == "boot":
        return await _boot_avd(str(args.get("avd_name") or ctx.device_identity_value).removeprefix("avd:"))
    if action_id == "shutdown":
        return await _shutdown_avd(str(args.get("avd_name") or ctx.device_identity_value).removeprefix("avd:"))
    if action_id == "state":
        return await _get_state(ctx.device_identity_value)
    if action_id == "resolve":
        return await _resolve_target(ctx.device_identity_value)
    return LifecycleActionResult(ok=False, detail=f"Unknown action: {action_id}")


async def _reconnect(args: dict[str, Any]) -> LifecycleActionResult:
    adb = find_adb()
    ip_address = str(args.get("ip_address") or "")
    port = int(args.get("port") or 5555)
    target = f"{ip_address}:{port}" if ":" not in ip_address else ip_address
    await run_cmd([adb, "disconnect", target])
    await asyncio.sleep(1)
    result = await run_cmd([adb, "connect", target])
    if "connected" not in result.lower() and "already connected" not in result.lower():
        return LifecycleActionResult(ok=False, detail=result or f"ADB connect failed for {target}")
    if not await _adb_shell_echo(adb, target):
        return LifecycleActionResult(ok=False, detail=f"ADB verify failed for {target}")
    return LifecycleActionResult(ok=True, state="reconnecting")


async def _boot_avd(avd_name: str) -> LifecycleActionResult:
    adb = find_adb()
    serial = await _running_serial_for_avd(adb, avd_name)
    if serial:
        return LifecycleActionResult(ok=True, state=serial)

    emulator = find_emulator()
    if not emulator:
        return LifecycleActionResult(ok=False, detail="emulator not found")
    proc = await asyncio.create_subprocess_exec(
        emulator,
        "-avd",
        avd_name,
        "-no-audio",
        "-no-window",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _ = proc
    return LifecycleActionResult(ok=True, state="booting")


async def _shutdown_avd(avd_name: str) -> LifecycleActionResult:
    adb = find_adb()
    serial = await _running_serial_for_avd(adb, avd_name)
    if not serial:
        return LifecycleActionResult(ok=False, detail=f"AVD {avd_name!r} is not running")
    await run_cmd([adb, "-s", serial, "emu", "kill"])
    return LifecycleActionResult(ok=True, state="stopped")


async def _get_state(serial: str) -> LifecycleActionResult:
    adb = find_adb()
    state = await run_cmd([adb, "-s", serial, "get-state"])
    if state == "device":
        return LifecycleActionResult(ok=True, state="running")
    if state:
        return LifecycleActionResult(ok=True, state=state)

    avd_name = serial.removeprefix("avd:")
    if _looks_like_avd_name(avd_name):
        resolved = await _running_serial_for_avd(adb, avd_name)
        if resolved:
            return LifecycleActionResult(ok=True, state="running")
    return LifecycleActionResult(ok=False, state="not_found")


async def _resolve_target(identity: str) -> LifecycleActionResult:
    if not identity.startswith("avd:"):
        return LifecycleActionResult(ok=True, state=identity)
    adb = find_adb()
    serial = await _running_serial_for_avd(adb, identity.removeprefix("avd:"))
    if not serial:
        return LifecycleActionResult(ok=False, detail=f"Unable to resolve {identity}")
    return LifecycleActionResult(ok=True, state=serial)


async def _running_serial_for_avd(adb: str, avd_name: str) -> str:
    raw = await run_cmd([adb, "devices"])
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            name = await get_running_emulator_avd_name(adb, parts[0])
            if name == avd_name:
                return parts[0]
    return ""


def _looks_like_avd_name(value: str) -> bool:
    return bool(value) and ":" not in value and not value.startswith("emulator-")
