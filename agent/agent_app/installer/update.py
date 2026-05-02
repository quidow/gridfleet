from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from agent_app.installer.install import (
    HealthCheckResult,
    _run_command,
    poll_agent_health,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.installer.plan import InstallConfig


@dataclass(frozen=True)
class DrainResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class UpdateResult:
    to_version: str | None
    restarted: bool
    drain: DrainResult
    health: HealthCheckResult


def _agent_package_spec(to_version: str | None) -> str:
    return f"gridfleet-agent=={to_version}" if to_version else "gridfleet-agent"


def _uv_upgrade_command(to_version: str | None) -> list[str]:
    return ["uv", "tool", "upgrade", _agent_package_spec(to_version)]


def _restart_command(os_name: str, *, uid: int | None = None) -> list[str]:
    if os_name == "Linux":
        return ["systemctl", "restart", "gridfleet-agent"]
    if os_name == "Darwin":
        sudo_uid = os.environ.get("SUDO_UID")
        if uid is not None:
            resolved_uid = uid
        elif sudo_uid and sudo_uid.isdecimal():
            resolved_uid = int(sudo_uid)
        else:
            resolved_uid = os.getuid()
        return ["launchctl", "kickstart", "-k", f"gui/{resolved_uid}/com.gridfleet.agent"]
    raise RuntimeError(f"Unsupported OS: {os_name}")


def _health_url(config: InstallConfig) -> str:
    return f"http://localhost:{config.port}/agent/health"


def format_update_dry_run(
    config: InstallConfig,
    *,
    to_version: str | None = None,
    os_name: str | None = None,
    uid: int | None = None,
) -> str:
    resolved_os = os_name or platform.system()
    uv_command = " ".join(_uv_upgrade_command(to_version))
    try:
        restart_command = " ".join(_restart_command(resolved_os, uid=uid))
    except RuntimeError as exc:
        restart_command = str(exc).replace("Unsupported OS", "unsupported OS", 1)

    return f"""GridFleet Agent update dry run

Actions:
  - Wait for active local nodes to drain: {_health_url(config)}
  - Upgrade package: {uv_command}
  - Restart service: {restart_command}
  - Poll local health: {_health_url(config)}
"""


def _active_node_count(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    appium_processes = payload.get("appium_processes")
    if not isinstance(appium_processes, dict):
        return None
    running_nodes = appium_processes.get("running_nodes")
    if not isinstance(running_nodes, list):
        return None
    return len(running_nodes)


def wait_for_update_drain(
    url: str,
    *,
    timeout_sec: float = 120.0,
    interval_sec: float = 2.0,
    get: Callable[..., object] = httpx.get,
) -> DrainResult:
    deadline = time.monotonic() + timeout_sec
    last_error = "no response"
    while time.monotonic() <= deadline:
        try:
            response = get(url, timeout=2.0)
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                json_body = getattr(response, "json", None)
                payload = json_body() if callable(json_body) else None
                active_count = _active_node_count(payload)
                if active_count == 0:
                    return DrainResult(ok=True, message="no active local nodes")
                if active_count is None:
                    last_error = "health payload did not include appium_processes.running_nodes"
                else:
                    suffix = "node" if active_count == 1 else "nodes"
                    last_error = f"{active_count} active local {suffix}"
            else:
                last_error = f"unexpected status {status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(interval_sec)
    return DrainResult(ok=False, message=f"update drain timed out: {last_error}")


def update_agent(
    config: InstallConfig,
    *,
    to_version: str | None = None,
    os_name: str | None = None,
    run_command: Callable[[list[str]], None] = _run_command,
    drain_check: Callable[[str], DrainResult] = wait_for_update_drain,
    health_check: Callable[[str], HealthCheckResult] = poll_agent_health,
    uid: int | None = None,
) -> UpdateResult:
    resolved_os = os_name or platform.system()
    health_url = _health_url(config)

    drain = drain_check(health_url)
    if not drain.ok:
        raise RuntimeError(drain.message)

    run_command(_uv_upgrade_command(to_version))
    run_command(_restart_command(resolved_os, uid=uid))
    health = health_check(health_url)

    return UpdateResult(to_version=to_version, restarted=True, drain=drain, health=health)
