"""Android lifecycle actions via ADB and emulator tools."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_app.pack.adapter_types import LifecycleActionResult, LifecycleContext
from agent_app.pack.adapter_utils import run_cmd, tcp_reachable

from .health import _adb_shell_echo
from .tools import find_adb, get_running_emulator_avd_name


async def lifecycle_action(
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> LifecycleActionResult:
    if action_id == "reconnect":
        return await _reconnect(args)
    if action_id == "release_forwarded_ports":
        return await _release_forwarded_ports(args, ctx)
    if action_id == "resolve":
        return await _resolve_target(ctx.device_identity_value)
    return LifecycleActionResult(ok=False, detail=f"Unknown action: {action_id}")


_UIA2_DEVICE_PORT = "tcp:6790"  # uia2 instrumentation port; remote side of the rebind probe


async def _release_forwarded_ports(args: dict[str, Any], ctx: LifecycleContext) -> LifecycleActionResult:
    """Instrumented cure ladder for the orphan adb-server systemPort socket.

    Climbs cheapest-first, re-probing the socket after each rung, and reports
    the curing rung in ``detail`` (the control plane records it per occurrence).
    ``forward --remove`` is host-local and free; the rebind path can find and
    dispose the table-invisible listener; ``adb kill-server`` is the known cure
    but drops every transport on the host, so it only runs when the control
    plane reports no live session anywhere on this host.
    """
    if args.get("has_live_session"):
        return LifecycleActionResult(ok=False, detail="refused: live session appeared for device")
    claimed = args.get("claimed_ports")
    raw_port = claimed.get("appium:systemPort") if isinstance(claimed, dict) else None
    if not raw_port:
        return LifecycleActionResult(ok=False, detail="no claimed systemPort supplied")
    port = int(raw_port)
    adb = find_adb()
    serial = ctx.device_identity_value

    if not await _system_port_bound(port):
        return LifecycleActionResult(ok=True, state="released", detail="cured_by=none (port already free)")

    await run_cmd([adb, "-s", serial, "forward", "--remove", f"tcp:{port}"], timeout=10)
    if not await _system_port_bound(port):
        return LifecycleActionResult(ok=True, state="released", detail="cured_by=forward_remove")

    await run_cmd([adb, "-s", serial, "forward", f"tcp:{port}", _UIA2_DEVICE_PORT], timeout=10)
    await run_cmd([adb, "-s", serial, "forward", "--remove", f"tcp:{port}"], timeout=10)
    if not await _system_port_bound(port):
        return LifecycleActionResult(ok=True, state="released", detail="cured_by=rebind_remove")

    if args.get("host_has_live_sessions"):
        return LifecycleActionResult(
            ok=False,
            detail="uncured: forward_remove,rebind_remove failed; bounce blocked by live sessions on host",
        )
    await run_cmd([adb, "kill-server"], timeout=15)
    ip_address = str(args.get("ip_address") or "")
    if ip_address:
        target = ip_address if ":" in ip_address else f"{ip_address}:{int(args.get('port') or 5555)}"
        await run_cmd([adb, "connect", target], timeout=15)
    else:
        await run_cmd([adb, "start-server"], timeout=15)
    if not await _system_port_bound(port):
        return LifecycleActionResult(ok=True, state="released", detail="cured_by=adb_bounce")
    return LifecycleActionResult(ok=False, detail="uncured: all rungs failed (forward_remove,rebind_remove,adb_bounce)")


async def _system_port_bound(port: int) -> bool:
    return await tcp_reachable("127.0.0.1", port, timeout=1)


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
