"""Android device health checks via ADB."""

from __future__ import annotations

import re

from adapter.tools import find_adb, get_running_emulator_avd_name
from agent_app.pack.adapter_types import HealthCheckResult, HealthContext
from agent_app.pack.adapter_utils import icmp_reachable, run_cmd, tcp_reachable

_IP_PORT_RE = re.compile(r"^(?P<host>\d+\.\d+\.\d+\.\d+):(?P<port>\d+)$")
_EMULATOR_SERIAL_RE = re.compile(r"^emulator-\d+$")


async def health_check(ctx: HealthContext) -> list[HealthCheckResult]:
    adb = find_adb()
    serial = await _adb_serial_for_health(adb, ctx)

    connected = await _adb_connected(adb, serial)
    responsive = await _adb_shell_echo(adb, serial)
    boot_completed = await _boot_completed(adb, serial)
    results = [
        HealthCheckResult(
            check_id="adb_connected",
            ok=connected,
            detail="" if connected else "Device is not connected to ADB",
        ),
        HealthCheckResult(
            check_id="adb_responsive",
            ok=responsive,
            detail="" if responsive else "Device is not responding to ADB",
        ),
        HealthCheckResult(
            check_id="boot_completed",
            ok=boot_completed,
            detail="" if boot_completed else "Android boot is not complete",
        ),
    ]
    ping_result = await _network_ping(serial)
    if ping_result is not None:
        results.append(ping_result)
    if getattr(ctx, "connection_type", None) == "usb" and getattr(ctx, "ip_address", None):
        timeout = float(getattr(ctx, "ip_ping_timeout_sec", None) or 2.0)
        count = int(getattr(ctx, "ip_ping_count", None) or 1)
        reachable = await icmp_reachable(ctx.ip_address, timeout=timeout, count=count)
        results.append(
            HealthCheckResult(
                check_id="ip_ping",
                ok=reachable,
                detail="" if reachable else "ICMP echo unanswered",
            )
        )
    return results


async def _adb_connected(adb: str, serial: str) -> bool:
    return await run_cmd([adb, "-s", serial, "get-state"], timeout=5) == "device"


async def _adb_shell_echo(adb: str, serial: str) -> bool:
    return await run_cmd([adb, "-s", serial, "shell", "echo", "ok"], timeout=5) == "ok"


async def _boot_completed(adb: str, serial: str) -> bool:
    return await run_cmd([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=5) == "1"


async def _adb_serial_for_health(adb: str, ctx: HealthContext) -> str:
    serial = ctx.device_identity_value
    if not _should_resolve_avd_name(serial, ctx):
        return serial
    avd_name = serial.removeprefix("avd:")
    resolved = await _running_serial_for_avd(adb, avd_name)
    return resolved or serial


def _should_resolve_avd_name(serial: str, ctx: HealthContext) -> bool:
    if serial.startswith("avd:"):
        return True
    if _EMULATOR_SERIAL_RE.match(serial) or _IP_PORT_RE.match(serial):
        return False
    device_type = getattr(ctx, "device_type", None)
    connection_type = getattr(ctx, "connection_type", None)
    return device_type == "emulator" or connection_type == "virtual"


async def _running_serial_for_avd(adb: str, avd_name: str) -> str:
    raw = await run_cmd([adb, "devices"], timeout=5)
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            running_name = await get_running_emulator_avd_name(adb, parts[0])
            if running_name == avd_name:
                return parts[0]
    return await _running_serial_for_avd_console_ports(adb, avd_name)


async def _running_serial_for_avd_console_ports(adb: str, avd_name: str) -> str:
    for port in range(5554, 5586, 2):
        serial = f"emulator-{port}"
        running_name = await get_running_emulator_avd_name(adb, serial)
        if running_name == avd_name:
            return serial
    return ""


async def _network_ping(serial: str) -> HealthCheckResult | None:
    match = _IP_PORT_RE.match(serial)
    if match is None:
        return None
    host = match.group("host")
    port = int(match.group("port"))
    reachable = await tcp_reachable(host, port, timeout=5)
    return HealthCheckResult(
        check_id="ping",
        ok=reachable,
        detail="" if reachable else f"ADB TCP endpoint {host}:{port} unreachable",
    )
