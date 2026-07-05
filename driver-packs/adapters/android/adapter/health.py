"""Android device health checks via ADB."""

from __future__ import annotations

import re

from .tools import find_adb, get_running_emulator_avd_name
from agent_app.pack.adapter_types import HealthCheckResult, HealthContext
from agent_app.pack.adapter_utils import icmp_reachable, run_cmd, tcp_reachable

_IP_PORT_RE = re.compile(r"^(?P<host>\d+\.\d+\.\d+\.\d+):(?P<port>\d+)$")
_EMULATOR_SERIAL_RE = re.compile(r"^emulator-\d+$")
_SYSTEM_PORT_CAPABILITY = "appium:systemPort"


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
    is_network = getattr(ctx, "connection_type", None) == "network" or _IP_PORT_RE.match(serial) is not None
    link_dead = not connected or not responsive
    tcp_ok = ping_result is not None and ping_result.ok
    if is_network and link_dead:
        if await _adb_unauthorized(adb, serial):
            results.append(
                HealthCheckResult(
                    check_id="adb_unauthorized",
                    ok=False,
                    detail="Device authorization revoked; re-authorize on the device",
                )
            )
        elif tcp_ok:
            results.append(
                HealthCheckResult(
                    check_id="link_repairable",
                    ok=False,
                    detail="adb transport down but device TCP-reachable; reconnect recommended",
                    recommended_action="reconnect",
                )
            )
    expected = getattr(ctx, "expected_identity_value", None)
    if connected and expected and getattr(ctx, "connection_type", None) == "network":
        identity_result = await _verify_identity(adb, serial, str(expected))
        if identity_result is not None:
            results.append(identity_result)
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
    orphan_result = await _orphan_system_port_check(ctx)
    if orphan_result is not None:
        results.append(orphan_result)
    return results


async def _orphan_system_port_check(ctx: HealthContext) -> HealthCheckResult | None:
    """Detect the orphan adb-server systemPort binding.

    Runs only when the control plane positively reports no live session or
    in-flight probe (``has_live_session is False``, not None): with nothing
    live, no one may legitimately hold the claimed systemPort on this host.
    The adb forward table is the wrong detector — the orphan server socket
    survives with an EMPTY table — so connect-test the port, exactly the
    uia2 driver's own busy check that fails the next create.
    """
    if getattr(ctx, "has_live_session", None) is not False:
        return None
    claimed = getattr(ctx, "claimed_ports", None)
    port = claimed.get(_SYSTEM_PORT_CAPABILITY) if isinstance(claimed, dict) else None
    if not port:
        return None
    bound = await tcp_reachable("127.0.0.1", int(port), timeout=2)
    return HealthCheckResult(
        check_id="claimed_ports_free",
        ok=not bound,
        detail="" if not bound else f"systemPort {port} bound with no live session (orphan adb-server socket)",
        recommended_action=None if not bound else "release_forwarded_ports",
    )


async def _verify_identity(adb: str, serial: str, expected: str) -> HealthCheckResult | None:
    """Compare ``ro.serialno`` at the probed adb target with the expected identity.

    Network adb targets are addressed by ip:port, so a different device on a
    reused address answers happily — only a definitive serial mismatch fails;
    an empty/failed read reports nothing (never flap health on a flaky query).
    """
    try:
        reported = await run_cmd([adb, "-s", serial, "shell", "getprop", "ro.serialno"], timeout=5)
    except Exception:  # noqa: BLE001 — inconclusive, not a health failure
        return None
    if not reported:
        return None
    ok = reported == expected
    return HealthCheckResult(
        check_id="identity",
        ok=ok,
        detail="" if ok else f"Device at target reports serial {reported}, expected {expected}",
    )


async def _adb_connected(adb: str, serial: str) -> bool:
    return await run_cmd([adb, "-s", serial, "get-state"], timeout=5) == "device"


async def _adb_unauthorized(adb: str, serial: str) -> bool:
    """True if ``serial`` is present in ``adb devices`` marked ``unauthorized``.

    Distinguishes a revoked-authorization device (TCP-reachable, but no retry of
    ``adb connect`` can repair it — needs device-side approval) from a dead link.
    """
    raw = await run_cmd([adb, "devices"], timeout=5)
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial and parts[1] == "unauthorized":
            return True
    return False


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
