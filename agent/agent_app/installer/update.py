from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from agent_app.installer.install import (
    HealthCheckCallable,
    HealthCheckResult,
    _run_command,
    poll_agent_health,
)
from agent_app.installer.uv_runtime import UvRuntime, build_upgrade_command

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.installer.identity import OperatorIdentity
    from agent_app.installer.plan import InstallConfig


# ---------------------------------------------------------------------------
# Typed exception hierarchy
# ---------------------------------------------------------------------------


class UpdateError(RuntimeError):
    """Base for update lifecycle failures."""


class UpdateDrainError(UpdateError):
    """Active local nodes prevented the upgrade."""


class UvNotFoundError(UpdateError):
    """No usable uv binary discovered for the operator."""


class UpdateUpgradeError(UpdateError):
    """`uv tool upgrade` failed."""


class UpdateRestartError(UpdateError):
    """systemctl/launchctl restart failed."""


class UpdateHealthError(UpdateError):
    """Post-restart health poll failed."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrainResult:
    ok: bool
    message: str


class DrainCheckCallable(Protocol):
    def __call__(self, url: str, *, auth: tuple[str, str] | None = None) -> DrainResult:
        raise NotImplementedError


@dataclass(frozen=True)
class UpdateResult:
    to_version: str | None
    restarted: bool
    drain: DrainResult
    health: HealthCheckResult


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _agent_package_spec(to_version: str | None) -> str:
    return f"gridfleet-agent=={to_version}" if to_version else "gridfleet-agent"


def _restart_command(os_name: str, *, operator_uid: int) -> list[str]:
    if os_name == "Linux":
        return ["systemctl", "restart", "gridfleet-agent"]
    if os_name == "Darwin":
        return ["launchctl", "kickstart", "-k", f"gui/{operator_uid}/com.gridfleet.agent"]
    raise RuntimeError(f"Unsupported OS: {os_name}")


def _health_url(config: InstallConfig) -> str:
    return f"http://localhost:{config.port}/agent/health"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_update_dry_run(
    config: InstallConfig,
    *,
    operator: OperatorIdentity,
    uv_runtime: UvRuntime,
    to_version: str | None = None,
    os_name: str | None = None,
) -> str:
    resolved_os = os_name or platform.system()
    package_spec = _agent_package_spec(to_version)

    # Resolve uv path display
    if uv_runtime.bin_path is not None:
        uv_path_display = str(uv_runtime.bin_path)
    else:
        searched_str = ", ".join(uv_runtime.searched) if uv_runtime.searched else "(none)"
        uv_path_display = f"not found (searched: {searched_str})"

    # Resolve upgrade command
    try:
        upgrade_cmd = build_upgrade_command(
            uv_runtime,
            operator=operator,
            package_spec=package_spec,
            os_name=resolved_os,
            current_uid=os.getuid(),
        )
        uv_command = " ".join(upgrade_cmd)
    except RuntimeError as exc:
        uv_command = f"uv missing — {exc}"

    # Resolve restart command
    try:
        restart_command = " ".join(_restart_command(resolved_os, operator_uid=operator.uid))
    except RuntimeError as exc:
        restart_command = str(exc).replace("Unsupported OS", "unsupported OS", 1)

    return f"""GridFleet Agent update dry run

Operator: {operator.login} (uid={operator.uid}, home={operator.home})
uv binary: {uv_path_display}

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
    auth: tuple[str, str] | None = None,
) -> DrainResult:
    deadline = time.monotonic() + timeout_sec
    last_error = "no response"
    while time.monotonic() <= deadline:
        try:
            response = get(url, timeout=2.0, auth=auth) if auth else get(url, timeout=2.0)
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
    operator: OperatorIdentity,
    uv_runtime: UvRuntime,
    to_version: str | None = None,
    os_name: str | None = None,
    current_uid: int | None = None,
    run_command: Callable[[list[str]], None] = _run_command,
    drain_check: DrainCheckCallable = wait_for_update_drain,
    health_check: HealthCheckCallable = poll_agent_health,
) -> UpdateResult:
    resolved_os = os_name or platform.system()
    health_url = _health_url(config)

    api_auth = (
        (config.api_auth_username, config.api_auth_password)
        if config.api_auth_username and config.api_auth_password
        else None
    )

    drain = drain_check(health_url, auth=api_auth) if api_auth else drain_check(health_url)
    if not drain.ok:
        raise UpdateDrainError(drain.message)

    package_spec = _agent_package_spec(to_version)
    try:
        upgrade_cmd = build_upgrade_command(
            uv_runtime,
            operator=operator,
            package_spec=package_spec,
            os_name=resolved_os,
            current_uid=current_uid if current_uid is not None else os.getuid(),
        )
    except RuntimeError as exc:
        raise UvNotFoundError(str(exc)) from exc

    try:
        run_command(upgrade_cmd)
    except RuntimeError as exc:
        raise UpdateUpgradeError(str(exc)) from exc

    try:
        run_command(_restart_command(resolved_os, operator_uid=operator.uid))
    except RuntimeError as exc:
        raise UpdateRestartError(str(exc)) from exc

    health = health_check(health_url, auth=api_auth) if api_auth else health_check(health_url)
    if not health.ok:
        raise UpdateHealthError(health.message)

    return UpdateResult(to_version=to_version, restarted=True, drain=drain, health=health)
